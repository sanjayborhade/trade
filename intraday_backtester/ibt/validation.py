"""Look-ahead bias detector ("future perturbation test").

Idea: if the system only uses past information, then changing everything AFTER time T
must not change any decision made at or before T. For several random cut-off times we
scramble all candles after the cut (prices, highs/lows, volume - of the stock AND the
benchmark), re-run the strategy, and require that every trade entered at or before the
cut is bit-for-bit identical (signal time, entry time, side, entry price, stop, target).
Trades that also exited before the cut must have identical exits.

Any difference means some calculation peeked into the future.
"""

from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from .engine.simulator import Simulator
from .features import Frame, eligibility
from .strategies import build

KEY_ENTRY = ["signal_ts", "entry_ts", "side", "entry_raw", "stop", "target"]
KEY_EXIT = ["exit_ts", "exit_raw", "exit_reason"]


def _scramble(df: pd.DataFrame, cut: pd.Timestamp, rng: np.random.Generator) -> pd.DataFrame:
    out = df.copy()
    m = (out["ts"] > cut).to_numpy()
    k = int(m.sum())
    if k == 0:
        return out
    f = np.exp(np.cumsum(rng.normal(0, 0.004, k)))   # a completely different future path
    for c in ("open", "high", "low", "close"):
        out.loc[m, c] = out.loc[m, c].to_numpy() * f * rng.uniform(0.97, 1.03)
    o, c = out.loc[m, "open"].to_numpy(), out.loc[m, "close"].to_numpy()
    out.loc[m, "high"] = np.maximum(o, c) * (1 + rng.uniform(0, 0.004, k))
    out.loc[m, "low"] = np.minimum(o, c) * (1 - rng.uniform(0, 0.004, k))
    if "volume" in out:
        out.loc[m, "volume"] = rng.uniform(0, 5, k) * (out["volume"].mean() + 1)
    return out


def _eq(a, b):
    if isinstance(a, float) or isinstance(b, float):
        return (pd.isna(a) and pd.isna(b)) or a == b
    return a == b


def perturbation_test(symbol: str, bars: pd.DataFrame, strategy_names: List[str], cfg: dict,
                      bench: Optional[Dict[str, pd.DataFrame]] = None, n_cuts: int = 6, seed: int = 0,
                      overrides: Optional[Dict[str, dict]] = None) -> List[str]:
    rng = np.random.default_rng(seed)
    sim = Simulator(cfg)
    f0 = Frame(symbol, bars, bench)
    ok0 = eligibility(f0, cfg, "all")
    base = {}
    for n in strategy_names:
        base[n], _ = sim.run(f0, build(n, cfg, (overrides or {}).get(n)), ok0)
    days = f0.dates[min(40, f0.n_days - 2):]
    failures: List[str] = []
    for k in range(n_cuts):
        d = days[int(rng.integers(0, len(days)))]
        minute = int(rng.integers(5, 360))
        cut = pd.Timestamp(d) + pd.Timedelta(minutes=555 + minute)
        pb = _scramble(bars, cut, rng)
        pbench = {nm: _scramble(b, cut, rng) for nm, b in (bench or {}).items()}
        f1 = Frame(symbol, pb, pbench or None)
        ok1 = eligibility(f1, cfg, "all")
        if not np.array_equal(ok0[: f1.day[np.searchsorted(f1.ts, np.datetime64(cut))]],
                              ok1[: f1.day[np.searchsorted(f1.ts, np.datetime64(cut))]]):
            failures.append(f"cut {cut}: day eligibility before the cut changed")
        upto = int(np.searchsorted(f1.ts, np.datetime64(cut), side="right"))   # candles with ts <= cut
        for n in strategy_names:
            # (1) every signal / order level / cancel level / score on candles up to the cut must be identical
            s0 = build(n, cfg, (overrides or {}).get(n)).generate_signal(f0)
            s1 = build(n, cfg, (overrides or {}).get(n)).generate_signal(f1)
            for fld in ("long", "short", "level_long", "level_short", "cancel_long", "cancel_short", "score",
                        "exit_long", "exit_short"):
                a0, a1 = getattr(s0, fld), getattr(s1, fld)
                if a0 is None and a1 is None:
                    continue
                if not np.array_equal(np.asarray(a0)[:upto], np.asarray(a1)[:upto], equal_nan=True) \
                        if np.asarray(a0).dtype.kind == "f" else \
                        not np.array_equal(np.asarray(a0)[:upto], np.asarray(a1)[:upto]):
                    bad = np.flatnonzero(~((np.asarray(a0)[:upto] == np.asarray(a1)[:upto]) |
                                           (pd.isna(np.asarray(a0)[:upto]) & pd.isna(np.asarray(a1)[:upto]))))
                    failures.append(f"{n} cut {cut}: signal field '{fld}' before the cut changed "
                                    f"(first at {pd.Timestamp(f0.ts[bad[0]]) if len(bad) else '?'})")
            # (2) trades entered at or before the cut must be identical
            t1, _ = sim.run(f1, build(n, cfg, (overrides or {}).get(n)), ok1)
            a = _before(base[n], cut)
            b = _before(t1, cut)
            if len(a) != len(b):
                failures.append(f"{n} cut {cut}: {len(a)} trades entered before the cut originally, {len(b)} after "
                                f"changing the future")
                continue
            for (_, ra), (_, rb) in zip(a.iterrows(), b.iterrows()):
                for col in KEY_ENTRY:
                    if not _eq(ra[col], rb[col]):
                        failures.append(f"{n} cut {cut}: trade {ra['entry_ts']} field {col} changed "
                                        f"{ra[col]} -> {rb[col]}")
                        break
                if pd.Timestamp(ra["exit_ts"]) <= cut:
                    for col in KEY_EXIT:
                        if not _eq(ra[col], rb[col]):
                            failures.append(f"{n} cut {cut}: exit of trade {ra['entry_ts']} field {col} changed")
                            break
    return failures


def _before(df: pd.DataFrame, cut: pd.Timestamp) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=KEY_ENTRY + KEY_EXIT)
    return df[pd.to_datetime(df["entry_ts"]) <= cut].sort_values("entry_ts").reset_index(drop=True)
