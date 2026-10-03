"""Report generation: tables, robustness checks, an honest verdict, CSV/Parquet/HTML output."""

from __future__ import annotations

import html
import json
import os
from collections import Counter
from datetime import date
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from ..engine.portfolio import PortfolioResult
from .charts import make_charts
from .performance import (bootstrap_mean_ci, group_table, period_slice, summary, t_stat, time_of_day)
from .regimes import classify_trades

TRADE_COLUMNS = [
    ("trade_id", "Trade ID"), ("date", "Date"), ("symbol", "Symbol"), ("strategy", "Strategy"),
    ("side_label", "Side"), ("signal_ts", "Signal timestamp"), ("entry_ts", "Entry timestamp"),
    ("entry_px", "Entry price"), ("qty", "Quantity"), ("stop", "Stop price"), ("target", "Target price"),
    ("exit_ts", "Exit timestamp"), ("exit_px", "Exit price"), ("theoretical_pnl", "Theoretical P&L"),
    ("gross_pnl", "Gross P&L"), ("charges", "Transaction costs"), ("net_pnl", "Net P&L"),
    ("r_multiple", "R multiple"), ("hold_minutes", "Holding time (min)"), ("market_regime", "Market regime"),
    ("exit_reason", "Exit reason"),
]


def _fmt(v, nd=2):
    if isinstance(v, (int, np.integer)):
        return f"{v:,}"
    if isinstance(v, (float, np.floating)):
        if not np.isfinite(v):
            return "inf" if v > 0 else ("-inf" if v < 0 else "n/a")
        return f"{v:,.{nd}f}"
    return str(v)


def build_tables(trades: pd.DataFrame, daily: pd.DataFrame, init: float, periods: Dict[str, tuple]) -> Dict:
    t = trades
    tables: Dict[str, pd.DataFrame] = {}
    rows = []
    for name, (a, b) in periods.items():
        pt, pdly, base = period_slice(t, daily, a, b, init)
        s = summary(pt, pdly, base)
        rows.append({"period": name, "start": a, "end": b, **{k: s.get(k) for k in (
            "trades", "net_pnl", "gross_pnl", "charges", "total_return_pct", "cagr_pct", "max_drawdown_pct",
            "profit_factor", "win_rate_pct", "expectancy_r", "sharpe", "sortino")}})
    tables["by_period"] = pd.DataFrame(rows)
    yrs = sorted({d.year for d in daily["date"]}) if len(daily) else []
    rows = []
    for y in yrs:
        pt, pdly, base = period_slice(t, daily, date(y, 1, 1), date(y, 12, 31), init)
        s = summary(pt, pdly, base)
        rows.append({"year": y, **{k: s.get(k) for k in (
            "trades", "net_pnl", "gross_pnl", "charges", "total_return_pct", "max_drawdown_pct", "profit_factor",
            "win_rate_pct", "avg_win", "avg_loss", "expectancy_r", "sharpe", "sortino",
            "max_consecutive_losses")}})
    tables["by_year"] = pd.DataFrame(rows)
    if t.empty:
        return tables
    tables["by_symbol"] = group_table(t, "symbol").sort_values("net_pnl", ascending=False)
    m = t.assign(year=pd.to_datetime(t["exit_ts"]).dt.year, month=pd.to_datetime(t["exit_ts"]).dt.month)
    piv = m.pivot_table(index="year", columns="month", values="net_pnl", aggfunc="sum", fill_value=0.0)
    piv["total"] = piv.sum(axis=1)
    tables["monthly_pnl"] = piv.reset_index()
    tables["by_time_of_day"] = group_table(t.assign(time_of_day=time_of_day(t)), "time_of_day")
    for col in ("market_trend", "volatility", "gap", "expiry", "volume_regime", "market_regime"):
        if col in t:
            tables[f"by_regime_{col}"] = group_table(t, col)
    tables["by_setup"] = group_table(t.assign(setup=t["strategy"] + " " + t["side_label"]), "setup")
    tables["by_exit_reason"] = group_table(t, "exit_reason")
    return tables


