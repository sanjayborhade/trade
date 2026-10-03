"""Robust parameter optimisation (development data only) with validation and optional walk-forward.

Examples
    python optimize.py --strategy orb
    python optimize.py --strategy orb --grid "or_minutes=5,15,30;stop_atr=0.5,1,1.5,2;target_r=1,1.5,2,3"
    python optimize.py --strategy orb --universe liquid --walk-forward
    python optimize.py --strategy orb --final --params-file results/<run>/orb/recommended_params.yaml

The grid defaults to `strategies.<name>.optimize` in config.yaml. The TEST period is only
touched with --final, for ONE parameter set chosen beforehand. Use it once.
"""

import argparse
import os
import sys
import time

import pandas as pd
import yaml

from ibt.analysis.regimes import market_regimes
from ibt.analysis.reports import write_report
from ibt.analysis.splits import compute_periods, walk_forward_windows
from ibt.engine.backtester import generate_candidates, select_symbols
from ibt.engine.portfolio import simulate_portfolio
from ibt.optimize import run_optimization
from ibt.runner import convert, ensure_cache, parse_params, start
from ibt.strategies import build, resolve_names


def parse_grid(s):
    grid = {}
    for part in s.split(";"):
        if not part.strip():
            continue
        k, v = part.split("=", 1)
        grid[k.strip()] = [convert(x.strip()) for x in v.split(",") if x.strip()]
    return grid


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--grid", help='e.g. "or_minutes=5,15,30;target_r=1,2,3" (default: config.yaml)')
    ap.add_argument("--fixed", help='parameters held constant, e.g. "entry_type=close,stop_mode=atr"')
    ap.add_argument("--symbols")
    ap.add_argument("--symbols-file")
    ap.add_argument("--universe", choices=["all", "liquid"])
    ap.add_argument("--limit", type=int)
    ap.add_argument("--walk-forward", action="store_true")
    ap.add_argument("--final", action="store_true", help="evaluate ONE chosen parameter set on the test period")
    ap.add_argument("--params-file", help="YAML with params (e.g. recommended_params.yaml) for --final")
    ap.add_argument("--workers", type=int)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    names = resolve_names(a.strategy)
    if len(names) != 1:
        ap.error("optimize one strategy at a time")
    name = names[0]
    cfg, run_dir, log = start(a.config, f"optimize_{name}", a.verbose)
    t0 = time.time()
    man = ensure_cache(cfg)
    calendar = [pd.Timestamp(d).date() for d in man["calendar"]]
    periods = compute_periods(cfg, calendar)
    for k, (x, y) in periods.items():
        log.info(f"  {k:12s} {x} to {y}")
    symbols = select_symbols(cfg, a.symbols, a.symbols_file, a.limit)
    universe = a.universe or cfg["universe"]["mode"]
    out_dir = os.path.join(run_dir, name)
    os.makedirs(out_dir, exist_ok=True)

    if a.final:
        if not a.params_file:
            ap.error("--final needs --params-file (the parameters you chose BEFORE looking at the test period)")
        with open(a.params_file) as f:
            params = (yaml.safe_load(f) or {}).get("params", {})
        log.warning("FINAL out-of-sample evaluation. Look at this ONCE; re-optimising after seeing it turns the "
                    "test period into development data.")
        cands, diags, _, _ = generate_candidates(cfg, symbols, [(name, name, params)], universe, a.workers)
        test = periods["test"]
        tcal = [d for d in calendar if test[0] <= d <= test[1]]
        res = simulate_portfolio(cands[name], cfg, tcal, *test)
        st = build(name, cfg, params)
        js = write_report(out_dir, name, st.title + " - FINAL OUT-OF-SAMPLE", st.research_basis, st.p, res,
                          diags[name], cfg, {"test": test}, market_regimes(cfg, calendar),
                          {"mode": "final", "symbols": len(symbols)})
        log.info(f"FINAL test {test[0]}..{test[1]}: {js['status']}")
        for n in js["notes"]:
            log.info(f"  - {n}")
        log.info(f"Report: {out_dir}/report.html")
        return

    scfg = (cfg.get("strategies") or {}).get(name) or {}
    grid = parse_grid(a.grid) if a.grid else dict(scfg.get("optimize") or {})
    if not grid:
        log.error(f"No grid given and strategies.{name}.optimize is empty in config.yaml")
        sys.exit(2)
    fixed = dict(scfg.get("params") or {})
    fixed.update(scfg.get("optimize_fixed") or {})
    fixed.update(parse_params(a.fixed))
    for k in grid:
        fixed.pop(k, None)
    wins = walk_forward_windows(cfg, [d for d in calendar]) if a.walk_forward else None
    if a.walk_forward:
        log.info(f"Walk-forward windows: {len(wins)}")
        if not wins:
            log.warning("Not enough data for the configured walk_forward windows.")
    res = run_optimization(cfg, name, grid, fixed, symbols, universe, calendar, periods, out_dir, wins, a.workers)
    log.info("\n" + "\n".join(res["messages"]))
    log.info("\nTop parameter sets by plateau (robustness) score:")
    log.info(pd.DataFrame(res["top_by_plateau"]).to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    log.info(f"\nAll results: {out_dir}/optimization_results.csv   ({time.time() - t0:.0f}s)")


if __name__ == "__main__":
    main()
