"""Run one, several or all strategies over your Nifty 500 1-minute data with a realistic portfolio.

Examples
    python backtest.py --list
    python backtest.py --strategy orb
    python backtest.py --strategy vwap --universe liquid
    python backtest.py --strategy all
    python backtest.py --strategy orb --symbols RELIANCE,TCS,INFY
    python backtest.py --strategy orb --symbols-file my_list.txt --start 2022-01-01 --end 2024-12-31
    python backtest.py --strategy orb --params "or_minutes=30,target_r=1.5"
    python backtest.py --strategy orb --limit 20            # quick test on the first 20 stocks

Parameters come from config.yaml (strategies: section) - they are the researched defaults,
NOT optimised values. Results are reported for the whole run AND separately for the
development / validation / out-of-sample periods.
"""

import argparse
import sys
import time

import pandas as pd

from ibt.analysis.regimes import market_regimes
from ibt.analysis.reports import write_report
from ibt.analysis.splits import compute_periods
from ibt.engine.backtester import generate_candidates, select_symbols
from ibt.engine.portfolio import simulate_portfolio
from ibt.runner import ensure_cache, parse_date, parse_params, start
from ibt.strategies import REGISTRY, build, resolve_names


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--strategy", default=None, help="name, comma list, or 'all'")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--list", action="store_true", help="list strategies and their default parameters")
    ap.add_argument("--symbols", help="comma-separated symbols (Mode 1/2)")
    ap.add_argument("--symbols-file", help="text file with one symbol per line")
    ap.add_argument("--universe", choices=["all", "liquid"], help="override universe.mode (Mode 3/4)")
    ap.add_argument("--limit", type=int, help="only the first N symbols (quick test)")
    ap.add_argument("--start", help="first date YYYY-MM-DD")
    ap.add_argument("--end", help="last date YYYY-MM-DD")
    ap.add_argument("--params", help="override parameters, e.g. \"or_minutes=30,target_r=1.5\" (one strategy)")
    ap.add_argument("--include-experimental", action="store_true")
    ap.add_argument("--workers", type=int)
    ap.add_argument("--no-charts", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    if a.list:
        for n, c in REGISTRY.items():
            s = c({})
            print(f"\n{s.describe()}\n  basis: {c.research_basis}\n  params: {s.p}")
        return
    if not a.strategy:
        ap.error("--strategy is required (or use --list)")
    try:
        names = resolve_names(a.strategy, a.include_experimental)
        overrides = parse_params(a.params)
    except ValueError as e:
        ap.error(str(e))
    if overrides and len(names) > 1:
        ap.error("--params can only be used with a single strategy")

    cfg, run_dir, log = start(a.config, f"backtest_{a.strategy.replace(',', '+')}", a.verbose)
    t0 = time.time()
    man = ensure_cache(cfg)
    calendar = [pd.Timestamp(d).date() for d in man["calendar"]]
    s_date, e_date = parse_date(a.start), parse_date(a.end)
    calendar = [d for d in calendar if (not s_date or d >= s_date) and (not e_date or d <= e_date)]
    if not calendar:
        log.error("No trading days in the selected date range.")
        sys.exit(4)
    periods = compute_periods(cfg, calendar)
    for k, (x, y) in periods.items():
        log.info(f"  {k:12s} {x} to {y}")
    try:
        symbols = select_symbols(cfg, a.symbols, a.symbols_file, a.limit)
    except Exception as e:  # noqa: BLE001
        log.error(str(e))
        sys.exit(4)
    universe = a.universe or cfg["universe"]["mode"]
    if not cfg["universe"].get("MEMBERSHIP_FILE"):
        log.warning("No universe.MEMBERSHIP_FILE: today's stock list is used for the past (survivorship bias - "
                    "results are likely optimistic).")
    specs = []
    for n in names:
        try:
            st = build(n, cfg, overrides)
        except ValueError as e:
            log.error(str(e))
            sys.exit(2)
        specs.append((n, n, overrides))
        log.info(f"Strategy {st.describe()} params={st.p}")
    log.info(f"Running {len(names)} strategy(ies) on {len(symbols)} symbol(s), universe={universe} ...")
    cands, diags, errors, _ = generate_candidates(cfg, symbols, specs, universe, a.workers)
    regimes = market_regimes(cfg, calendar)
    log.info(f"Regime benchmark: {regimes.attrs.get('benchmark')}")

    rows = []
    for n in names:
        c = cands[n]
        if len(c):
            c = c[(c["date"] >= calendar[0]) & (c["date"] <= calendar[-1])]
        res = simulate_portfolio(c, cfg, calendar)
        st = build(n, cfg, overrides)
        js = write_report(f"{run_dir}/{n}", n, st.title, st.research_basis, st.p, res, diags[n], cfg, periods,
                          regimes, {"symbols": len(symbols), "universe": universe, "start": str(calendar[0]),
                                    "end": str(calendar[-1]), "errors": len(errors)}, not a.no_charts)
        o = js["overall"]
        rows.append({"strategy": n, "verdict": js["status"], "trades": o.get("trades", 0),
                     "net_pnl": o.get("net_pnl", 0), "gross_pnl": o.get("gross_pnl", 0),
                     "charges": o.get("charges", 0), "win_rate_pct": o.get("win_rate_pct"),
                     "profit_factor": o.get("profit_factor"), "expectancy_r": o.get("expectancy_r"),
                     "max_drawdown_pct": o.get("max_drawdown_pct"), "sharpe": o.get("sharpe"),
                     "mean_r_ci95": js["robustness"].get("mean_r_ci95")})
        log.info(f"\n=== {n}: {js['status']}")
        for note in js["notes"]:
            log.info(f"    - {note}")
        log.info(f"    trades {o.get('trades', 0):,} | net Rs {o.get('net_pnl', 0):,.0f} | gross Rs "
                 f"{o.get('gross_pnl', 0):,.0f} | charges Rs {o.get('charges', 0):,.0f} | "
                 f"PF {o.get('profit_factor', float('nan')):.2f} | exp {o.get('expectancy_r', float('nan')):.3f}R | "
                 f"maxDD {o.get('max_drawdown_pct', 0):.1f}%")
        log.info(f"    report: {run_dir}/{n}/report.html")
    comp = pd.DataFrame(rows)
    comp.to_csv(f"{run_dir}/strategy_comparison.csv", index=False)
    if len(rows) > 1:
        log.info("\n" + comp.drop(columns=["mean_r_ci95"]).to_string(index=False, float_format=lambda x: f"{x:,.2f}"))
    log.info(f"\nDone in {time.time() - t0:.0f}s. Results: {run_dir}")
    if errors:
        log.warning(f"{len(errors)} error(s) occurred - see {run_dir}/backtest_*.log")


if __name__ == "__main__":
    main()
