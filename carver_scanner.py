"""
Carver EWMAC Forecast scanner for the Nifty 500.

A Python port of the "Carver EWMAC Forecast" TradingView indicator (Pine v6).
It scans every Nifty 500 stock and lists those where the combined forecast
(the green line) has reached the +20 "Max Long" cap.

Usage examples
--------------
    # Monthly chart (as in the TradingView screenshot), stocks at +20 right now
    python carver_scanner.py

    # Only stocks that touched +20 on the latest bar after being below it
    python carver_scanner.py --mode cross

    # Stocks that crossed up to +20 at any point in the last 3 bars
    python carver_scanner.py --mode cross --lookback 3

    # Weekly / daily charts, only closed bars, save results
    python carver_scanner.py --timeframe W --closed-only --out hits.csv

    # Quick test on a few symbols
    python carver_scanner.py --symbols AEGISLOG RELIANCE TCS
"""

from __future__ import annotations

import argparse
import io
import math
import sys
import time
from dataclasses import dataclass

import numpy as np
import pandas as pd
import requests
import yfinance as yf

NIFTY500_URL = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"

# (fast, slow, published Carver forecast scalar)
SPEEDS = [
    (4, 16, 8.53),
    (8, 32, 5.95),
    (16, 64, 4.10),
    (32, 128, 2.79),
    (64, 256, 1.91),
]

# Per-timeframe settings, identical to the Pine "Auto-adjust" mode.
TIMEFRAMES = {
    "D": {"rule": None, "ann_factor": 16.0, "long_window": 500},
    "W": {"rule": "W-FRI", "ann_factor": math.sqrt(52), "long_window": 150},
    "M": {"rule": "ME", "ann_factor": math.sqrt(12), "long_window": 60},
}


@dataclass
class Params:
    vol_span: int = 35
    blend_weight: float = 0.3
    f_cap: float = 20.0
    div_mult: float = 1.35
    # Pine's expanding-mean fallback never actually activates (cumVol starts
    # as na and stays na), so TradingView shows nothing until the long-run
    # SMA window fills. Keep False to match the chart exactly.
    expanding_fallback: bool = False


# ---------------------------------------------------------------------------
#  Indicator maths (faithful to Pine semantics)
# ---------------------------------------------------------------------------
def pine_ema(src: pd.Series, length: int) -> pd.Series:
    """ta.ema: seeded with the SMA of the first `length` values, na before that."""
    values = src.to_numpy(dtype=float)
    out = np.full(len(values), np.nan)
    if len(values) < length:
        return pd.Series(out, index=src.index)
    alpha = 2.0 / (length + 1)
    prev = values[:length].mean()
    out[length - 1] = prev
    for i in range(length, len(values)):
        prev = alpha * values[i] + (1 - alpha) * prev
        out[i] = prev
    return pd.Series(out, index=src.index)


def carver_forecast(close: pd.Series, timeframe: str = "M", p: Params = Params()) -> pd.DataFrame:
    """Return a DataFrame with the combined (capped) forecast and per-speed forecasts."""
    tf = TIMEFRAMES[timeframe]
    close = close.astype(float)

    # 1. Blended volatility (price units)
    diff2 = close.diff() ** 2
    ew_var = diff2.ewm(alpha=2.0 / (p.vol_span + 1), adjust=False).mean()
    recent_vol = np.sqrt(ew_var)

    long_avg = recent_vol.rolling(tf["long_window"]).mean()
    if p.expanding_fallback:
        long_avg = long_avg.fillna(recent_vol.expanding().mean())

    daily_vol = p.blend_weight * long_avg + (1 - p.blend_weight) * recent_vol
    daily_vol = daily_vol.replace(0, np.nan)
    ann_vol_pct = daily_vol * tf["ann_factor"] / close * 100

    # 2. Per-speed forecasts
    out = pd.DataFrame(index=close.index)
    uncapped = []
    for fast, slow, scalar in SPEEDS:
        raw = (pine_ema(close, fast) - pine_ema(close, slow)) / daily_vol
        out[f"ewmac_{fast}_{slow}"] = (raw * scalar).clip(-p.f_cap, p.f_cap)
        uncapped.append(raw * scalar)

    # 3. Combine: equal weight over speeds that have a value, times FDM, capped
    fc = out[[f"ewmac_{f}_{s}" for f, s, _ in SPEEDS]]
    out["n_speeds"] = fc.notna().sum(axis=1)
    out["combined"] = (fc.mean(axis=1, skipna=True) * p.div_mult).clip(-p.f_cap, p.f_cap)
    # trend strength without any cap: tells apart stocks that all sit at +20
    out["strength"] = pd.concat(uncapped, axis=1).mean(axis=1, skipna=True) * p.div_mult
    out["ann_vol_pct"] = ann_vol_pct
    return out


