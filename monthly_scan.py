"""
Monthly scanner: MONTHLY CLOSE AT MONTHLY HIGH  (NIFTY 500)

Run it after the last trading day of the month closes (or any day before the
next month's open). It uses the last *completed* month; pass
--include-current to preview the running month (signals can still vanish).

New-entry filters (same rules as monthly_high_close_backtest.py):
    1. monthly close >= monthly high * (1 - tol)      (default tol 0.5%)
    2. monthly close >  previous monthly close          (up month)
    3. at least 10 trading sessions in the month, high > low
Practical filters (not part of the backtest, on by default, set to 0 to drop):
    4. average daily traded value over 20 sessions >= --min-value (Rs crore)
    5. close >= --min-price
Market check (reported, not applied): NIFTY 500 index monthly close vs its
10-month EMA.

For each signal it prints the trade plan: entry at next month's open,
stop-loss = signal month low, the 10-EMA exit level, the Exit 1 target, and
the position size for --capital / --max-pos.

Holdings check (--holdings file.csv with a "symbol" column, optional
"stop_loss"): flags positions whose monthly close is below the 10 EMA
(exit at next open) or that broke their stop-loss.

Self-contained: needs only this file and Python 3.9+.

Usage
    pip install pandas numpy yfinance requests
    python monthly_scan.py
    python monthly_scan.py --tol 1 --min-value 10 --capital 1000000 --max-pos 20
    python monthly_scan.py --holdings my_positions.csv
"""

import argparse
import os
import time
from datetime import date, datetime

import numpy as np
import pandas as pd
import requests
import yfinance as yf

NIFTY500_URL = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
OUT_DIR = "scans"
INDEX_TICKER = "^CRSLDX"  # NIFTY 500 index on Yahoo


def clean_daily(d):
    """Remove Yahoo data errors. NSE stocks move at most ~20% a day, so:
    1. drop one-off spike bars that sit >50% away from the 7-day median;
    2. if a jump of >+100% / <-60% remains (unadjusted split, demerger, bad
       series), keep only the history after the last such jump."""
    px = d[["Open", "High", "Low", "Close"]]
    ratio = px.div(d["Close"].rolling(7, center=True, min_periods=1).median(), axis=0)
    d = d[~((ratio > 1.5) | (ratio < 0.67)).any(axis=1)]
    prev = d["Close"].shift()
    jump = pd.concat([d["Close"] / prev - 1, d["Open"] / prev - 1], axis=1)
    breaks = d.index[(jump.max(axis=1) > 1.0) | (jump.min(axis=1) < -0.6)]
    if len(breaks):
        d = d[d.index >= breaks[-1]]
    return d


