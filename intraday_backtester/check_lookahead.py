"""Look-ahead bias check on YOUR data (or synthetic data).

For random cut-off times, everything after the cut is scrambled and each strategy is
re-run; every signal and every trade entered before the cut must be identical.

    python check_lookahead.py                         # 3 random stocks, all strategies
    python check_lookahead.py --symbols RELIANCE,TCS --cuts 10
    python check_lookahead.py --synthetic              # no data needed
"""

import argparse
import sys

import numpy as np

from ibt.data.store import load_bars, load_benchmark
from ibt.engine.backtester import select_symbols
from ibt.runner import ensure_cache, start
from ibt.strategies import REGISTRY
from ibt.validation import perturbation_test


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--symbols")
    ap.add_argument("--n", type=int, default=3, help="number of random stocks")
    ap.add_argument("--cuts", type=int, default=6)
    ap.add_argument("--synthetic", action="store_true")
    a = ap.parse_args()
    cfg, run_dir, log = start(a.config, "check_lookahead")
    names = list(REGISTRY)
    if a.synthetic:
        from tests.synthetic import make_symbol, trading_days
        days = trading_days("2023-01-02", 120)
        cfg["universe"]["min_trading_days"] = 20
        work = [("SYNTH", make_symbol(11, days), {"UNIVERSE_EW": make_symbol(12, days)})]
    else:
        ensure_cache(cfg)
        syms = select_symbols(cfg, a.symbols)
        if not a.symbols:
            rng = np.random.default_rng(0)
            syms = list(rng.choice(syms, size=min(a.n, len(syms)), replace=False))
        bench = {n: load_benchmark(cfg, n) for n in ("NIFTY50", "NIFTY500", "UNIVERSE_EW")}
        bench = {k: v for k, v in bench.items() if v is not None}
        work = [(s, load_bars(cfg, s), bench) for s in syms]
    total = 0
    for sym, bars, bench in work:
        log.info(f"Checking {sym} ({len(bars):,} candles), {len(names)} strategies, {a.cuts} cut-offs ...")
        fails = perturbation_test(sym, bars, names, cfg, bench, n_cuts=a.cuts, seed=1)
        total += len(fails)
        for f in fails[:20]:
            log.error(f"  LOOK-AHEAD: {f}")
    if total:
        log.error(f"FAILED: {total} look-ahead violation(s) found. Do not trust backtests until fixed.")
        sys.exit(1)
    log.info("PASSED: no decision before a cut-off changed when the future was altered.")


if __name__ == "__main__":
    main()