# ---------------------------------------------------------------------------
#  Data
# ---------------------------------------------------------------------------
def load_nifty500(path: str | None = None) -> pd.DataFrame:
    if path:
        df = pd.read_csv(path)
    else:
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "text/csv"}
        resp = requests.get(NIFTY500_URL, headers=headers, timeout=30)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))
    df.columns = [c.strip() for c in df.columns]
    return df[["Symbol", "Company Name"]].dropna()


def resample_close(daily_close: pd.Series, timeframe: str) -> pd.Series:
    rule = TIMEFRAMES[timeframe]["rule"]
    s = daily_close.dropna()
    if rule is None:
        return s
    return s.resample(rule).last().dropna()


def download_daily(symbols: list[str], chunk: int = 50, retries: int = 3) -> dict[str, pd.Series]:
    """Download max-history daily closes (split-adjusted, not dividend-adjusted,
    like TradingView's default) for NSE symbols. Returns {symbol: close series}."""
    result: dict[str, pd.Series] = {}
    for start in range(0, len(symbols), chunk):
        batch = symbols[start:start + chunk]
        tickers = [f"{s}.NS" for s in batch]
        for attempt in range(retries):
            try:
                data = yf.download(
                    tickers, period="max", interval="1d", auto_adjust=False,
                    group_by="ticker", threads=True, progress=False,
                )
                break
            except Exception as exc:  # network / rate limit
                wait = 5 * (attempt + 1)
                print(f"  download error ({exc}); retrying in {wait}s", file=sys.stderr)
                time.sleep(wait)
        else:
            continue
        for sym, tic in zip(batch, tickers):
            try:
                close = data[tic]["Close"] if isinstance(data.columns, pd.MultiIndex) else data["Close"]
            except KeyError:
                continue
            close = close.dropna()
            if not close.empty:
                close.index = pd.to_datetime(close.index).tz_localize(None)
                result[sym] = close
        print(f"  downloaded {min(start + chunk, len(symbols))}/{len(symbols)}", file=sys.stderr)
    return result