def nifty500_symbols():
    """Fresh constituents from NSE; falls back to the cached list."""
    cache = os.path.join("data_cache", "nifty500.csv")
    try:
        resp = requests.get(NIFTY500_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        resp.raise_for_status()
        os.makedirs("data_cache", exist_ok=True)
        with open(cache, "w") as f:
            f.write(resp.text)
    except Exception as e:
        if not os.path.exists(cache):
            raise
        print(f"NSE list download failed ({e}); using cached {cache}")
    return [s.strip() + ".NS" for s in pd.read_csv(cache)["Symbol"]]


def download(tickers):
    frames = {}
    for i in range(0, len(tickers), 50):
        chunk = tickers[i:i + 50]
        for attempt in range(4):
            try:
                raw = yf.download(chunk, period="3y", interval="1d", auto_adjust=True,
                                  group_by="ticker", threads=True, progress=False)
                break
            except Exception as e:
                print(f"  retry {attempt + 1}: {e}")
                time.sleep(2 ** (attempt + 1))
        else:
            continue
        for t in chunk:
            if t in raw.columns.get_level_values(0):
                d = raw[t][["Open", "High", "Low", "Close", "Volume"]].dropna()
                if len(d):
                    d.index = pd.to_datetime(d.index).tz_localize(None)
                    frames[t] = d
    return frames


def monthly_bars(d, include_current):
    d = d.copy()
    if not include_current:
        d = d[d.index < pd.Timestamp(date.today()).replace(day=1)]
    d["Days"] = 1
    m = d.resample("ME").agg({"Open": "first", "High": "max", "Low": "min",
                              "Close": "last", "Days": "sum"}).dropna()
    m = m[m["Days"] > 0]
    m["EMA10"] = m["Close"].ewm(span=10, adjust=False).mean()
    return d, m


def market_check(include_current):
    idx = yf.download(INDEX_TICKER, period="5y", interval="1d", auto_adjust=True, progress=False)
    if idx.empty:
        return "NIFTY 500 index data unavailable"
    idx = idx.droplevel(1, axis=1) if isinstance(idx.columns, pd.MultiIndex) else idx
    idx.index = pd.to_datetime(idx.index).tz_localize(None)
    _, m = monthly_bars(idx[["Open", "High", "Low", "Close"]], include_current)
    last = m.iloc[-1]
    state = "ABOVE" if last.Close > last.EMA10 else "BELOW"
    return (f"NIFTY 500 {m.index[-1]:%b %Y} close {last.Close:,.0f} is {state} its 10-month EMA "
            f"{last.EMA10:,.0f}" + ("" if state == "ABOVE" else
                                    "  -> weak market, consider fewer/smaller new entries"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tol", type=float, default=0.5, help="%% below the high the close may be")
    ap.add_argument("--min-value", type=float, default=5.0,
                    help="min 20-day avg traded value, Rs crore (0 = off)")
    ap.add_argument("--min-price", type=float, default=20.0)
    ap.add_argument("--capital", type=float, default=1_000_000)
    ap.add_argument("--max-pos", type=int, default=20)
    ap.add_argument("--holdings", help="CSV with a 'symbol' column (optional 'stop_loss')")
    ap.add_argument("--include-current", action="store_true",
                    help="use the running month (preview only)")
    args = ap.parse_args()
    tol = args.tol / 100

    syms = nifty500_symbols()
    held = set()
    holdings = None
    if args.holdings:
        holdings = pd.read_csv(args.holdings)
        holdings["symbol"] = holdings["symbol"].str.upper().str.replace(".NS", "", regex=False)
        held = set(holdings["symbol"])
        syms = sorted(set(syms) | {s + ".NS" for s in held})

    print(f"Downloading {len(syms)} stocks ...")
    data = download(syms)
    print(market_check(args.include_current))

    signals, exits, scan_month = [], [], None
    for t, raw in data.items():
        sym = t.replace(".NS", "")
        d, m = monthly_bars(clean_daily(raw), args.include_current)
        if len(m) < 2:
            continue
        cur, prev = m.iloc[-1], m.iloc[-2]
        scan_month = max(scan_month or m.index[-1], m.index[-1])
        value_cr = (d["Close"] * d["Volume"]).tail(20).mean() / 1e7

        if sym in held:
            h = holdings[holdings.symbol == sym].iloc[0]
            sl = h.get("stop_loss", np.nan)
            reason = []
            if cur.Close < cur.EMA10:
                reason.append("monthly close below 10 EMA")
            if pd.notna(sl) and round(cur.Low, 2) < sl:
                reason.append(f"stop-loss {sl} hit")
            exits.append({"symbol": sym, "close": round(cur.Close, 2),
                          "ema10": round(cur.EMA10, 2),
                          "action": "EXIT at next open: " + ", ".join(reason) if reason else "HOLD"})
            continue

        growth = cur.Close / prev.Close - 1
        if not (cur.Close >= cur.High * (1 - tol) and growth > 0
                and cur.Days >= 10 and cur.High > cur.Low):
            continue
        if value_cr < args.min_value or cur.Close < args.min_price:
            continue
        size = args.capital / args.max_pos
        signals.append({
            "symbol": sym,
            "month": f"{m.index[-1]:%Y-%m}",
            "close": round(cur.Close, 2),
            "high": round(cur.High, 2),
            "close_vs_high_%": round((cur.Close / cur.High - 1) * 100, 2),
            "month_gain_%": round(growth * 100, 2),
            "stop_loss": round(cur.Low, 2),
            "risk_%": round((1 - cur.Low / cur.Close) * 100, 1),
            "ema10_exit_level": round(cur.EMA10, 2),
            "exit1_target": round(cur.Close * (1 + growth), 2),
            "avg_value_cr": round(value_cr, 1),
            "qty_at_close": int(size // cur.Close),
        })

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = f"{scan_month:%Y-%m}" if scan_month is not None else datetime.now().strftime("%Y-%m")
    label = "RUNNING month (preview)" if args.include_current else "completed month"
    print(f"\nScan of {stamp} ({label}): tol {args.tol}%, min value Rs {args.min_value} Cr, "
          f"min price {args.min_price}")

    if signals:
        s = pd.DataFrame(signals).sort_values("month_gain_%", ascending=False)
        path = os.path.join(OUT_DIR, f"signals_{stamp}.csv")
        s.to_csv(path, index=False)
        print(f"\nNEW SIGNALS: {len(s)}  (sorted by month gain; buy at next month's open, "
              f"skip if it opens below stop_loss)")
        with pd.option_context("display.width", 200, "display.max_columns", None):
            print(s.to_string(index=False))
        print(f"\nPosition size: Rs {args.capital / args.max_pos:,.0f} each "
              f"(capital / max-pos). Saved -> {path}")
    else:
        print("\nNo new signals.")

    missing = held - {x["symbol"] for x in exits}
    if missing:
        print(f"\nNo data for holdings: {', '.join(sorted(missing))} (check the NSE symbol)")
    if exits:
        e = pd.DataFrame(exits)
        print("\nHOLDINGS CHECK:")
        print(e.to_string(index=False))
        e.to_csv(os.path.join(OUT_DIR, f"holdings_{stamp}.csv"), index=False)


if __name__ == "__main__":
    main()