def robustness(trades: pd.DataFrame, tables: Dict, cfg) -> Dict:
    out: Dict = {}
    if trades.empty:
        return out
    r = trades["r_multiple"].to_numpy(dtype=float)
    out["mean_r"] = float(np.nanmean(r))
    out["mean_r_ci95"] = bootstrap_mean_ci(r, int(cfg["analysis"]["bootstrap_samples"]))
    out["t_stat"] = t_stat(r)
    k = int(cfg["analysis"]["top_symbols_concentration"])
    bs = trades.groupby("symbol")["net_pnl"].sum().sort_values(ascending=False)
    total = float(bs.sum())
    out["symbols_traded"] = int(len(bs))
    out["pct_symbols_profitable"] = float((bs > 0).mean() * 100)
    out[f"net_from_top{k}_symbols"] = float(bs.head(k).sum())
    out[f"net_excluding_top{k}_symbols"] = float(total - bs.head(k).sum())
    out["top_symbols"] = list(bs.head(k).index)
    yr = tables.get("by_year")
    if yr is not None and len(yr):
        active = yr[yr["trades"] > 0]
        out["years_profitable"] = f"{int((active['net_pnl'] > 0).sum())}/{len(active)}"
        out["pct_years_profitable"] = float((active["net_pnl"] > 0).mean() * 100) if len(active) else float("nan")
    mp = tables.get("monthly_pnl")
    if mp is not None and len(mp):
        vals = mp.drop(columns=["year", "total"]).to_numpy().ravel()
        vals = vals[vals != 0]
        out["pct_months_profitable"] = float((vals > 0).mean() * 100) if len(vals) else float("nan")
    return out


def verdict(overall: Dict, rob: Dict, tables: Dict, min_trades: int = 100) -> (str, List[str]):
    n = overall.get("trades", 0)
    notes: List[str] = []
    if n == 0:
        return "NO TRADES", ["The strategy produced no trades - check filters, data and the warnings in the log."]
    net, gross, theo = overall["net_pnl"], overall["gross_pnl"], overall.get("theoretical_pnl", 0.0)
    if net <= 0:
        status = "NO EDGE AFTER COSTS"
        if theo > 0 >= gross:
            notes.append("Positive at signal prices, but slippage + spread turn it negative.")
        elif gross > 0:
            notes.append(f"Profitable before charges (Rs {gross:,.0f}) but transaction costs "
                         f"(Rs {overall['charges']:,.0f}) eliminate the edge.")
        else:
            notes.append("Loses money even before transaction costs.")
    else:
        status = "CANDIDATE EDGE (requires forward testing)"
    lo, hi = rob.get("mean_r_ci95", (np.nan, np.nan))
    if n < min_trades:
        notes.append(f"Only {n} trades: too few to draw conclusions (want >= {min_trades}).")
    if np.isfinite(lo) and lo <= 0 <= hi:
        notes.append(f"Mean R {rob['mean_r']:.3f} is not statistically different from zero "
                     f"(95% bootstrap CI {lo:.3f} to {hi:.3f}).")
    bp = tables.get("by_period")
    if bp is not None and len(bp):
        bad = bp[bp["net_pnl"].fillna(0) <= 0]["period"].tolist()
        if bad:
            notes.append(f"Net P&L <= 0 in period(s): {', '.join(bad)}.")
        tst = bp[bp["period"] == "test"]
        if len(tst) and net > 0 and float(tst["net_pnl"].fillna(0).iloc[0]) <= 0:
            notes.append("Performance DISAPPEARS in the out-of-sample test period.")
    k = [x for x in rob if x.startswith("net_excluding_top")]
    if k and net > 0 and rob[k[0]] <= 0 and rob.get("symbols_traded", 0) > 2 * len(rob.get("top_symbols", [])):
        notes.append(f"Profit is concentrated: without the top symbols ({', '.join(rob['top_symbols'])}) "
                     f"the result is {rob[k[0]]:,.0f}.")
    py = rob.get("pct_years_profitable")
    if py is not None and np.isfinite(py) and py < 60 and net > 0:
        notes.append(f"Only {rob.get('years_profitable')} calendar years profitable.")
    if net > 0 and len(notes) > 0:
        status = "NOT ROBUST / INCONCLUSIVE"
    if status.startswith("CANDIDATE"):
        notes.append("Passed the automatic checks. This is NOT proof of a future edge: paper-trade it, "
                     "and confirm on data the system has never seen.")
    return status, notes


