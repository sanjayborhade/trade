"""
Backtest of the Carver EWMAC "+20" strategy on the Nifty 500 (monthly bars).

Rules (evaluated on CLOSED monthly bars only, no look-ahead):
    ENTRY : forecast reaches the entry level (default 20) while flat
    EXIT  : forecast drops below the exit level (default 19) while long
Orders fill at the OPEN of the next month (first trading day's open), the
same way the Pine notes say to act: "signal on the close, trade next session".

Outputs
    * every trade (trades.csv)
    * per-trade statistics (win rate, avg return, profit factor, holding time ...)
    * an equal-weight portfolio of all open positions, rebalanced monthly,
      compared with the Nifty 500 index (equity.csv)

Usage
    python carver_backtest.py                         # full Nifty 500, defaults
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

from carver_scanner import Params, carver_forecast, load_nifty500

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
    stocks = {s: cached[f"{s}.NS"] for s in symbols if f"{s}.NS" in cached}
    return stocks, cached.get(BENCHMARK)


def to_monthly(daily: pd.DataFrame, drop_current: bool = True) -> pd.DataFrame:
    """Monthly bars: Close = last close, NextOpen = first open of the following month."""
    m = pd.DataFrame({
        "Open": daily["Open"].resample("ME").first(),
        "Close": daily["Close"].resample("ME").last(),
    }).dropna()
    if drop_current and len(m):
        today = pd.Timestamp.today().normalize()
        if m.index[-1] >= today:  # month not finished yet
            m = m.iloc[:-1]
    return m


# ---------------------------------------------------------------------------
#  Backtest
# ---------------------------------------------------------------------------
def backtest_symbol(sym: str, m: pd.DataFrame, entry: float, exit_: float,
                    cost: float, start: pd.Timestamp | None, p: Params) -> tuple[list[dict], pd.Series]:
    """Return (trades, monthly position series). position[t] = 1 means held during month t."""
    fc = carver_forecast(m["Close"], "M", p)["combined"].to_numpy()
    opens = m["Open"].to_numpy()
    closes = m["Close"].to_numpy()
    idx = m.index
    n = len(m)

    trades: list[dict] = []
    held = np.zeros(n)
    in_pos = False
    e_i = 0
    for i in range(n):
        f = fc[i]
        if np.isnan(f):
            continue
        if not in_pos:
            if f >= entry - 1e-9 and i + 1 < n and (start is None or idx[i + 1] >= start):
                in_pos, e_i = True, i + 1  # fill at next month's open
                entry_fc = f
        else:
            if f < exit_:
                x_i = i + 1
                if x_i < n:
                    px, xdate, status = opens[x_i], idx[x_i], "closed"
                else:  # exit signal on last closed bar: next open not available yet
                    px, xdate, status = closes[i], idx[i], "exit pending (marked at close)"
                trades.append(_trade(sym, idx, opens, e_i, entry_fc, xdate, px, f, status, cost))
                held[e_i:x_i] = 1
                in_pos = False
    if in_pos:
        trades.append(_trade(sym, idx, opens, e_i, entry_fc, idx[-1], closes[-1], fc[-1], "open", cost))
        held[e_i:] = 1
    return trades, pd.Series(held, index=idx, name=sym)


def _trade(sym, idx, opens, e_i, entry_fc, xdate, xpx, exit_fc, status, cost) -> dict:
    epx = opens[e_i]
    ret = (xpx * (1 - cost)) / (epx * (1 + cost)) - 1
    months = (xdate.year - idx[e_i].year) * 12 + xdate.month - idx[e_i].month
    return {
        "Symbol": sym, "EntryMonth": idx[e_i].strftime("%Y-%m"), "EntryPrice": round(epx, 2),
        "SignalFc": round(entry_fc, 2), "ExitMonth": xdate.strftime("%Y-%m"),
        "ExitPrice": round(float(xpx), 2), "ExitFc": round(float(exit_fc), 2),
        "Months": months, "Return%": round(ret * 100, 2), "Status": status,
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
        "Avg holding": f"{t['Months'].mean():.1f} months",
        "Median holding": f"{t['Months'].median():.0f} months",
    }


def perf(rets: pd.Series) -> dict:
    rets = rets.dropna()
    eq = (1 + rets).cumprod()
    years = len(rets) / 12
    cagr = eq.iloc[-1] ** (1 / years) - 1 if years > 0 else np.nan
    vol = rets.std() * np.sqrt(12)
    dd = (eq / eq.cummax() - 1).min()
    return {"CAGR": f"{cagr * 100:.2f}%", "Ann. vol": f"{vol * 100:.2f}%",
            "Sharpe (rf=0)": f"{(rets.mean() * 12) / vol:.2f}" if vol > 0 else "-",
            "Max drawdown": f"{dd * 100:.2f}%", "Total return": f"{(eq.iloc[-1] - 1) * 100:.1f}%"}


def main() -> None:
    ap = argparse.ArgumentParser(description="Backtest Carver EWMAC +20 entry / <19 exit on Nifty 500 (monthly)")
    ap.add_argument("--entry", type=float, default=20.0, help="enter when forecast >= this (default 20)")
    ap.add_argument("--exit", type=float, default=19.0, help="exit when forecast < this (default 19)")
    ap.add_argument("--cost", type=float, default=0.15,
                    help="cost per side in %% (brokerage+STT+slippage, default 0.15)")
    ap.add_argument("--start", help="only take trades entered on/after this date, e.g. 2010-01-01")
    ap.add_argument("--symbols", nargs="+", help="test only these NSE symbols")
    ap.add_argument("--list-file", help="local copy of ind_nifty500list.csv")
    ap.add_argument("--refresh", action="store_true", help="re-download prices instead of using the cache")
    ap.add_argument("--out-dir", default="backtest_results")
    args = ap.parse_args()

    p = Params()
    symbols = [s.upper() for s in args.symbols] if args.symbols else list(load_nifty500(args.list_file)["Symbol"])
    stocks, bench = load_prices(symbols, args.refresh)
    print(f"Price data for {len(stocks)}/{len(symbols)} symbols", file=sys.stderr)

    start = pd.Timestamp(args.start) if args.start else None
    all_trades, positions, monthly_rets = [], [], []
    for sym, daily in stocks.items():
        m = to_monthly(daily)
        if len(m) < 3:
            continue
        trades, held = backtest_symbol(sym, m, args.entry, args.exit, args.cost / 100, start, p)
        all_trades += trades
        positions.append(held)
        # close-to-close proxy for the portfolio curve (position taken at next open ~ prior close)
        monthly_rets.append(m["Close"].pct_change().rename(sym))

    if not all_trades:
        print("No trades.")
        return
    os.makedirs(args.out_dir, exist_ok=True)
    trades = pd.DataFrame(all_trades).sort_values(["EntryMonth", "Symbol"]).reset_index(drop=True)
    trades.to_csv(os.path.join(args.out_dir, "trades.csv"), index=False)

    print(f"\n=== Trade statistics (entry >= {args.entry:g}, exit < {args.exit:g}, "
          f"cost {args.cost}%/side) ===")
    for k, v in trade_stats(trades).items():
        print(f"  {k:<22}{v}")

    # Equal-weight portfolio of all open positions, rebalanced monthly; cash (0%) when none.
    pos = pd.concat(positions, axis=1, sort=True).fillna(0)
    rets = pd.concat(monthly_rets, axis=1, sort=True).reindex(pos.index)
    first = trades["EntryMonth"].min()
    pos, rets = pos.loc[first:], rets.loc[first:]
    n_held = pos.sum(axis=1)
    gross = (pos * rets.fillna(0)).sum(axis=1) / n_held.replace(0, np.nan)
    turnover = pos.diff().abs().sum(axis=1) / n_held.replace(0, np.nan)
    port = (gross.fillna(0) - turnover.fillna(0) * args.cost / 100).rename("Strategy")

    eq = pd.DataFrame({"Strategy": (1 + port).cumprod(), "Positions": n_held})
    print(f"\n=== Equal-weight portfolio ({port.index[0]:%Y-%m} to {port.index[-1]:%Y-%m}) ===")
    stats = {"Strategy": perf(port)}
    if bench is not None:
        b = to_monthly(bench)["Close"].pct_change().reindex(port.index)
        stats["Nifty 500 index"] = perf(b)
        eq["Nifty500"] = (1 + b.fillna(0)).cumprod()
        common = b.notna()
        if not common.all():  # index history is shorter -> also compare on the same months
            span = f"{b[common].index[0]:%Y-%m}+"
            stats[f"Strategy ({span})"] = perf(port[common])
            stats = {k: stats[k] for k in ["Strategy", f"Strategy ({span})", "Nifty 500 index"]}
    print(pd.DataFrame(stats).to_string())
    print(f"  Avg positions held: {n_held.mean():.1f}   Months fully in cash: {(n_held == 0).sum()}")
    eq.to_csv(os.path.join(args.out_dir, "equity.csv"))

    yearly = (1 + port).groupby(port.index.year).prod() - 1
    print("\n=== Strategy return by year ===")
    print((yearly * 100).round(1).to_string())

    open_now = trades[trades["Status"] != "closed"]
    if len(open_now):
        print(f"\n=== Currently open positions ({len(open_now)}) ===")
        print(open_now[["Symbol", "EntryMonth", "EntryPrice", "ExitPrice", "Return%", "Status"]]
              .to_string(index=False))
    print(f"\nTrades saved to {args.out_dir}/trades.csv, equity curve to {args.out_dir}/equity.csv")


if __name__ == "__main__":
    main()