# ---------------------------------------------------------------------------
#  Scanner
# ---------------------------------------------------------------------------
def scan(closes: dict[str, pd.Series], names: dict[str, str], timeframe: str, mode: str,
         threshold: float, lookback: int, closed_only: bool, p: Params) -> pd.DataFrame:
    rows = []
    for sym, daily in closes.items():
        close = resample_close(daily, timeframe)
        if closed_only and len(close) > 0:
            close = close.iloc[:-1]  # drop the still-forming bar
        if len(close) < 3:
            continue
        fc = carver_forecast(close, timeframe, p)
        comb = fc["combined"]
        if comb.dropna().empty:
            continue

        at_top = comb >= threshold - 1e-9
        crossed = at_top & ~(comb.shift(1) >= threshold - 1e-9)
        window = crossed if mode == "cross" else at_top
        recent = window.iloc[-lookback:]
        if not recent.any():
            continue

        last = fc.iloc[-1]
        signal_date = recent[recent].index[-1]
        rows.append({
            "Symbol": sym,
            "Company": names.get(sym, ""),
            "Close": round(float(close.iloc[-1]), 2),
            "Forecast": round(float(last["combined"]), 2),
            "PrevForecast": round(float(comb.iloc[-2]), 2) if pd.notna(comb.iloc[-2]) else None,
            "FreshCross": bool(crossed.iloc[-1]),
            "SignalBar": signal_date.date(),
            "ActiveSpeeds": f"{int(last['n_speeds'])}/5",
            "AnnVol%": round(float(last["ann_vol_pct"]), 2),
            "BarDate": close.index[-1].date(),
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["FreshCross", "AnnVol%"], ascending=[False, True]).reset_index(drop=True)
    return df


def main() -> None:
    ap = argparse.ArgumentParser(description="Scan Nifty 500 for Carver EWMAC forecast at +20")
    ap.add_argument("--timeframe", "-t", choices=list(TIMEFRAMES), default="M",
                    help="D=daily, W=weekly, M=monthly (default: M, as in the screenshot)")
    ap.add_argument("--mode", choices=["touch", "cross"], default="touch",
                    help="touch: forecast is at +20 now; cross: it just moved up to +20 from below")
    ap.add_argument("--threshold", type=float, default=20.0, help="forecast level to scan for (default 20)")
    ap.add_argument("--lookback", type=int, default=1,
                    help="match if the condition happened in any of the last N bars (default 1)")
    ap.add_argument("--closed-only", action="store_true",
                    help="ignore the still-forming bar (the Pine notes say only closed bars are actionable)")
    ap.add_argument("--symbols", nargs="+", help="scan only these NSE symbols instead of the Nifty 500")
    ap.add_argument("--list-file", help="local copy of ind_nifty500list.csv (if the NSE download fails)")
    ap.add_argument("--expanding-fallback", action="store_true",
                    help="use an expanding-mean vol before the long-run window fills "
                         "(gives values for newly listed stocks; differs from TradingView)")
    ap.add_argument("--vol-span", type=int, default=35)
    ap.add_argument("--blend-weight", type=float, default=0.3)
    ap.add_argument("--div-mult", type=float, default=1.35)
    ap.add_argument("--out", help="save results to this CSV file")
    args = ap.parse_args()

    p = Params(vol_span=args.vol_span, blend_weight=args.blend_weight, div_mult=args.div_mult,
               f_cap=20.0, expanding_fallback=args.expanding_fallback)

    if args.symbols:
        universe = pd.DataFrame({"Symbol": [s.upper() for s in args.symbols], "Company Name": ""})
    else:
        print("Fetching Nifty 500 constituents...", file=sys.stderr)
        universe = load_nifty500(args.list_file)
    names = dict(zip(universe["Symbol"], universe["Company Name"]))

    print(f"Downloading price history for {len(universe)} symbols...", file=sys.stderr)
    closes = download_daily(list(universe["Symbol"]))
    missing = sorted(set(universe["Symbol"]) - set(closes))
    if missing:
        print(f"No data for {len(missing)} symbols: {', '.join(missing[:20])}"
              f"{' ...' if len(missing) > 20 else ''}", file=sys.stderr)

    hits = scan(closes, names, args.timeframe, args.mode, args.threshold,
                max(1, args.lookback), args.closed_only, p)

    print()
    label = {"D": "Daily", "W": "Weekly", "M": "Monthly"}[args.timeframe]
    if hits.empty:
        print(f"No stocks match ({label}, mode={args.mode}, threshold={args.threshold}).")
        return
    print(f"{len(hits)} stocks with Carver forecast {'crossing up to' if args.mode == 'cross' else 'at'} "
          f"+{args.threshold:g} ({label}):\n")
    with pd.option_context("display.max_rows", None, "display.width", 200):
        print(hits.to_string(index=False))
    if args.out:
        hits.to_csv(args.out, index=False)
        print(f"\nSaved to {args.out}")


if __name__ == "__main__":
    main()
