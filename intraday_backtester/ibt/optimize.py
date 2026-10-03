"""Robust parameter search.

Rules that keep it honest:
  1. Parameters are ranked on the DEVELOPMENT period only.
  2. The top candidates are then checked on VALIDATION; degradation is reported.
  3. The out-of-sample TEST period is never used for selection; it is evaluated only with
     --final, for one parameter set you have already chosen (do this once).
  4. Ranking is NOT "highest profit". Every metric (expectancy, profit factor, Sharpe,
     Sortino, CAGR, drawdown, stability across years and stocks) is turned into a percentile
     rank and averaged; then each parameter set is scored by the average of itself and its
     grid neighbours ("plateau score"). An isolated spike surrounded by poor neighbours is a
     classic overfit and scores low.
  5. Optional walk-forward: choose on each rolling training window, measure on the following
     unseen window, report the stitched out-of-sample result.
"""

from __future__ import annotations

import itertools
import json
import os
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import yaml

from .analysis.charts import heatmap
from .analysis.performance import summary
from .engine.backtester import generate_candidates
from .engine.portfolio import simulate_portfolio
from .logutil import get_logger
from .strategies import build

RANK_METRICS = [("expectancy_r", True), ("profit_factor", True), ("sharpe", True), ("sortino", True),
                ("cagr_pct", True), ("max_drawdown_pct", True), ("pct_years_positive", True),
                ("pct_symbols_positive", True), ("net_pnl", True)]


def expand_grid(grid: Dict[str, list], fixed: Optional[Dict] = None) -> Tuple[List[str], List[Dict]]:
    keys = list(grid)
    combos = []
    for vals in itertools.product(*[grid[k] for k in keys]):
        d = dict(fixed or {})
        d.update(dict(zip(keys, vals)))
        combos.append(d)
    return keys, combos


def window_metrics(res) -> Dict:
    t = res.trades
    m = summary(t, res.daily, res.initial_capital)
    keep = {k: m.get(k, np.nan) for k in ("trades", "net_pnl", "gross_pnl", "cagr_pct", "profit_factor",
                                          "expectancy_r", "expectancy_rs", "max_drawdown_pct", "sharpe",
                                          "sortino", "win_rate_pct")}
    if len(t):
        yr = t.groupby(pd.to_datetime(t["exit_ts"]).dt.year)["net_pnl"].sum()
        keep["pct_years_positive"] = float((yr > 0).mean() * 100)
        sy = t.groupby("symbol")["net_pnl"].sum().sort_values(ascending=False)
        keep["pct_symbols_positive"] = float((sy > 0).mean() * 100)
        keep["net_excl_top5_symbols"] = float(sy.iloc[5:].sum())
    else:
        keep.update(pct_years_positive=np.nan, pct_symbols_positive=np.nan, net_excl_top5_symbols=np.nan)
    return keep


def rank_and_plateau(df: pd.DataFrame, keys: List[str], grid: Dict[str, list], min_trades: int) -> pd.DataFrame:
    df = df.copy()
    elig = df["dev_trades"] >= min_trades
    score = np.zeros(len(df))
    for m, higher in RANK_METRICS:
        col = df[f"dev_{m}"].replace([np.inf, -np.inf], np.nan)
        r = col.where(elig).rank(pct=True, ascending=higher)
        score += r.fillna(0).to_numpy()
    df["robust_score"] = np.where(elig, score / len(RANK_METRICS), np.nan)
    pos = {k: {v: i for i, v in enumerate(grid[k])} for k in keys}
    coords = np.array([[pos[k][r[k]] for k in keys] for _, r in df.iterrows()]) if keys else np.zeros((len(df), 0))
    plateau, nmin, nn = [], [], []
    for i in range(len(df)):
        if not keys:
            nb = [i]
        else:
            d = np.abs(coords - coords[i]).sum(axis=1)
            nb = list(np.flatnonzero(d <= 1))
        sc = df["robust_score"].iloc[nb].fillna(0).to_numpy()
        ex = df["dev_expectancy_r"].iloc[nb].to_numpy()
        plateau.append(float(sc.mean()))
        nmin.append(float(np.nanmin(np.where(df["dev_trades"].iloc[nb] >= min_trades, ex, -np.inf))))
        nn.append(len(nb) - 1)
    df["plateau_score"] = plateau
    df["worst_neighbour_expectancy_r"] = nmin
    df["neighbours"] = nn
    return df.sort_values("plateau_score", ascending=False)


