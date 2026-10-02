"""
Monthly Ichimoku Cloud-Top Crossover backtest on the NIFTY 500 universe.

ENTRY : monthly close crosses ABOVE the cloud top  (prev close <= prev top, close > top)
EXIT  : monthly close crosses BELOW the cloud top  (prev close >= prev top, close < top)

Ichimoku (standard 9 / 26 / 52, computed on monthly bars):
    Tenkan   = (HH9  + LL9)  / 2
    Kijun    = (HH26 + LL26) / 2
    Span A   = (Tenkan + Kijun) / 2         plotted 26 bars ahead
    Span B   = (HH52 + LL52) / 2            plotted 26 bars ahead
    Cloud top at bar t = max(SpanA[t-26], SpanB[t-26])

Signals are evaluated on the monthly close. By default trades are filled at the
NEXT month's open (no look-ahead); use --fill close to fill at the signal close.

Usage:
    pip install pandas numpy yfinance requests
    python ichimoku_monthly_backtest.py                 # full run
    python ichimoku_monthly_backtest.py --fill close    # fill on signal close
    python ichimoku_monthly_backtest.py --cost 0.2      # 0.2% round-trip cost
"""

import argparse
import io
import os
import time

import numpy as np
import pandas as pd
import requests
import yfinance as yf

NIFTY500_URL = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
BENCHMARK = "^CRSLDX"  # NIFTY 500 index on Yahoo Finance
OUT_DIR = "results"


