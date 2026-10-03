"""Market-regime labels for every trade, using only information available at entry.

  market_trend : bull / bear / sideways  - benchmark close vs 20/50-day SMAs as of YESTERDAY
  volatility   : high / low              - 20-day realised vol vs its 250-day median, as of YESTERDAY
  gap          : gap_up / gap_down / flat - the stock's own opening gap (known at 09:15)
  expiry       : expiry / normal         - Nifty weekly expiry weekday rules (config) + holidays
  volume       : high / normal / low      - stock's cumulative volume vs normal for that minute
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import List

import numpy as np
import pandas as pd

from ..data.store import load_benchmark


def regime_benchmark_name(cfg) -> str:
    choice = cfg["benchmarks"].get("regime_benchmark", "auto")
    if choice == "auto":
        for name in ("NIFTY50", "UNIVERSE_EW"):
            if load_benchmark(cfg, name) is not None:
                return name
        return ""
    return {"nifty50": "NIFTY50", "nifty500": "NIFTY500", "universe": "UNIVERSE_EW"}.get(choice, choice)


def market_regimes(cfg, calendar: List[date]) -> pd.DataFrame:
    r = cfg["regimes"]
    idx = pd.Index(sorted(calendar), name="date")
    out = pd.DataFrame(index=idx)
    out["market_trend"] = "unknown"
    out["volatility"] = "unknown"
    name = regime_benchmark_name(cfg)
    b = load_benchmark(cfg, name) if name else None
    if b is not None and len(b):
        d = b.assign(date=b["ts"].dt.date).groupby("date")["close"].last()
        d = d.reindex(idx).ffill()
        f_, s_ = int(r["trend_fast_days"]), int(r["trend_slow_days"])
        sf = d.rolling(f_, min_periods=f_).mean().shift(1)
        ss = d.rolling(s_, min_periods=s_).mean().shift(1)
        c1 = d.shift(1)
        trend = np.where((c1 > ss) & (sf > ss), "bull", np.where((c1 < ss) & (sf < ss), "bear", "sideways"))
        trend = np.where(ss.isna(), "unknown", trend)
        out["market_trend"] = trend
        lr = np.log(d).diff()
        rv = lr.rolling(int(r["vol_lookback_days"]), min_periods=10).std().shift(1)
        med = rv.rolling(int(r["vol_rank_days"]), min_periods=60).median()
        vol = np.where(rv > med, "high", "low")
        out["volatility"] = np.where(med.isna() | rv.isna(), "unknown", vol)
    out["expiry"] = np.where(pd.Index(idx).isin(expiry_days(cfg, list(idx))), "expiry", "normal")
    out.attrs["benchmark"] = name or "none"
    return out


def expiry_days(cfg, calendar: List[date]) -> set:
    rules = sorted(cfg["regimes"]["expiry_rules"], key=lambda x: str(x["from"]))
    rules = [(date.fromisoformat(str(x["from"])), int(x["weekday"])) for x in rules]
    cal = set(calendar)
    res = set()
    if not calendar:
        return res
    d = min(calendar)
    end = max(calendar)
    d = d - timedelta(days=d.weekday())  # Monday of first week
    while d <= end:
        wd = 3
        for start, w in rules:
            if d + timedelta(days=w) >= start:
                wd = w
        target = d + timedelta(days=wd)
        x = target
        while x >= d and x not in cal:     # holiday -> previous trading day of that week
            x -= timedelta(days=1)
        if x >= d and x in cal:
            res.add(x)
        d += timedelta(days=7)
    return res


def classify_trades(trades: pd.DataFrame, regimes: pd.DataFrame, cfg) -> pd.DataFrame:
    if trades.empty:
        return trades
    r = cfg["regimes"]
    t = trades.copy()
    reg = regimes.reindex(pd.Index(t["date"]))
    t["market_trend"] = reg["market_trend"].fillna("unknown").to_numpy()
    t["volatility"] = reg["volatility"].fillna("unknown").to_numpy()
    t["expiry"] = reg["expiry"].fillna("normal").to_numpy()
    g = float(r["gap_threshold_pct"])
    t["gap"] = np.where(t["gap_pct"] >= g, "gap_up", np.where(t["gap_pct"] <= -g, "gap_down", "flat"))
    hv, lv = float(r["high_volume_ratio"]), float(r["low_volume_ratio"])
    rv = t["rvol_cum"]
    t["volume_regime"] = np.where(rv.isna(), "unknown", np.where(rv >= hv, "high", np.where(rv <= lv, "low",
                                                                                            "normal")))
    t["market_regime"] = t["market_trend"] + "/" + t["volatility"] + "-vol"
    return t
