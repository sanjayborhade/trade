"""
Backtest: MONTHLY HIGH == MONTHLY CLOSE  (NIFTY 500, monthly timeframe)

ENTRY
    A month closes at its high (close >= high * (1 - tol)).
    Entry price: next month's open (default) or the signal month's close
    (--entry close).

EXIT (two separate backtests)
    1. TARGET : target = entry * (1 + signal-month growth), where growth is
                the signal month's close vs the previous month's close.
                Exit when a later month's high touches the target (at the
                target, or at that month's open if it gapped above it).
                No stop-loss: trades that never hit the target stay open and
                are marked to market at the last close.
    2. EMA10  : exit when a monthly close crosses below the 10-period EMA of
                monthly closes (exit at next month's open, or at that close
                with --entry close).

    One position per stock at a time; signals while already in a trade are
    ignored. Costs are applied per round trip (--cost, in %).

Usage
    pip install pandas numpy yfinance
    python monthly_high_close_backtest.py                 # defaults
    python monthly_high_close_backtest.py --tol 0.5       # close within 0.5% of high
    python monthly_high_close_backtest.py --entry close --start 2010-01-01

Data is cached in ./data_cache so re-runs are fast. Outputs trade lists as
CSV in ./results.
"""

import argparse
import io
import os
import time
from datetime import date

import numpy as np
import pandas as pd
import requests
import yfinance as yf

NIFTY500_URL = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
CACHE_DIR = "data_cache"
RESULTS_DIR = "results"


# --------------------------------------------------------------------------- data
def get_nifty500_symbols():
    path = os.path.join(CACHE_DIR, "nifty500.csv")
    if not os.path.exists(path):
        resp = requests.get(NIFTY500_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        resp.raise_for_status()
        with open(path, "w") as f:
            f.write(resp.text)
    df = pd.read_csv(path)
    return [s.strip() + ".NS" for s in df["Symbol"]]


def download_daily(tickers, start):
    path = os.path.join(CACHE_DIR, f"daily_{start}.pkl")
    if os.path.exists(path):
        return pd.read_pickle(path)
    frames = {}
    for i in range(0, len(tickers), 50):
        chunk = tickers[i:i + 50]
        for attempt in range(4):
            try:
                raw = yf.download(chunk, start=start, interval="1d", auto_adjust=True,
                                  group_by="ticker", threads=True, progress=False)
                break
            except Exception as e:  # rate limit / network
                print(f"  retry {attempt + 1} for chunk {i}: {e}")
                time.sleep(2 ** (attempt + 1))
        else:
            continue
        for t in chunk:
            if t in raw.columns.get_level_values(0):
                d = raw[t][["Open", "High", "Low", "Close"]].dropna()
                if len(d):
                    frames[t] = d
        print(f"  downloaded {min(i + 50, len(tickers))}/{len(tickers)}")
    pd.to_pickle(frames, path)
    return frames


def to_monthly(daily):
    d = daily.copy()
    d.index = pd.to_datetime(d.index).tz_localize(None)
    d["Days"] = 1
    m = d.resample("ME").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last",
                              "Days": "sum"}).dropna()
    # drop the current, still-incomplete month
    this_month_end = pd.Timestamp(date.today()) + pd.offsets.MonthEnd(0)
    return m[m.index < this_month_end]


# ----------------------------------------------------------------------- backtest
def backtest_symbol(sym, m, tol, entry_mode, exit_mode, cost):
    o, h, lo, c = m["Open"].values, m["High"].values, m["Low"].values, m["Close"].values
    days = m["Days"].values
    ema10 = m["Close"].ewm(span=10, adjust=False).mean().values
    idx = m.index
    n = len(m)
    trades = []
    i = 1
    while i < n:
        signal = c[i] >= h[i] * (1 - tol)
        growth = c[i] / c[i - 1] - 1
        # data-quality filters: a real trading month (>=10 sessions, price moved)
        # and an up month (needed for a growth target; also used for EMA so both
        # exits are tested on the same entries)
        valid = days[i] >= 10 and h[i] > lo[i] and growth > 0
        if not signal or not valid:
            i += 1
            continue

        if entry_mode == "next_open":
            if i + 1 >= n:
                break
            e_idx, e_px, scan_from = i + 1, o[i + 1], i + 1  # entry month can also hit target
        else:
            e_idx, e_px, scan_from = i, c[i], i + 1

        target = e_px * (1 + growth)
        x_idx = x_px = None
        for j in range(scan_from, n):
            if exit_mode == "target":
                if j == e_idx and entry_mode == "next_open":
                    if h[j] >= target:
                        x_idx, x_px = j, target
                        break
                elif o[j] >= target:
                    x_idx, x_px = j, o[j]
                    break
                elif h[j] >= target:
                    x_idx, x_px = j, target
                    break
            else:  # ema
                if j < 10:  # need EMA warm-up
                    continue
                if c[j] < ema10[j]:
                    if entry_mode == "next_open":
                        if j + 1 < n:
                            x_idx, x_px = j + 1, o[j + 1]
                        else:
                            x_idx, x_px = None, None  # exit pending next open
                    else:
                        x_idx, x_px = j, c[j]
                    break

        is_open = x_idx is None
        if is_open:
            x_idx, x_px = n - 1, c[n - 1]
        ret = x_px / e_px - 1 - cost
        trades.append({
            "symbol": sym.replace(".NS", ""),
            "signal_month": idx[i].strftime("%Y-%m"),
            "signal_growth_%": round(growth * 100, 2),
            "entry_month": idx[e_idx].strftime("%Y-%m"),
            "entry_price": round(e_px, 2),
            "target_price": round(target, 2) if exit_mode == "target" else np.nan,
            "exit_month": idx[x_idx].strftime("%Y-%m"),
            "exit_price": round(x_px, 2),
            "months_held": x_idx - e_idx,
            "return_%": round(ret * 100, 2),
            "status": "OPEN" if is_open else "CLOSED",
        })
        if is_open:
            break
        # next signal can only come from a month after the exit
        i = x_idx + 1 if x_idx > i else i + 1
    return trades