def write_report(out_dir: str, name: str, title: str, research_basis: str, params: Dict,
                 res: PortfolioResult, cand_diag: Counter, cfg, periods: Dict[str, tuple],
                 regimes: pd.DataFrame, run_info: Dict, make_plots: bool = True) -> Dict:
    os.makedirs(out_dir, exist_ok=True)
    tdir = os.path.join(out_dir, "tables")
    os.makedirs(tdir, exist_ok=True)
    init = res.initial_capital
    trades = res.trades.copy()
    if len(trades):
        trades = classify_trades(trades, regimes, cfg)
        trades["side_label"] = np.where(trades["side"] == 1, "LONG", "SHORT")
    overall = summary(trades, res.daily, init)
    tables = build_tables(trades, res.daily, init, periods)
    rob = robustness(trades, tables, cfg)
    status, notes = verdict(overall, rob, tables, int(cfg["optimization"]["min_trades"]))

    # ---- files
    if len(trades):
        main = [c for c, _ in TRADE_COLUMNS if c in trades]
        extra = [c for c in trades.columns if c not in main]
        tr_out = trades[main + extra].rename(columns=dict(TRADE_COLUMNS))
        tr_out.to_csv(os.path.join(out_dir, "trades.csv"), index=False)
        if cfg["output"].get("save_parquet", True):
            try:
                tr_out.to_parquet(os.path.join(out_dir, "trades.parquet"), index=False)
            except Exception:  # noqa: BLE001 - parquet is optional
                pass
    res.daily.to_csv(os.path.join(out_dir, "daily_equity.csv"), index=False)
    for k, v in tables.items():
        if isinstance(v, pd.DataFrame) and len(v):
            v.to_csv(os.path.join(tdir, f"{k}.csv"), index=False)
    funnel = {"signals": int(cand_diag.get("signals", 0)), "orders_filled": int(cand_diag.get("filled", 0)),
              "candidate_trades": int(cand_diag.get("trades", 0)), **{f"order_{k}": int(v) for k, v in
                                                                       cand_diag.items() if k not in
                                                                       ("signals", "filled", "trades")},
              **{f"portfolio_{k}": int(v) for k, v in res.rejections.items()}}
    charts = make_charts(trades, res.daily, os.path.join(out_dir, "charts"), title, periods) if make_plots else []
    js = {"strategy": name, "title": title, "status": status, "notes": notes, "params": params,
          "overall": overall, "robustness": rob, "funnel": funnel, "periods": {k: [str(a), str(b)] for k, (a, b)
                                                                                in periods.items()},
          "run": run_info}
    with open(os.path.join(out_dir, "summary.json"), "w") as f:
        json.dump(js, f, indent=1, default=_json_default)
    _write_md(out_dir, js, tables, charts, research_basis)
    _write_html(out_dir, js, tables, charts, research_basis)
    return js


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (date, pd.Timestamp)):
        return str(o)
    if isinstance(o, tuple):
        return list(o)
    return str(o)


OVERALL_ORDER = [
    ("initial_capital", "Initial capital"), ("final_capital", "Final capital"),
    ("theoretical_pnl", "Theoretical P&L (signal prices)"), ("slippage_spread_cost", "Slippage + spread cost"),
    ("gross_pnl", "Gross P&L (after slippage, before charges)"), ("charges", "Transaction charges"),
    ("net_pnl", "Net P&L"), ("total_return_pct", "Total return %"), ("cagr_pct", "CAGR %"),
    ("max_drawdown_pct", "Max drawdown %"), ("longest_underwater_days", "Longest time below peak (days)"),
    ("profit_factor", "Profit factor (net)"), ("gross_profit_factor", "Profit factor (gross)"),
    ("win_rate_pct", "Win rate %"), ("avg_win", "Average win"), ("avg_loss", "Average loss"),
    ("payoff_ratio", "Avg win / avg loss"), ("expectancy_rs", "Expectancy per trade (Rs)"),
    ("expectancy_r", "Expectancy per trade (R)"), ("sharpe", "Sharpe (daily, annualised)"),
    ("sortino", "Sortino"), ("trades", "Number of trades"), ("long_trades", "Long trades"),
    ("short_trades", "Short trades"), ("avg_trades_per_day", "Average trades/day"),
    ("largest_win", "Largest winning trade"), ("largest_loss", "Largest losing trade"),
    ("max_consecutive_wins", "Max consecutive wins"), ("max_consecutive_losses", "Max consecutive losses"),
    ("avg_hold_minutes", "Average holding time (min)"), ("trading_days", "Trading days in test"),
]


def _overall_rows(o):
    return [(lbl, _fmt(o[k])) for k, lbl in OVERALL_ORDER if k in o]


