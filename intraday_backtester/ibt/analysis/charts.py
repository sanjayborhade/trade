"""Charts saved as PNG files (matplotlib, no display needed)."""

from __future__ import annotations

import os
from typing import Dict, List

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from .performance import time_of_day  # noqa: E402

POS, NEG, LINE, GREY = "#2a7f62", "#c0392b", "#1f4e79", "#8a8a8a"


def _save(fig, path):
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return os.path.basename(path)


def _bars(ax, labels, values, title, ylabel="Net P&L (Rs)"):
    vals = np.asarray(values, dtype=float)
    ax.bar(range(len(vals)), vals, color=[POS if v >= 0 else NEG for v in vals])
    ax.set_xticks(range(len(vals)))
    ax.set_xticklabels([str(x) for x in labels], rotation=45, ha="right", fontsize=8)
    ax.axhline(0, color=GREY, lw=0.8)
    ax.set_title(title)
    ax.set_ylabel(ylabel)


def make_charts(trades: pd.DataFrame, daily: pd.DataFrame, out_dir: str, title: str,
                periods: Dict[str, tuple] = None) -> List[str]:
    os.makedirs(out_dir, exist_ok=True)
    files: List[str] = []
    if daily.empty:
        return files
    d = daily.copy()
    d["date"] = pd.to_datetime(d["date"])

    fig, ax = plt.subplots(figsize=(11, 4.5))
    ax.plot(d["date"], d["equity"], color=LINE, lw=1.2, label="Net equity")
    if len(trades):
        g = trades.groupby(pd.to_datetime(trades["exit_ts"]).dt.normalize())["gross_pnl"].sum()
        g = g.reindex(d["date"], fill_value=0).cumsum() + d["equity"].iloc[0] - d["net_pnl"].iloc[0]
        ax.plot(d["date"], g.values, color=GREY, lw=1, ls="--", label="Before charges (gross)")
    for name, (a, b) in (periods or {}).items():
        ax.axvspan(pd.Timestamp(a), pd.Timestamp(b), alpha=0.06 if name != "test" else 0.12,
                   color={"development": "#1f77b4", "validation": "#ff7f0e", "test": "#d62728"}.get(name, "grey"))
        ax.text(pd.Timestamp(a), ax.get_ylim()[0], f" {name}", va="bottom", fontsize=8)
    ax.set_title(f"Equity curve - {title}")
    ax.set_ylabel("Rs")
    ax.legend(loc="best", fontsize=8)
    files.append(_save(fig, os.path.join(out_dir, "01_equity_curve.png")))

    fig, ax = plt.subplots(figsize=(11, 3.2))
    dd = (d["equity"] / d["equity"].cummax() - 1) * 100
    ax.fill_between(d["date"], dd, 0, color=NEG, alpha=0.5)
    ax.set_title("Drawdown (%)")
    files.append(_save(fig, os.path.join(out_dir, "02_drawdown.png")))

    if trades.empty:
        return files
    t = trades.copy()
    t["exit_date"] = pd.to_datetime(t["exit_ts"])

    m = d.set_index("date")["net_pnl"].resample("ME").sum()
    fig, ax = plt.subplots(figsize=(12, 4))
    _bars(ax, [x.strftime("%Y-%m") for x in m.index], m.values, "Monthly net P&L")
    if len(m) > 36:
        for lbl in ax.get_xticklabels()[::1]:
            lbl.set_visible(False)
        for lbl in ax.get_xticklabels()[::6]:
            lbl.set_visible(True)
    files.append(_save(fig, os.path.join(out_dir, "03_monthly_returns.png")))

    y = d.set_index("date")["net_pnl"].groupby(d["date"].dt.year.values).sum()
    fig, ax = plt.subplots(figsize=(8, 4))
    _bars(ax, y.index, y.values, "Net P&L by year")
    files.append(_save(fig, os.path.join(out_dir, "04_yearly_returns.png")))

    fig, ax = plt.subplots(figsize=(8, 4))
    net = t["net_pnl"].to_numpy()
    lim = np.nanpercentile(np.abs(net), 99) if len(net) > 20 else np.nanmax(np.abs(net))
    ax.hist(np.clip(net, -lim, lim), bins=60, color=LINE, alpha=0.8)
    ax.axvline(0, color=GREY)
    ax.axvline(np.mean(net), color=NEG if np.mean(net) < 0 else POS, ls="--", label=f"mean {np.mean(net):,.0f}")
    ax.set_title("Net P&L per trade (Rs, clipped at 99th pct)")
    ax.legend()
    files.append(_save(fig, os.path.join(out_dir, "05_pnl_distribution.png")))

    fig, ax = plt.subplots(figsize=(8, 4))
    r = t["r_multiple"].clip(-3, 6).to_numpy()
    ax.hist(r, bins=np.arange(-3, 6.25, 0.25), color=LINE, alpha=0.8)
    ax.axvline(0, color=GREY)
    ax.axvline(np.nanmean(t["r_multiple"]), color=NEG if np.nanmean(t["r_multiple"]) < 0 else POS, ls="--",
               label=f"mean {np.nanmean(t['r_multiple']):.3f}R")
    ax.set_title("R-multiple distribution (net, clipped -3..6)")
    ax.legend()
    files.append(_save(fig, os.path.join(out_dir, "06_r_distribution.png")))

    fig, axs = plt.subplots(1, 2, figsize=(10, 4))
    w, l = (net > 0).sum(), (net <= 0).sum()
    axs[0].bar(["Wins", "Losses"], [w, l], color=[POS, NEG])
    axs[0].set_title(f"Win/loss count (win rate {w / max(len(net), 1) * 100:.1f}%)")
    axs[1].bar(["Avg win", "Avg loss"], [net[net > 0].mean() if w else 0, net[net <= 0].mean() if l else 0],
               color=[POS, NEG])
    axs[1].set_title("Average win vs average loss (Rs)")
    files.append(_save(fig, os.path.join(out_dir, "07_win_loss.png")))

    tod = t.groupby(time_of_day(t))["net_pnl"].sum()
    fig, ax = plt.subplots(figsize=(8, 4))
    _bars(ax, tod.index, tod.values, "Net P&L by entry time of day")
    files.append(_save(fig, os.path.join(out_dir, "08_pnl_by_hour.png")))

    s = t.groupby("symbol")["net_pnl"].sum().sort_values()
    sel = pd.concat([s.head(15), s.tail(15)]) if len(s) > 30 else s
    sel = sel[~sel.index.duplicated()]
    fig, ax = plt.subplots(figsize=(12, 4.5))
    _bars(ax, sel.index, sel.values, f"Net P&L by stock (worst 15 / best 15 of {len(s)})")
    files.append(_save(fig, os.path.join(out_dir, "09_pnl_by_stock.png")))

    if "market_regime" in t:
        fig, axs = plt.subplots(1, 4, figsize=(15, 4))
        for ax, col in zip(axs, ("market_trend", "volatility", "gap", "expiry")):
            g = t.groupby(col)["net_pnl"].sum()
            _bars(ax, g.index, g.values, f"By {col.replace('_', ' ')}")
        files.append(_save(fig, os.path.join(out_dir, "10_pnl_by_regime.png")))
    return files


def heatmap(df: pd.DataFrame, x: str, y: str, value: str, path: str, title: str) -> str:
    piv = df.pivot_table(index=y, columns=x, values=value, aggfunc="mean")
    fig, ax = plt.subplots(figsize=(1.2 * max(4, piv.shape[1]) + 2, 0.6 * max(4, piv.shape[0]) + 2))
    vmax = np.nanmax(np.abs(piv.values)) if np.isfinite(piv.values).any() else 1
    im = ax.imshow(piv.values, cmap="RdYlGn", vmin=-vmax, vmax=vmax, aspect="auto")
    ax.set_xticks(range(piv.shape[1]))
    ax.set_xticklabels(piv.columns)
    ax.set_yticks(range(piv.shape[0]))
    ax.set_yticklabels(piv.index)
    ax.set_xlabel(x)
    ax.set_ylabel(y)
    for i in range(piv.shape[0]):
        for j in range(piv.shape[1]):
            v = piv.values[i, j]
            if np.isfinite(v):
                ax.text(j, i, f"{v:.3f}", ha="center", va="center", fontsize=8)
    fig.colorbar(im, ax=ax)
    ax.set_title(title)
    return _save(fig, path)