def summarize(df, label):
    print("\n" + "=" * 72)
    print(f"  {label}")
    print("=" * 72)
    if df.empty:
        print("  no trades")
        return
    closed = df[df.status == "CLOSED"]
    opn = df[df.status == "OPEN"]
    r = closed["return_%"]
    wins, losses = r[r > 0], r[r <= 0]
    pf = wins.sum() / abs(losses.sum()) if len(losses) and losses.sum() != 0 else np.inf
    yrs = closed["months_held"].replace(0, 0.5) / 12
    ann = ((1 + r / 100) ** (1 / yrs) - 1) * 100
    print(f"  Total trades           : {len(df)}  (closed {len(closed)}, open {len(opn)})")
    print(f"  Stocks traded          : {df.symbol.nunique()}")
    print(f"  Win rate (closed)      : {len(wins) / max(len(closed), 1) * 100:.1f}%")
    print(f"  Avg return / trade     : {r.mean():.2f}%")
    print(f"  Median return / trade  : {r.median():.2f}%")
    print(f"  Avg win / avg loss     : {wins.mean():.2f}% / {losses.mean() if len(losses) else 0:.2f}%")
    print(f"  Best / worst trade     : {r.max():.2f}% / {r.min():.2f}%")
    print(f"  Profit factor          : {pf:.2f}")
    print(f"  Avg / median hold (mo) : {closed.months_held.mean():.1f} / {closed.months_held.median():.0f}")
    print(f"  Median annualised ret  : {ann.median():.1f}%  (per closed trade)")
    if len(opn):
        print(f"  Open trades (MTM)      : avg {opn['return_%'].mean():.2f}%, "
              f"{(opn['return_%'] < 0).sum()} under water, avg age {opn.months_held.mean():.1f} mo")
    by_year = closed.assign(year=closed.entry_month.str[:4]).groupby("year")["return_%"]
    tbl = pd.DataFrame({"trades": by_year.size(), "win%": by_year.apply(lambda s: (s > 0).mean() * 100),
                        "avg%": by_year.mean()}).round(1)
    print("\n  By entry year (closed trades):")
    print("  " + tbl.to_string().replace("\n", "\n  "))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2005-01-01")
    ap.add_argument("--tol", type=float, default=0.0,
                    help="%% tolerance: close >= high*(1-tol). 0 = exact (default)")
    ap.add_argument("--entry", choices=["next_open", "close"], default="next_open")
    ap.add_argument("--cost", type=float, default=0.3, help="round-trip cost in %%")
    args = ap.parse_args()

    os.makedirs(CACHE_DIR, exist_ok=True)
    os.makedirs(RESULTS_DIR, exist_ok=True)
    tol = args.tol / 100 if args.tol > 0 else 1e-9  # tiny epsilon for float rounding
    cost = args.cost / 100

    syms = get_nifty500_symbols()
    print(f"NIFTY 500 symbols: {len(syms)}; downloading daily data from {args.start} ...")
    daily = download_daily(syms, args.start)
    monthly = {s: to_monthly(d) for s, d in daily.items()}
    monthly = {s: m for s, m in monthly.items() if len(m) >= 12}
    print(f"Stocks with >=12 months of data: {len(monthly)}")

    for exit_mode, label in [("target", "EXIT 1: TARGET = SIGNAL-MONTH GROWTH"),
                             ("ema", "EXIT 2: MONTHLY CLOSE BELOW 10 EMA")]:
        trades = []
        for s, m in monthly.items():
            trades += backtest_symbol(s, m, tol, args.entry, exit_mode, cost)
        df = pd.DataFrame(trades)
        out = os.path.join(RESULTS_DIR, f"trades_{exit_mode}.csv")
        df.to_csv(out, index=False)
        summarize(df, f"{label}  (entry={args.entry}, tol={args.tol}%, cost={args.cost}%)")
        print(f"\n  trade list -> {out}")


if __name__ == "__main__":
    main()
