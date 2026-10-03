"""Performance statistics."""

from __future__ import annotations

import math
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

TOD_BUCKETS = [("09:15-10:00", 0, 45), ("10:00-11:00", 45, 105), ("11:00-12:00", 105, 165),
               ("12:00-13:00", 165, 225), ("13:00-14:00", 225, 285), ("14:00-15:00", 285, 345),
               ("15:00-15:30", 345, 376)]


def _streaks(x: np.ndarray):
    best_w = best_l = cw = cl = 0
    for v in x:
        if v > 0:
            cw, cl = cw + 1, 0
        else:
            cl, cw = cl + 1, 0
        best_w, best_l = max(best_w, cw), max(best_l, cl)
    return best_w, best_l


def drawdown(equity: pd.Series):
    peak = equity.cummax()
    dd = equity / peak - 1
    return dd


def recovery_days(equity: pd.Series) -> int:
    """Longest number of trading days spent below a previous equity high."""
    peak = equity.cummax()
    under = (equity < peak - 1e-9).to_numpy()
    best = cur = 0
    for u in under:
        cur = cur + 1 if u else 0
        best = max(best, cur)
    return int(best)


def summary(trades: pd.DataFrame, daily: pd.DataFrame, init: float) -> Dict[str, float]:
    n_days = len(daily)
    out: Dict[str, float] = {"initial_capital": init}
    eq = daily["equity"] if n_days else pd.Series([init])
    final = float(eq.iloc[-1]) if n_days else init
    out["final_capital"] = final
    n = len(trades)
    out["trades"] = n
    out["trading_days"] = n_days
    if n == 0:
        out.update({"net_pnl": 0.0, "gross_pnl": 0.0, "total_return_pct": 0.0})
        return out
    net = trades["net_pnl"].to_numpy()
    gross = trades["gross_pnl"].to_numpy()
    theo = trades["theoretical_pnl"].to_numpy()
    r = trades["r_multiple"].to_numpy()
    wins, losses = net[net > 0], net[net <= 0]
    out["theoretical_pnl"] = float(theo.sum())
    out["slippage_spread_cost"] = float(theo.sum() - gross.sum())
    out["gross_pnl"] = float(gross.sum())
    out["charges"] = float(trades["charges"].sum())
    out["net_pnl"] = float(net.sum())
    out["total_return_pct"] = (final / init - 1) * 100
    years = n_days / 252 if n_days else 0
    out["cagr_pct"] = ((final / init) ** (1 / years) - 1) * 100 if years > 0 and final > 0 else float("nan")
    dd = drawdown(eq)
    out["max_drawdown_pct"] = float(dd.min() * 100)
    out["max_drawdown_rs"] = float((eq - eq.cummax()).min())
    out["longest_underwater_days"] = recovery_days(eq)
    out["profit_factor"] = float(wins.sum() / -losses.sum()) if losses.sum() < 0 else float("inf")
    out["gross_profit_factor"] = (float(gross[gross > 0].sum() / -gross[gross <= 0].sum())
                                  if gross[gross <= 0].sum() < 0 else float("inf"))
    out["win_rate_pct"] = len(wins) / n * 100
    out["avg_win"] = float(wins.mean()) if len(wins) else 0.0
    out["avg_loss"] = float(losses.mean()) if len(losses) else 0.0
    out["payoff_ratio"] = out["avg_win"] / -out["avg_loss"] if out["avg_loss"] < 0 else float("inf")
    out["expectancy_rs"] = float(net.mean())
    out["expectancy_r"] = float(np.nanmean(r))
    out["avg_trade_rs"] = out["expectancy_rs"]
    ret = daily["return"].to_numpy() if n_days else np.array([0.0])
    sd = ret.std(ddof=1) if len(ret) > 1 else 0.0
    out["sharpe"] = float(ret.mean() / sd * math.sqrt(252)) if sd > 0 else float("nan")
    dsd = math.sqrt(np.mean(np.minimum(ret, 0) ** 2)) if len(ret) else 0.0
    out["sortino"] = float(ret.mean() / dsd * math.sqrt(252)) if dsd > 0 else float("nan")
    out["avg_trades_per_day"] = n / n_days if n_days else float("nan")
    out["largest_win"] = float(net.max())
    out["largest_loss"] = float(net.min())
    order = np.argsort(trades["exit_ts"].to_numpy(), kind="mergesort")
    out["max_consecutive_wins"], out["max_consecutive_losses"] = _streaks(net[order])
    out["avg_hold_minutes"] = float(trades["hold_minutes"].mean())
    out["long_trades"] = int((trades["side"] == 1).sum())
    out["short_trades"] = int((trades["side"] == -1).sum())
    return out


def group_table(trades: pd.DataFrame, by, min_trades: int = 1) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    rows = []
    for key, g in trades.groupby(by, observed=True, sort=True):
        net = g["net_pnl"].to_numpy()
        w, l = net[net > 0], net[net <= 0]
        cum = np.cumsum(net[np.argsort(g["exit_ts"].to_numpy(), kind="mergesort")])
        mdd = float((cum - np.maximum.accumulate(np.r_[0, cum])[1:]).min()) if len(cum) else 0.0
        rows.append({
            "group": key if not isinstance(key, tuple) else " | ".join(map(str, key)),
            "trades": len(g), "win_rate_pct": len(w) / len(g) * 100,
            "gross_pnl": g["gross_pnl"].sum(), "charges": g["charges"].sum(), "net_pnl": net.sum(),
            "profit_factor": w.sum() / -l.sum() if l.sum() < 0 else np.inf,
            "avg_trade_rs": net.mean(), "expectancy_r": g["r_multiple"].mean(), "max_drawdown_rs": mdd,
        })
    df = pd.DataFrame(rows)
    df = df[df["trades"] >= min_trades]
    name = by if isinstance(by, str) else "group"
    return df.rename(columns={"group": name})


def time_of_day(trades: pd.DataFrame) -> pd.Series:
    m = trades["entry_minute"].to_numpy()
    lab = np.full(len(m), "other", dtype=object)
    for name, a, b in TOD_BUCKETS:
        lab[(m >= a) & (m < b)] = name
    return pd.Series(lab, index=trades.index)


def bootstrap_mean_ci(x: np.ndarray, samples: int = 2000, seed: int = 7, alpha: float = 0.05):
    x = x[np.isfinite(x)]
    if len(x) < 2:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    means = np.empty(samples)
    for k in range(samples):
        means[k] = x[rng.integers(0, len(x), len(x))].mean()
    return float(np.quantile(means, alpha / 2)), float(np.quantile(means, 1 - alpha / 2))


def t_stat(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if len(x) < 2 or x.std(ddof=1) == 0:
        return float("nan")
    return float(x.mean() / (x.std(ddof=1) / math.sqrt(len(x))))


def period_slice(trades: pd.DataFrame, daily: pd.DataFrame, start, end, init_equity: Optional[float] = None):
    t = trades[(trades["date"] >= start) & (trades["date"] <= end)] if len(trades) else trades
    d = daily[(daily["date"] >= start) & (daily["date"] <= end)].copy()
    if len(d):
        base = float(d["equity"].iloc[0] - d["net_pnl"].iloc[0])
    else:
        base = init_equity or 0.0
    return t, d, base
