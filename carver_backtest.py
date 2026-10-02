"""
Backtest of the Carver EWMAC "+20" strategy on the Nifty 500 (daily, weekly or monthly bars).

Rules (evaluated on CLOSED bars only, no look-ahead):
    ENTRY : forecast reaches the entry level (default 20) while flat
    EXIT  : forecast drops below the exit level (default 19) while long
Orders fill at the OPEN of the next bar (next day / week / month), the
same way the Pine notes say to act: "signal on the close, trade next session".

Outputs
    * every trade (trades.csv)
    * per-trade statistics (win rate, avg return, profit factor, holding time ...)
    * an equal-weight portfolio of all open positions, rebalanced every bar,
      compared with the Nifty 500 index (equity.csv)

Usage
    python carver_backtest.py                         # full Nifty 500, monthly
    python carver_backtest.py -t W                    # weekly   (-t D for daily)
    python carver_backtest.py --start 2010-01-01      # only trades entered from 2010
    python carver_backtest.py --entry 19.5 --exit 19  # looser entry
    python carver_backtest.py --symbols AEGISLOG TCS  # quick test

Price data is cached in ./data_cache so re-runs don't download again
(use --refresh to force a new download).

Caveat: the universe is TODAY's Nifty 500 list, so stocks that were dropped
or delisted in the past are missing (survivorship bias -> results are
optimistic).
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np
import pandas as pd
import yfinance as yf

from carver_scanner import TIMEFRAMES, Params, carver_forecast, load_nifty500

CACHE_DIR = "data_cache"
BENCHMARK = "^CRSLDX"  # Nifty 500 index on Yahoo Finance


# ---------------------------------------------------------------------------
#  Data
# ---------------------------------------------------------------------------
def download_ohlc(tickers: list[str], chunk: int = 50) -> dict[str, pd.DataFrame]:
    """Daily Open/Close, split-adjusted, not dividend-adjusted (TradingView default)."""
    out: dict[str, pd.DataFrame] = {}
    for start in range(0, len(tickers), chunk):
        batch = tickers[start:start + chunk]
        data = None
        for attempt in range(3):
            try:
                data = yf.download(batch, period="max", interval="1d", auto_adjust=False,
                                   group_by="ticker", threads=True, progress=False)
                break
            except Exception as exc:
                print(f"  download error ({exc}); retrying", file=sys.stderr)
                time.sleep(5 * (attempt + 1))
        if data is None:
            continue
        for tic in batch:
            try:
                df = data[tic] if isinstance(data.columns, pd.MultiIndex) else data
                df = df[["Open", "Close"]].dropna()
            except KeyError:
                continue
            if not df.empty:
                df.index = pd.to_datetime(df.index).tz_localize(None)
                out[tic] = df
        print(f"  downloaded {min(start + chunk, len(tickers))}/{len(tickers)}", file=sys.stderr)
    return out


def clean_prices(df: pd.DataFrame, jump: float = 0.5) -> pd.DataFrame:
    """Repair Yahoo data errors before testing.

    NSE stocks rarely move more than +50% / -33% in one day, so such a
    close-to-close move is treated as a data problem (an unadjusted split /
    bonus, or a bad tick that reverses the next day) and is neutralised by
    rescaling all earlier prices, exactly as a split adjustment would.
    Opens far away from the previous close are replaced by that close."""
    df = df[(df["Close"] > 0) & (df["Open"] > 0)].copy()
    hi, lo = 1 + jump, 1 / (1 + jump)
    r = df["Close"] / df["Close"].shift(1)
    factor = r.where((r > hi) | (r < lo), 1.0).fillna(1.0)
    # multiplier for each row = product of all LATER jump factors
    mult = factor[::-1].cumprod()[::-1].shift(-1).fillna(1.0)
    df["Open"] *= mult
    df["Close"] *= mult
    o = df["Open"] / df["Close"].shift(1)
    bad_open = (o > hi) | (o < lo)
    df.loc[bad_open, "Open"] = df["Close"].shift(1)[bad_open]
    return df


def load_prices(symbols: list[str], refresh: bool) -> tuple[dict[str, pd.DataFrame], pd.DataFrame | None]:
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache = os.path.join(CACHE_DIR, "daily_ohlc.pkl")
    cached: dict[str, pd.DataFrame] = {}
    if os.path.exists(cache) and not refresh:
        cached = pd.read_pickle(cache)
    wanted = [f"{s}.NS" for s in symbols] + [BENCHMARK]
    missing = [t for t in wanted if t not in cached]
    if missing:
        print(f"Downloading daily prices for {len(missing)} tickers...", file=sys.stderr)
        cached.update(download_ohlc(missing))
        pd.to_pickle(cached, cache)
    stocks = {s: clean_prices(cached[f"{s}.NS"]) for s in symbols if f"{s}.NS" in cached}
    bench = cached.get(BENCHMARK)
    return stocks, clean_prices(bench) if bench is not None else None


BARS_PER_YEAR = {"D": 252, "W": 52, "M": 12}
TF_NAME = {"D": "Daily", "W": "Weekly", "M": "Monthly"}


def to_bars(daily: pd.DataFrame, tf: str, drop_current: bool = True) -> pd.DataFrame:
    """Daily/weekly/monthly bars: Open = first open of the bar, Close = last close."""
    rule = TIMEFRAMES[tf]["rule"]
    if rule is None:
        m = daily[["Open", "Close"]].dropna()
    else:
        m = pd.DataFrame({
            "Open": daily["Open"].resample(rule).first(),
            "Close": daily["Close"].resample(rule).last(),
        }).dropna()
    if drop_current and len(m):
        today = pd.Timestamp.today().normalize()
        if m.index[-1] >= today:  # bar not finished yet
            m = m.iloc[:-1]
    return m


# ---------------------------------------------------------------------------
#  Backtest
# ---------------------------------------------------------------------------
def backtest_symbol(sym: str, m: pd.DataFrame, tf: str, entry: float, exit_: float,
                    cost: float, start: pd.Timestamp | None, p: Params) -> tuple[list[dict], pd.Series, pd.Series]:
    """Return (trades, held flag per bar, this stock's return per bar while held)."""
    fc = carver_forecast(m["Close"], tf, p)["combined"].to_numpy()
    opens = m["Open"].to_numpy()
    closes = m["Close"].to_numpy()
    idx = m.index
    n = len(m)

    trades: list[dict] = []
    held = np.zeros(n)
    bar_ret = np.zeros(n)  # this stock's return on each bar while it is held

    def book(e: int, x: int) -> None:
        """Fill-accurate returns: buy at open of bar e, sell at open of bar x (x == n: still held)."""
        held[e:min(x + 1, n)] = 1
        bar_ret[e] = closes[e] / (opens[e] * (1 + cost)) - 1
        if e + 1 < min(x, n):
            bar_ret[e + 1:min(x, n)] = closes[e + 1:min(x, n)] / closes[e:min(x, n) - 1] - 1
        if x < n:  # exit bar: only the overnight gap until the sell at the open
            bar_ret[x] = opens[x] * (1 - cost) / closes[x - 1] - 1
    in_pos = False
    e_i = 0
    for i in range(n):
        f = fc[i]
        if np.isnan(f):
            continue
        if not in_pos:
            if f >= entry - 1e-9 and i + 1 < n and (start is None or idx[i + 1] >= start):
                in_pos, e_i = True, i + 1  # fill at next bar's open
                entry_fc = f
        else:
            if f < exit_:
                x_i = i + 1
                if x_i < n:
                    px, xdate, status = opens[x_i], idx[x_i], "closed"
                else:  # exit signal on last closed bar: next open not available yet
                    px, xdate, status = closes[i], idx[i], "exit pending (marked at close)"
                trades.append(_trade(sym, idx, opens, e_i, entry_fc, xdate, px, f, status, cost))
                book(e_i, x_i)
                in_pos = False
    if in_pos:
        trades.append(_trade(sym, idx, opens, e_i, entry_fc, idx[-1], closes[-1], fc[-1], "open", cost))
        book(e_i, n)
    return trades, pd.Series(held, index=idx, name=sym), pd.Series(bar_ret, index=idx, name=sym)


def _trade(sym, idx, opens, e_i, entry_fc, xdate, xpx, exit_fc, status, cost) -> dict:
    epx = opens[e_i]
    ret = (xpx * (1 - cost)) / (epx * (1 + cost)) - 1
    return {
        "Symbol": sym, "EntryBar": idx[e_i].strftime("%Y-%m-%d"), "EntryPrice": round(epx, 2),
        "SignalFc": round(entry_fc, 2), "ExitBar": xdate.strftime("%Y-%m-%d"),
        "ExitPrice": round(float(xpx), 2), "ExitFc": round(float(exit_fc), 2),
        "Bars": int(idx.get_loc(xdate) - e_i), "Days": (xdate - idx[e_i]).days,
        "Return%": round(ret * 100, 2), "Status": status,
    }


def trade_stats(t: pd.DataFrame) -> dict:
    r = t["Return%"] / 100
    wins, losses = r[r > 0], r[r <= 0]
    pf = wins.sum() / -losses.sum() if losses.sum() < 0 else np.inf
    return {
        "Trades": len(t),
        "Closed / open": f"{(t['Status'] == 'closed').sum()} / {(t['Status'] != 'closed').sum()}",
        "Win rate": f"{len(wins) / len(t) * 100:.1f}%",
        "Avg return / trade": f"{r.mean() * 100:.2f}%",
        "Median return / trade": f"{r.median() * 100:.2f}%",
        "Avg win": f"{wins.mean() * 100:.2f}%" if len(wins) else "-",
        "Avg loss": f"{losses.mean() * 100:.2f}%" if len(losses) else "-",
        "Profit factor": f"{pf:.2f}",
        "Best / worst": f"{r.max() * 100:.1f}% / {r.min() * 100:.1f}%",
        "Avg holding": f"{t['Bars'].mean():.1f} bars ({t['Days'].mean() / 30.4:.1f} months)",
        "Median holding": f"{t['Bars'].median():.0f} bars ({t['Days'].median() / 30.4:.1f} months)",
    }


def perf(rets: pd.Series, per_year: int) -> dict:
    rets = rets.dropna()
    eq = (1 + rets).cumprod()
    years = (rets.index[-1] - rets.index[0]).days / 365.25
    cagr = eq.iloc[-1] ** (1 / years) - 1 if years > 0 else np.nan
    vol = rets.std() * np.sqrt(per_year)
    dd = (eq / eq.cummax() - 1).min()
    return {"CAGR": f"{cagr * 100:.2f}%", "Ann. vol": f"{vol * 100:.2f}%",
            "Sharpe (rf=0)": f"{(rets.mean() * per_year) / vol:.2f}" if vol > 0 else "-",
            "Max drawdown": f"{dd * 100:.2f}%", "Total return": f"{(eq.iloc[-1] - 1) * 100:.1f}%"}


def main() -> None:
    ap = argparse.ArgumentParser(description="Backtest Carver EWMAC +20 entry / <19 exit on Nifty 500")
    ap.add_argument("--timeframe", "-t", choices=["D", "W", "M"], default="M",
                    help="D=daily, W=weekly, M=monthly (default M)")
    ap.add_argument("--entry", type=float, default=20.0, help="enter when forecast >= this (default 20)")
    ap.add_argument("--exit", type=float, default=19.0, help="exit when forecast < this (default 19)")
    ap.add_argument("--cost", type=float, default=0.15,
                    help="cost per side in %% (brokerage+STT+slippage, default 0.15)")
    ap.add_argument("--start", help="only take trades entered on/after this date, e.g. 2010-01-01")
    ap.add_argument("--symbols", nargs="+", help="test only these NSE symbols")
    ap.add_argument("--exclude", nargs="+", default=[], help="leave these NSE symbols out")
    ap.add_argument("--list-file", help="local copy of ind_nifty500list.csv")
    ap.add_argument("--refresh", action="store_true", help="re-download prices instead of using the cache")
    ap.add_argument("--out-dir", help="default: backtest_results/<timeframe>")
    args = ap.parse_args()

    p = Params()
    tf, per_year = args.timeframe, BARS_PER_YEAR[args.timeframe]
    out_dir = args.out_dir or os.path.join("backtest_results", TF_NAME[tf].lower())
    symbols = [s.upper() for s in args.symbols] if args.symbols else list(load_nifty500(args.list_file)["Symbol"])
    symbols = [s for s in symbols if s not in {x.upper() for x in args.exclude}]
    stocks, bench = load_prices(symbols, args.refresh)
    print(f"Price data for {len(stocks)}/{len(symbols)} symbols", file=sys.stderr)

    start = pd.Timestamp(args.start) if args.start else None
    all_trades, positions, bar_rets = [], [], []
    for sym, daily in stocks.items():
        m = to_bars(daily, tf)
        if len(m) < 3:
            continue
        trades, held, ret = backtest_symbol(sym, m, tf, args.entry, args.exit, args.cost / 100, start, p)
        all_trades += trades
        positions.append(held)
        bar_rets.append(ret)

    if not all_trades:
        print("No trades.")
        return
    os.makedirs(out_dir, exist_ok=True)
    trades = pd.DataFrame(all_trades).sort_values(["EntryBar", "Symbol"]).reset_index(drop=True)
    trades.to_csv(os.path.join(out_dir, "trades.csv"), index=False)

    print(f"\n##### {TF_NAME[tf]} timeframe #####")
    print(f"\n=== Trade statistics (entry >= {args.entry:g}, exit < {args.exit:g}, "
          f"cost {args.cost}%/side) ===")
    for k, v in trade_stats(trades).items():
        print(f"  {k:<22}{v}")

    # Equal-weight portfolio of all open positions, rebalanced every bar; cash (0%) when none.
    pos = pd.concat(positions, axis=1, sort=True).fillna(0)
    rets = pd.concat(bar_rets, axis=1, sort=True).reindex(pos.index)
    first = trades["EntryBar"].min()
    pos, rets = pos.loc[first:], rets.loc[first:]
    n_held = pos.sum(axis=1)
    # costs are already inside the entry/exit bar returns
    port = ((pos * rets.fillna(0)).sum(axis=1) / n_held.replace(0, np.nan)).fillna(0).rename("Strategy")

    eq = pd.DataFrame({"Strategy": (1 + port).cumprod(), "Positions": n_held})
    print(f"\n=== Equal-weight portfolio ({port.index[0]:%Y-%m} to {port.index[-1]:%Y-%m}) ===")
    stats = {"Strategy": perf(port, per_year)}
    if bench is not None:
        b = to_bars(bench, tf)["Close"].pct_change().reindex(port.index)
        stats["Nifty 500 index"] = perf(b, per_year)
        eq["Nifty500"] = (1 + b.fillna(0)).cumprod()
        common = b.notna()
        if not common.all():  # index history is shorter -> also compare on the same period
            span = f"{b[common].index[0]:%Y-%m}+"
            stats[f"Strategy ({span})"] = perf(port[common], per_year)
            stats = {k: stats[k] for k in ["Strategy", f"Strategy ({span})", "Nifty 500 index"]}
    print(pd.DataFrame(stats).to_string())
    print(f"  Avg positions held: {n_held.mean():.1f}   Bars fully in cash: {(n_held == 0).sum()}")
    eq.to_csv(os.path.join(out_dir, "equity.csv"))

    yearly = (1 + port).groupby(port.index.year).prod() - 1
    print("\n=== Strategy return by year ===")
    print((yearly * 100).round(1).to_string())

    open_now = trades[trades["Status"] != "closed"]
    if len(open_now):
        print(f"\n=== Currently open positions ({len(open_now)}) ===")
        print(open_now[["Symbol", "EntryBar", "EntryPrice", "ExitPrice", "Return%", "Status"]]
              .to_string(index=False))
    print(f"\nTrades saved to {out_dir}/trades.csv, equity curve to {out_dir}/equity.csv")


if __name__ == "__main__":
    main()