def run_optimization(cfg, name: str, grid: Dict[str, list], fixed: Dict, symbols: List[str], universe: str,
                     calendar, periods: Dict, out_dir: str, walk_windows=None, workers=None) -> Dict:
    log = get_logger()
    keys, combos = expand_grid(grid, fixed)
    for c in combos:
        build(name, cfg, c)  # validates every parameter set up-front
    log.info(f"Optimising {name}: {len(combos)} parameter sets x {len(symbols)} symbols "
             f"(grid: {grid}, fixed: {fixed})")
    dev, val = periods["development"], periods["validation"]
    bs = max(1, int(cfg["optimization"]["batch_size"]))
    rows: List[Dict] = []
    wf_rows: List[Dict] = []
    for b0 in range(0, len(combos), bs):
        batch = combos[b0:b0 + bs]
        specs = [(f"c{b0 + k:04d}", name, c) for k, c in enumerate(batch)]
        log.info(f"  batch {b0 // bs + 1}/{(len(combos) + bs - 1) // bs}: sets {b0 + 1}-{b0 + len(batch)}")
        cands, _, errors, _ = generate_candidates(cfg, symbols, specs, universe, workers, progress=False)
        if errors:
            log.warning(f"  {len(errors)} errors in this batch (see log)")
        for (key, _, params) in specs:
            c = cands[key]
            r = {"set": key, **{k: params[k] for k in keys}}
            for label, (a, z) in (("dev", dev), ("val", val)):
                m = window_metrics(simulate_portfolio(c, cfg, calendar, a, z))
                r.update({f"{label}_{k}": v for k, v in m.items()})
            rows.append(r)
            for wi, ((ta, tz), (sa, sz)) in enumerate(walk_windows or []):
                tr = window_metrics(simulate_portfolio(c, cfg, calendar, ta, tz))
                te = window_metrics(simulate_portfolio(c, cfg, calendar, sa, sz))
                wf_rows.append({"window": wi, "set": key, "train_start": ta, "train_end": tz, "test_start": sa,
                                "test_end": sz, **{f"train_{k}": v for k, v in tr.items()},
                                **{f"test_{k}": v for k, v in te.items()}, **{k: params[k] for k in keys}})
            cands[key] = None
    df = pd.DataFrame(rows)
    for k in keys:   # warn about grid parameters that change nothing (e.g. stop_atr while stop_mode=or)
        others = [o for o in keys if o != k]
        g = df.groupby(others)["dev_net_pnl"] if others else df["dev_net_pnl"]
        spread = g.nunique() if others else pd.Series([df["dev_net_pnl"].nunique()])
        if (spread <= 1).all():
            log.warning(f"Parameter '{k}' has NO effect on results with the current fixed settings "
                        f"(e.g. stop_atr needs stop_mode=atr). Remove it from the grid.")
    min_tr = int(cfg["optimization"]["min_trades"])
    df = rank_and_plateau(df, keys, grid, min_tr)
    df.to_csv(os.path.join(out_dir, "optimization_results.csv"), index=False)

    # recommendation: best plateau whose own AND worst-neighbour development expectancy are positive
    ok = df[(df["dev_trades"] >= min_tr) & (df["dev_expectancy_r"] > 0) & (df["worst_neighbour_expectancy_r"] > 0)]
    rec = None
    msg = []
    if ok.empty:
        msg.append("NO parameter set has positive development expectancy with positive neighbours. "
                   "There is no robust parameter region - do not proceed to live trading with this strategy.")
    else:
        rec = ok.iloc[0]
        params = {**fixed, **{k: _py(rec[k]) for k in keys}}
        with open(os.path.join(out_dir, "recommended_params.yaml"), "w") as f:
            yaml.safe_dump({"strategy": name, "params": params}, f, sort_keys=False)
        msg.append(f"Recommended (most robust region, chosen on development data only): {params}")
        dv, vv = rec["dev_expectancy_r"], rec["val_expectancy_r"]
        msg.append(f"Development expectancy {dv:.3f}R on {int(rec['dev_trades'])} trades -> validation "
                   f"{vv:.3f}R on {int(rec['val_trades']) if np.isfinite(rec['val_trades']) else 0} trades.")
        if not (np.isfinite(vv) and vv > 0):
            msg.append("VALIDATION FAILS: the chosen parameters do not hold up on unseen data.")
        elif vv < 0.5 * dv:
            msg.append("Validation expectancy is less than half of development: expect strong decay.")
    top = df.head(int(cfg["optimization"]["top_n_validate"]))
    best_profit = df.sort_values("dev_net_pnl", ascending=False).iloc[0]
    msg.append(f"(For comparison, the single highest-profit development set was {best_profit['set']} with "
               f"validation expectancy {best_profit['val_expectancy_r']:.3f}R.)")

    charts = []
    if len(keys) >= 2:
        for x, y in itertools.combinations(keys, 2):
            p = os.path.join(out_dir, f"heatmap_{x}_vs_{y}.png")
            try:
                charts.append(heatmap(df, x, y, "dev_expectancy_r", p,
                                      f"Development expectancy (R), averaged over other parameters"))
            except Exception as e:  # noqa: BLE001
                log.warning(f"heatmap {x}/{y} failed: {e}")

    wf_summary = None
    if wf_rows:
        wf = pd.DataFrame(wf_rows)
        wf.to_csv(os.path.join(out_dir, "walk_forward_all.csv"), index=False)
        picks = []
        for wi, g in wf.groupby("window"):
            g = g.copy()
            g["dev_trades"] = g["train_trades"]
            for m, _ in RANK_METRICS:
                g[f"dev_{m}"] = g[f"train_{m}"]
            g = rank_and_plateau(g, keys, grid, max(20, min_tr // 4))
            pick = g.iloc[0]
            picks.append({"window": wi, "train": f"{pick['train_start']}..{pick['train_end']}",
                          "test": f"{pick['test_start']}..{pick['test_end']}", "chosen_set": pick["set"],
                          **{k: pick[k] for k in keys}, "train_expectancy_r": pick["train_expectancy_r"],
                          "test_trades": pick["test_trades"], "test_net_pnl": pick["test_net_pnl"],
                          "test_expectancy_r": pick["test_expectancy_r"],
                          "test_profit_factor": pick["test_profit_factor"]})
        pk = pd.DataFrame(picks)
        pk.to_csv(os.path.join(out_dir, "walk_forward.csv"), index=False)
        tr_n = pk["test_trades"].fillna(0)
        pooled = float((pk["test_expectancy_r"].fillna(0) * tr_n).sum() / tr_n.sum()) if tr_n.sum() else np.nan
        tm = float(pk["train_expectancy_r"].mean())
        # efficiency only meaningful when the in-sample result was positive
        eff = float(pk["test_expectancy_r"].mean()) / tm if np.isfinite(tm) and tm > 0 else np.nan
        wf_summary = {"windows": len(pk), "oos_trades": int(tr_n.sum()), "oos_net_pnl": float(pk["test_net_pnl"].sum()),
                      "oos_pooled_expectancy_r": pooled,
                      "pct_windows_profitable": float((pk["test_net_pnl"] > 0).mean() * 100),
                      "walk_forward_efficiency": float(eff) if np.isfinite(eff) else None}
        msg.append(f"Walk-forward: {wf_summary}")
    res = {"strategy": name, "combos": len(combos), "messages": msg,
           "recommended": None if rec is None else {k: _py(rec[k]) for k in keys},
           "top_by_plateau": top[["set"] + keys + ["plateau_score", "dev_trades", "dev_expectancy_r",
                                                  "dev_profit_factor", "val_trades", "val_expectancy_r"]]
           .to_dict(orient="records"), "walk_forward": wf_summary, "charts": charts}
    with open(os.path.join(out_dir, "optimization_summary.json"), "w") as f:
        json.dump(res, f, indent=1, default=str)
    return res


def _py(v):
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return float(v)
    return v
