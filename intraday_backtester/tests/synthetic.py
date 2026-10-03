"""Synthetic NSE-like 1-minute data (pure random walk => no real edge exists).

Used by the tests and by `python check_lookahead.py --synthetic`: on a random walk every
strategy should lose roughly its transaction costs; a consistent profit means a bug or
look-ahead bias.
"""

from __future__ import annotations

import os
from typing import List, Optional

import numpy as np
import pandas as pd


def trading_days(start: str, n_days: int) -> List[pd.Timestamp]:
    d = pd.bdate_range(start, periods=int(n_days * 1.1) + 10)
    return list(d[:n_days])


def make_symbol(seed: int, days: List[pd.Timestamp], price: float = 1000.0, daily_vol: float = 0.018,
                base_volume: float = 20000, drop_minutes: float = 0.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    minutes = np.arange(375)
    # U-shaped intraday volatility and volume
    u = 1.0 + 1.5 * np.exp(-minutes / 25) + 0.6 * np.exp(-(374 - minutes) / 30)
    sig = daily_vol / np.sqrt((u ** 2).sum()) * u
    frames = []
    p = price
    for d in days:
        p *= np.exp(rng.normal(0, daily_vol * 0.5))           # overnight gap
        r = rng.normal(0, sig)
        close = p * np.exp(np.cumsum(r))
        open_ = np.r_[p, close[:-1]]
        wig = np.abs(rng.normal(0, sig * 0.6)) * close
        high = np.maximum(open_, close) + wig
        low = np.minimum(open_, close) - np.abs(rng.normal(0, sig * 0.6)) * close
        vol = rng.gamma(2.0, base_volume * u / 2.0)
        ts = d + pd.Timedelta(hours=9, minutes=15) + pd.to_timedelta(minutes, unit="m")
        f = pd.DataFrame({"ts": ts, "open": open_, "high": high, "low": low, "close": close, "volume": vol.round()})
        if drop_minutes > 0:
            f = f[rng.random(len(f)) > drop_minutes]
        frames.append(f)
        p = close[-1]
    return pd.concat(frames, ignore_index=True)


def write_dataset(folder: str, n_symbols: int = 8, n_days: int = 160, start: str = "2023-01-02",
                  fmt: str = "standard", seed: int = 1) -> List[str]:
    """Write CSVs in one of several vendor-like formats."""
    os.makedirs(folder, exist_ok=True)
    days = trading_days(start, n_days)
    names = [f"SYN{k:02d}" for k in range(n_symbols)]
    paths = []
    allf = []
    for k, s in enumerate(names):
        df = make_symbol(seed * 100 + k, days, price=200 + 150 * k)
        if fmt == "standard":       # one file per symbol, ISO timestamps
            out = df.rename(columns={"ts": "datetime"})
            p = os.path.join(folder, f"{s}.csv")
            out.to_csv(p, index=False)
        elif fmt == "kite":         # Date with +05:30, Title case, end-stamped minute
            out = pd.DataFrame({"Date": (df["ts"] + pd.Timedelta(minutes=1)).dt.strftime("%Y-%m-%d %H:%M:%S+05:30"),
                                "Open": df["open"], "High": df["high"], "Low": df["low"], "Close": df["close"],
                                "Volume": df["volume"]})
            p = os.path.join(folder, f"NSE_{s}_1min.csv")
            out.to_csv(p, index=False)
        elif fmt == "split":        # separate date (dd-mm-yyyy) and time columns, semicolon
            out = pd.DataFrame({"date": df["ts"].dt.strftime("%d-%m-%Y"), "time": df["ts"].dt.strftime("%H:%M"),
                                "open": df["open"], "high": df["high"], "low": df["low"], "close": df["close"],
                                "vol": df["volume"]})
            p = os.path.join(folder, f"{s}-EQ.csv")
            out.to_csv(p, index=False, sep=";")
        elif fmt == "multi":        # all symbols in one file with a Symbol column
            allf.append(df.assign(Symbol=s))
            continue
        paths.append(p)
    if fmt == "multi":
        out = pd.concat(allf).rename(columns={"ts": "Timestamp"})
        p = os.path.join(folder, "nifty500_all.csv")
        out.to_csv(p, index=False)
        paths.append(p)
    return paths