def _write_md(out_dir, js, tables, charts, basis):
    L = [f"# Backtest report - {js['title']}", "", f"**VERDICT: {js['status']}**", ""]
    L += [f"- {n}" for n in js["notes"]]
    L += ["", f"Research basis: {basis}", "", "## Parameters", "", "```", json.dumps(js["params"], indent=1),
          "```", "", "## Overall", "", "| Metric | Value |", "|---|---|"]
    L += [f"| {a} | {b} |" for a, b in _overall_rows(js["overall"])]
    L += ["", "## Robustness", ""]
    for k, v in js["robustness"].items():
        L.append(f"- {k}: {_fmt(v) if not isinstance(v, (list, tuple)) else v}")
    L += ["", "## Signal -> trade funnel", ""] + [f"- {k}: {v:,}" for k, v in js["funnel"].items()]
    for name in ("by_period", "by_year", "by_time_of_day", "by_regime_market_trend", "by_regime_volatility",
                 "by_regime_gap", "by_regime_expiry", "by_regime_volume_regime", "by_setup", "by_exit_reason"):
        df = tables.get(name)
        if df is not None and len(df):
            L += ["", f"## {name.replace('_', ' ').title()}", "", df.to_markdown(index=False, floatfmt=",.2f")
                  if _has_tabulate() else df.to_string(index=False)]
    bs = tables.get("by_symbol")
    if bs is not None and len(bs):
        L += ["", "## Top / bottom 15 stocks (full list in tables/by_symbol.csv)", ""]
        sel = pd.concat([bs.head(15), bs.tail(15)]).drop_duplicates()
        L.append(sel.to_markdown(index=False, floatfmt=",.2f") if _has_tabulate() else sel.to_string(index=False))
    L += ["", "## Charts", ""] + [f"![{c}](charts/{c})" for c in charts]
    with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))


def _has_tabulate():
    try:
        import tabulate  # noqa: F401
        return True
    except ImportError:
        return False


def _write_html(out_dir, js, tables, charts, basis):
    st = js["status"]
    color = "#2a7f62" if st.startswith("CANDIDATE") else ("#b9770e" if st.startswith("NOT ROBUST") else "#c0392b")
    h = ["<!doctype html><html><head><meta charset='utf-8'><title>Backtest report</title><style>",
         "body{font-family:Segoe UI,Arial,sans-serif;max-width:1200px;margin:24px auto;padding:0 16px;color:#222}",
         "table{border-collapse:collapse;font-size:13px;margin:8px 0 20px}td,th{border:1px solid #ddd;"
         "padding:4px 8px;text-align:right}th{background:#f3f3f3}td:first-child,th:first-child{text-align:left}",
         f".v{{padding:12px 16px;border-left:6px solid {color};background:#fafafa;margin:12px 0}}",
         "img{max-width:100%;border:1px solid #eee;margin:8px 0}</style></head><body>",
         f"<h1>{html.escape(js['title'])}</h1><div class='v'><b>VERDICT: {html.escape(st)}</b><ul>"]
    h += [f"<li>{html.escape(n)}</li>" for n in js["notes"]]
    h += ["</ul></div>", f"<p><i>Research basis:</i> {html.escape(basis)}</p>",
          f"<p><i>Parameters:</i> <code>{html.escape(json.dumps(js['params']))}</code></p>",
          "<h2>Overall</h2><table>"]
    h += [f"<tr><td>{html.escape(a)}</td><td>{html.escape(b)}</td></tr>" for a, b in _overall_rows(js["overall"])]
    h.append("</table><h2>Robustness</h2><table>")
    for k, v in js["robustness"].items():
        h.append(f"<tr><td>{html.escape(k)}</td><td>{html.escape(_fmt(v) if not isinstance(v, (list, tuple)) else str(v))}"
                 f"</td></tr>")
    h.append("</table><h2>Signal &rarr; trade funnel</h2><table>")
    h += [f"<tr><td>{html.escape(k)}</td><td>{v:,}</td></tr>" for k, v in js["funnel"].items()]
    h.append("</table>")
    for c in charts:
        h.append(f"<img src='charts/{c}'>")
    for name, df in tables.items():
        if isinstance(df, pd.DataFrame) and len(df):
            show = df if name != "by_symbol" else pd.concat([df.head(20), df.tail(20)]).drop_duplicates()
            h.append(f"<h2>{html.escape(name.replace('_', ' '))}</h2>")
            h.append(show.to_html(index=False, float_format=lambda x: f"{x:,.2f}", border=0))
    h.append("</body></html>")
    with open(os.path.join(out_dir, "report.html"), "w", encoding="utf-8") as f:
        f.write("\n".join(h))
