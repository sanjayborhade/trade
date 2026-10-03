"""Development / validation / out-of-sample periods and walk-forward windows,
always derived from the dates that actually exist in your data."""

from __future__ import annotations

from datetime import date
from typing import Dict, List, Tuple

import pandas as pd

Period = Tuple[date, date]


def compute_periods(cfg, calendar: List[date]) -> Dict[str, Period]:
    if not calendar:
        raise ValueError("Empty trading calendar - no data in the cache?")
    cal = sorted(calendar)
    p = cfg["periods"]
    out: Dict[str, Period] = {}
    if p["mode"] == "fractions":
        n = len(cal)
        a, b, _ = p["fractions"]
        i1 = max(1, int(round(n * a)))
        i2 = max(i1 + 1, int(round(n * (a + b))))
        i2 = min(i2, n - 1)
        out["development"] = (cal[0], cal[i1 - 1])
        out["validation"] = (cal[i1], cal[i2 - 1])
        out["test"] = (cal[i2], cal[-1])
    else:
        for name in ("development", "validation", "test"):
            s, e = (date.fromisoformat(str(x)) for x in p["dates"][name])
            days = [d for d in cal if s <= d <= e]
            if not days:
                raise ValueError(f"periods.dates.{name} ({s} to {e}) contains no trading days in your data "
                                 f"({cal[0]} to {cal[-1]}).")
            out[name] = (days[0], days[-1])
    return out


def walk_forward_windows(cfg, calendar: List[date]) -> List[Tuple[Period, Period]]:
    w = cfg["walk_forward"]
    cal = pd.to_datetime(pd.Series(sorted(calendar)))
    start, last = cal.iloc[0], cal.iloc[-1]
    tr, te, st = int(w["train_months"]), int(w["test_months"]), int(w["step_months"])
    out = []
    t0 = start
    while True:
        train_end = t0 + pd.DateOffset(months=tr) - pd.Timedelta(days=1)
        test_start = train_end + pd.Timedelta(days=1)
        test_end = test_start + pd.DateOffset(months=te) - pd.Timedelta(days=1)
        if test_start > last:
            break
        trd = cal[(cal >= t0) & (cal <= train_end)]
        ted = cal[(cal >= test_start) & (cal <= test_end)]
        if len(trd) > 20 and len(ted) > 5:
            out.append(((trd.iloc[0].date(), trd.iloc[-1].date()), (ted.iloc[0].date(), ted.iloc[-1].date())))
        t0 = t0 + pd.DateOffset(months=st)
    return out
