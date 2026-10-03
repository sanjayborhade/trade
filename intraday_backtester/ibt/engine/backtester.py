"""Stage-1 orchestration: run strategies over many symbols in parallel.

Each worker process loads ONE symbol's Parquet file at a time, builds its features
once and runs every requested (strategy, parameter set) on it, returning compact
candidate-trade tables. Memory use therefore scales with one stock, not the dataset.
"""

from __future__ import annotations

import os
import traceback
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from ..config import n_workers
from ..data.store import load_bars, load_benchmark, load_manifest, load_membership
from ..features import Frame, eligibility
from ..logutil import get_logger
from ..strategies import REGISTRY, build
from .simulator import Simulator

# (key, strategy_name, params_override)
Spec = Tuple[str, str, dict]

_WORKER_BENCH: Dict[str, Optional[pd.DataFrame]] = {}


def _benches(cfg, need: bool):
    if not need:
        return None
    key = cfg["data"]["CACHE_FOLDER"]
    if key not in _WORKER_BENCH:
        b = {}
        for name in ("NIFTY50", "NIFTY500", "UNIVERSE_EW"):
            df = load_benchmark(cfg, name)
            if df is not None:
                b[name] = df[["ts", "open", "close"]]
        _WORKER_BENCH[key] = b
    return _WORKER_BENCH[key]


def run_symbol(args) -> dict:
    cfg, symbol, specs, universe_mode, ca_dates, membership = args
    out = {"symbol": symbol, "results": {}, "errors": [], "warnings": []}
    try:
        bars = load_bars(cfg, symbol)
        need_bench = any(REGISTRY[name].needs_benchmark for _, name, _ in specs)
        f = Frame(symbol, bars, _benches(cfg, need_bench))
        if not f.has_volume:
            out["warnings"].append(f"{symbol}: no volume data - volume filters cannot pass, VWAP is time-weighted")
        day_ok = eligibility(f, cfg, universe_mode, membership, ca_dates)
        sim = Simulator(cfg)
        for key, name, params in specs:
            try:
                strat = build(name, cfg, params)
                if strat.needs_benchmark and not np.isfinite(f.bench_ret(strat.p.get("benchmark", "auto"))).any():
                    out["warnings"].append(f"{symbol}/{name}: benchmark not available - no signals")
                df, diag = sim.run(f, strat, day_ok)
                out["results"][key] = (df, diag)
            except Exception as e:  # noqa: BLE001 - one strategy failing must not hide others
                out["errors"].append(f"{symbol}/{name}{params or ''}: {type(e).__name__}: {e}\n"
                                     f"{traceback.format_exc(limit=3)}")
        out["eligible_days"] = int(day_ok.sum())
        out["days"] = int(f.n_days)
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"{symbol}: {type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")
    return out


def select_symbols(cfg, symbols_arg: Optional[str] = None, symbols_file: Optional[str] = None,
                   limit: Optional[int] = None) -> List[str]:
    man = load_manifest(cfg)
    avail = sorted(man["symbols"])
    if not avail:
        raise RuntimeError("The cache contains no symbols. Run prepare_data.py and check the quality report.")
    want = None
    if symbols_arg:
        want = [s.strip().upper() for s in symbols_arg.split(",") if s.strip()]
    elif symbols_file:
        if not os.path.exists(symbols_file):
            raise FileNotFoundError(f"--symbols-file not found: {symbols_file}")
        with open(symbols_file) as fh:
            want = [ln.strip().upper() for ln in fh if ln.strip() and not ln.startswith("#")]
    if want is not None:
        missing = [s for s in want if s not in man["symbols"]]
        if missing:
            get_logger().warning(f"{len(missing)} requested symbol(s) not in the data: {missing[:20]}")
        avail = [s for s in want if s in man["symbols"]]
        if not avail:
            raise RuntimeError("None of the requested symbols are in the prepared data.")
    min_days = int(cfg["universe"].get("min_trading_days", 60))
    short = [s for s in avail if man["symbols"][s]["days"] < min_days]
    if short:
        get_logger().info(f"{len(short)} symbol(s) have fewer than {min_days} days of data and will "
                          f"produce no trades (universe.min_trading_days).")
    if limit:
        avail = avail[: int(limit)]
    return avail


def generate_candidates(cfg, symbols: Sequence[str], specs: List[Spec], universe_mode: str,
                        workers: Optional[int] = None, progress: bool = True):
    """Returns ({key: candidates DataFrame}, {key: Counter diagnostics}, errors, warnings)."""
    log = get_logger()
    man = load_manifest(cfg)
    membership = load_membership(cfg)
    workers = workers or n_workers(cfg)
    jobs = []
    for s in symbols:
        ca = man["symbols"].get(s, {}).get("ca_gap_dates", [])
        mem = {s: membership.get(s, [])} if membership is not None else None
        jobs.append((cfg, s, specs, universe_mode, ca, mem))
    parts: Dict[str, List[pd.DataFrame]] = {k: [] for k, _, _ in specs}
    diags: Dict[str, Counter] = {k: Counter() for k, _, _ in specs}
    errors, warnings = [], []
    done = 0

    def collect(r):
        nonlocal done
        done += 1
        errors.extend(r["errors"])
        warnings.extend(r["warnings"])
        for k, (df, dg) in r["results"].items():
            if len(df):
                parts[k].append(df)
            diags[k].update(dg)
        if progress and (done % 25 == 0 or done == len(jobs)):
            log.info(f"  processed {done}/{len(jobs)} symbols")

    if workers <= 1 or len(jobs) <= 1:
        for j in jobs:
            collect(run_symbol(j))
    else:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            for fu in as_completed([ex.submit(run_symbol, j) for j in jobs]):
                collect(fu.result())
    for e in errors[:20]:
        log.error(f"  ERROR {e}")
    if len(errors) > 20:
        log.error(f"  ... {len(errors) - 20} more errors (see log file)")
        for e in errors[20:]:
            log.debug(f"  ERROR {e}")
    for w in sorted(set(warnings))[:10]:
        log.warning(f"  {w}")
    if len(set(warnings)) > 10:
        log.warning(f"  ... {len(set(warnings)) - 10} more warnings (see log file)")
    out = {}
    for k, lst in parts.items():
        out[k] = (pd.concat(lst, ignore_index=True).sort_values(["entry_ts", "symbol"]).reset_index(drop=True)
                  if lst else pd.DataFrame())
    return out, diags, errors, warnings
