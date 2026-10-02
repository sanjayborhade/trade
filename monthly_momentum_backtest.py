"""
Monthly breakout backtest on the NIFTY 500 universe.

ENTRY  : a month closes up more than GROWTH (30% or 50%) vs the previous month's close,
         AND each of the previous LOOKBACK months rose less than 10%.
         Buy at that month's close.
EXIT A : target = same % as the signal month's gain (e.g. +34% month -> +34% target).
         Checked against later monthly highs; if a month opens above target we fill at the open.
EXIT B : first monthly close below the 10-period EMA of monthly closes -> sell at that close.

One position per stock at a time (new signals are ignored while a trade is open).
Trades still open at the end are marked to the last completed monthly close.

Usage:
    pip install yfinance pandas
    python monthly_momentum_backtest.py                 # defaults
    python monthly_momentum_backtest.py --lookback 12   # require 12 quiet months
"""
import argparse
import os

import pandas as pd
import yfinance as yf

NIFTY500_URL = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
CACHE = "data_monthly.pkl"


def load_symbols():
    import io
    import urllib.request

    req = urllib.request.Request(NIFTY500_URL, headers={"User-Agent": "Mozilla/5.0"})
    df = pd.read_csv(io.StringIO(urllib.request.urlopen(req).read().decode()))
    return [s + ".NS" for s in df["Symbol"]]


def load_data(symbols):
    if os.path.exists(CACHE):
        return pd.read_pickle(CACHE)
    raw = yf.download(symbols, period="max", interval="1mo", auto_adjust=True,
                      group_by="ticker", progress=False, threads=True)
    data = {}
    for s in symbols:
        if s in raw.columns.get_level_values(0):
            d = raw[s][["Open", "High", "Low", "Close"]].dropna()
            if len(d) > 12:
                data[s] = d
    pd.to_pickle(data, CACHE)
    return data


def backtest_stock(sym, d, growth, lookback, small, exit_mode, max_gain):
    # drop the current, incomplete month
    today = pd.Timestamp.today()
    d = d[~((d.index.year == today.year) & (d.index.month == today.month))]
    c = d["Close"]
    ret = c.pct_change()
    ema = c.ewm(span=10, adjust=False).mean()
    quiet = (ret.shift(1).rolling(lookback).max() < small)
    # gains above max_gain are almost always bad split adjustments in Yahoo data
    signal = (ret > growth) & (ret <= max_gain) & quiet

    trades, i, n = [], 0, len(d)
    idx = d.index
    while i < n:
        if not signal.iloc[i]:
            i += 1
            continue
        entry_px, g = c.iloc[i], ret.iloc[i]
        target = entry_px * (1 + g)
        exit_px = exit_dt = None
        reason = "open"
        j = i + 1
        while j < n:
            if exit_mode == "target":
                if d["Open"].iloc[j] >= target:
                    exit_px, reason = d["Open"].iloc[j], "target(gap)"
                elif d["High"].iloc[j] >= target:
                    exit_px, reason = target, "target"
            else:  # ema
                if c.iloc[j] < ema.iloc[j]:
                    exit_px, reason = c.iloc[j], "close<EMA10"
            if exit_px is not None:
                exit_dt = idx[j]
                break
            j += 1
        if exit_px is None:  # still open -> mark to market
            j = n - 1
            exit_px, exit_dt = c.iloc[j], idx[j]
        trades.append(dict(symbol=sym.replace(".NS", ""), signal_month=idx[i].strftime("%Y-%m"),
                           signal_gain=round(g * 100, 1), entry=round(entry_px, 2),
                           exit_month=exit_dt.strftime("%Y-%m"), exit=round(exit_px, 2),
                           months_held=j - i, pnl_pct=round((exit_px / entry_px - 1) * 100, 2),
                           reason=reason))
        i = j + 1 if reason != "open" else n
    return trades


def summarize(t):
    if t.empty:
        return dict(trades=0)
    closed = t[t.reason != "open"]
    return dict(
        trades=len(t),
        closed=len(closed),
        still_open=int((t.reason == "open").sum()),
        win_rate=round((t.pnl_pct > 0).mean() * 100, 1),
        avg_pnl=round(t.pnl_pct.mean(), 2),
        median_pnl=round(t.pnl_pct.median(), 2),
        avg_win=round(t[t.pnl_pct > 0].pnl_pct.mean(), 2),
        avg_loss=round(t[t.pnl_pct <= 0].pnl_pct.mean(), 2) if (t.pnl_pct <= 0).any() else 0.0,
        worst=round(t.pnl_pct.min(), 2),
        avg_months=round(t.months_held.mean(), 1),
        median_months=t.months_held.median(),
        avg_pnl_per_month=round((t.pnl_pct / t.months_held.clip(lower=1)).mean(), 2),
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookback", type=int, default=6, help="quiet months before signal")
    ap.add_argument("--small", type=float, default=0.10, help="max monthly gain in quiet period")
    ap.add_argument("--max-gain", type=float, default=1.0,
                    help="skip signal months above this gain (Yahoo split glitches)")
    ap.add_argument("--start", default="2005-01-01", help="ignore signals before this date")
    args = ap.parse_args()

    data = load_data(load_symbols())
    print(f"Loaded {len(data)} stocks | lookback={args.lookback} months < {args.small:.0%}\n")

    rows, all_trades = [], []
    for growth in (0.30, 0.50):
        for mode in ("target", "ema"):
            trades = []
            for s, d in data.items():
                trades += backtest_stock(s, d, growth, args.lookback, args.small, mode,
                                         args.max_gain)
            t = pd.DataFrame(trades)
            if not t.empty:
                t = t[t.signal_month >= args.start[:7]]
                t.insert(0, "scenario", f">{growth:.0%} / {mode}")
                all_trades.append(t)
            rows.append({"entry": f">{growth:.0%}",
                         "exit": "target=same %" if mode == "target" else "close<10EMA",
                         **summarize(t)})

    print(pd.DataFrame(rows).to_string(index=False))
    out = pd.concat(all_trades)
    out.to_csv(f"trades_lookback{args.lookback}.csv", index=False)
    print(f"\nAll trades written to trades_lookback{args.lookback}.csv")


if __name__ == "__main__":
    main()