# --------------------------------------------------------------------------- data
def get_nifty500_symbols() -> list[str]:
    r = requests.get(NIFTY500_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    return [f"{s.strip()}.NS" for s in df["Symbol"]]


def download_monthly(tickers: list[str], batch: int = 50) -> dict[str, pd.DataFrame]:
    """Download max-history monthly OHLC (split/dividend adjusted) per ticker."""
    data = {}
    for i in range(0, len(tickers), batch):
        chunk = tickers[i : i + batch]
        raw = yf.download(
            chunk, period="max", interval="1mo", auto_adjust=True,
            group_by="ticker", progress=False, threads=False,
        )
        for t in chunk:
            try:
                df = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
                df = df[["Open", "High", "Low", "Close"]].dropna()
                if len(df):
                    data[t] = df
            except KeyError:
                pass
        print(f"  downloaded {min(i + batch, len(tickers))}/{len(tickers)}")
        time.sleep(1)
    # drop the current, still-forming month so signals use completed bars only
    this_month = pd.Timestamp.today().to_period("M").to_timestamp()
    return {t: d[d.index < this_month] for t, d in data.items()}


# --------------------------------------------------------------------- indicator
def cloud_top(df: pd.DataFrame, t: int = 9, k: int = 26, s: int = 52) -> pd.Series:
    hi, lo = df["High"], df["Low"]
    tenkan = (hi.rolling(t).max() + lo.rolling(t).min()) / 2
    kijun = (hi.rolling(k).max() + lo.rolling(k).min()) / 2
    span_a = ((tenkan + kijun) / 2).shift(k)
    span_b = ((hi.rolling(s).max() + lo.rolling(s).min()) / 2).shift(k)
    return pd.concat([span_a, span_b], axis=1).max(axis=1, skipna=False)


# ---------------------------------------------------------------------- backtest
def backtest_symbol(sym: str, df: pd.DataFrame, fill: str, cost: float):
    """Return (trades list, monthly in-position return series)."""
    df = df.copy()
    df["top"] = cloud_top(df)
    c, top = df["Close"], df["top"]
    df["entry_sig"] = (c > top) & (c.shift(1) <= top.shift(1))
    df["exit_sig"] = (c < top) & (c.shift(1) >= top.shift(1))

    trades, pos = [], np.zeros(len(df))  # pos[i] = holding during bar i (for returns)
    in_pos, entry_px, entry_dt, entry_i = False, None, None, None
    idx = df.index

    for i in range(len(df)):
        if not in_pos and df["entry_sig"].iat[i]:
            j = i + 1 if fill == "open" else i
            if j >= len(df):
                break  # signal on last bar, can't fill yet
            entry_px = df["Open"].iat[j] if fill == "open" else c.iat[i]
            entry_dt, entry_i, in_pos = idx[j], j, True
        elif in_pos and df["exit_sig"].iat[i] and i >= entry_i:
            j = i + 1 if fill == "open" else i
            if j >= len(df):
                break
            exit_px = df["Open"].iat[j] if fill == "open" else c.iat[i]
            trades.append(_trade(sym, entry_dt, entry_px, idx[j], exit_px, cost, False))
            in_pos = False

    if in_pos:  # mark open position to last close
        trades.append(_trade(sym, entry_dt, entry_px, idx[-1], c.iat[-1], cost, True))

    # Monthly return series while holding, consistent with the fill prices:
    # first month from entry fill, last month to exit fill, close-to-close between.
    ret = pd.Series(np.nan, index=idx)
    o = df["Open"]
    for tr in trades:
        a = idx.get_loc(pd.Timestamp(tr["entry_date"]))
        b = idx.get_loc(pd.Timestamp(tr["exit_date"]))
        if fill == "open":
            if a == b:
                continue
            ret.iat[a] = c.iat[a] / o.iat[a] - 1
            ret.iloc[a + 1 : b] = (c / c.shift(1) - 1).iloc[a + 1 : b]
            ret.iat[b] = (c.iat[b] if tr["open"] else o.iat[b]) / c.iat[b - 1] - 1
        else:
            if a == b:
                continue
            ret.iloc[a + 1 : b + 1] = (c / c.shift(1) - 1).iloc[a + 1 : b + 1]
            a += 1  # first month of exposure
        # half the round-trip cost on entry, half on exit (not on still-open trades)
        ret.iat[a] -= cost / 200
        if not tr["open"]:
            ret.iat[b] -= cost / 200
    return trades, ret


def _trade(sym, edt, epx, xdt, xpx, cost, is_open):
    ret = xpx / epx - 1 - cost / 100
    months = (xdt.year - edt.year) * 12 + (xdt.month - edt.month)
    return dict(symbol=sym, entry_date=edt.date(), entry_price=round(epx, 2),
                exit_date=xdt.date(), exit_price=round(xpx, 2),
                return_pct=round(ret * 100, 2), months_held=months, open=is_open)


# ----------------------------------------------------------------------- metrics
def trade_stats(tr: pd.DataFrame) -> dict:
    r = tr["return_pct"] / 100
    wins, losses = r[r > 0], r[r <= 0]
    return {
        "Total trades": len(tr),
        "Closed trades": int((~tr["open"]).sum()),
        "Open trades": int(tr["open"].sum()),
        "Win rate %": round(100 * len(wins) / len(r), 2) if len(r) else 0,
        "Avg return / trade %": round(100 * r.mean(), 2),
        "Median return / trade %": round(100 * r.median(), 2),
        "Avg winner %": round(100 * wins.mean(), 2) if len(wins) else 0,
        "Avg loser %": round(100 * losses.mean(), 2) if len(losses) else 0,
        "Best trade %": round(100 * r.max(), 2),
        "Worst trade %": round(100 * r.min(), 2),
        "Profit factor": round(wins.sum() / -losses.sum(), 2) if losses.sum() < 0 else np.inf,
        "Avg holding (months)": round(tr["months_held"].mean(), 1),
    }


def equity_stats(ret: pd.Series, name: str) -> dict:
    ret = ret.dropna()
    eq = (1 + ret).cumprod()
    yrs = len(ret) / 12
    dd = eq / eq.cummax() - 1
    vol = ret.std() * np.sqrt(12)
    return {
        "Series": name,
        "Start": ret.index[0].date(), "End": ret.index[-1].date(),
        "CAGR %": round(100 * (eq.iloc[-1] ** (1 / yrs) - 1), 2),
        "Total return %": round(100 * (eq.iloc[-1] - 1), 1),
        "Ann. volatility %": round(100 * vol, 2),
        "Sharpe (rf=0)": round(ret.mean() * 12 / vol, 2) if vol else np.nan,
        "Max drawdown %": round(100 * dd.min(), 2),
    }


# -------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fill", choices=["open", "close"], default="open")
    ap.add_argument("--cost", type=float, default=0.0, help="round-trip cost in %%")
    ap.add_argument("--refresh", action="store_true", help="re-download data")
    args = ap.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)

    print("Fetching NIFTY 500 constituents...")
    tickers = get_nifty500_symbols()
    print(f"  {len(tickers)} symbols")

    cache = f"{OUT_DIR}/monthly_data.pkl"
    if os.path.exists(cache) and not args.refresh:
        print(f"Loading cached data from {cache} (use --refresh to re-download)")
        data = pd.read_pickle(cache)
    else:
        print("Downloading monthly data...")
        data = download_monthly(tickers + [BENCHMARK])
        pd.to_pickle(data, cache)
    bench = data.pop(BENCHMARK, None)

    all_trades, rets = [], {}
    for sym, df in data.items():
        tr, r = backtest_symbol(sym, df, args.fill, args.cost)
        all_trades += tr
        rets[sym] = r

    trades = pd.DataFrame(all_trades).sort_values(["entry_date", "symbol"])
    trades.to_csv(f"{OUT_DIR}/trades.csv", index=False)

    per_sym = trades.groupby("symbol").agg(
        trades=("return_pct", "size"),
        win_rate=("return_pct", lambda x: round(100 * (x > 0).mean(), 1)),
        avg_ret=("return_pct", "mean"),
        compounded_ret=("return_pct", lambda x: round(100 * ((1 + x / 100).prod() - 1), 1)),
    ).sort_values("compounded_ret", ascending=False)
    per_sym.to_csv(f"{OUT_DIR}/per_symbol.csv")

    # Equal-weight portfolio: each month, average return of all stocks in position
    panel = pd.DataFrame(rets)
    n_held = panel.notna().sum(axis=1)
    port = panel.mean(axis=1).fillna(0.0)
    port = port[n_held.cumsum() > 0]  # start once the first position exists
    pd.DataFrame({"portfolio_ret": port, "positions": n_held.loc[port.index],
                  "equity": (1 + port).cumprod()}).to_csv(f"{OUT_DIR}/equity_curve.csv")

    stats = trade_stats(trades)
    print("\n================ TRADE STATISTICS ================")
    print(f"Symbols with data: {len(data)} | fill: {args.fill} | cost: {args.cost}%")
    for k, v in stats.items():
        print(f"{k:<26}{v}")

    rows = [equity_stats(port, "Strategy (EW portfolio)")]
    for since in ["2010-01-01", "2015-01-01"]:
        p = port[port.index >= since]
        rows.append(equity_stats(p, f"Strategy since {since[:4]}"))
        if bench is not None:
            b = bench["Close"].pct_change()
            rows.append(equity_stats(b[b.index >= since], f"NIFTY 500 B&H since {since[:4]}"))
    eq = pd.DataFrame(rows).set_index("Series")
    eq.to_csv(f"{OUT_DIR}/portfolio_stats.csv")
    print("\n================ PORTFOLIO (equal weight) ================")
    print(eq.to_string())

    print("\nTop 10 symbols by compounded return:")
    print(per_sym.head(10).to_string())
    print("\nCurrently open positions:", int(trades["open"].sum()))
    print(f"\nCSV outputs written to ./{OUT_DIR}/")


if __name__ == "__main__":
    main()
