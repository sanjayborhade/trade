"""Stage 2: realistic portfolio simulation.

All candidate trades from all stocks are processed in entry-time order against ONE
account. When several stocks signal in the same minute they are ranked (strategy
score by default) and taken one by one until a limit blocks them:

  * a stock can have only one open position
  * MAX_OPEN_POSITIONS, MAX_DAILY_TRADES
  * MAX_DAILY_LOSS (realised loss today [+ risk of open positions] may not exceed it)
  * quantity = equity x RISK_PER_TRADE / (stop distance + estimated costs per share)
  * capped by MAX_POSITION_VALUE_PCT / _ABS, remaining MAX_PORTFOLIO_EXPOSURE and
    MAX_PARTICIPATION of the fill candle's volume
  * equity used for sizing = realised equity at that moment (positions still open
    are not counted until they close)

Exits are booked when they happen, so capital and the daily loss limit always reflect
only what was known at the time of each new entry.
"""

from __future__ import annotations

import heapq
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional

import numpy as np
import pandas as pd

from .costs import CostModel
from .risk import participation_cap, risk_based_quantity

OPEN_TIME_EXITS = {"square_off", "time_exit", "trailing_exit", "strategy_exit"}


@dataclass
class PortfolioResult:
    trades: pd.DataFrame
    daily: pd.DataFrame
    rejections: Counter
    initial_capital: float
    start: Optional[date] = None
    end: Optional[date] = None
    notes: List[str] = field(default_factory=list)


def simulate_portfolio(cands: pd.DataFrame, cfg: dict, calendar: List[date],
                       start: Optional[date] = None, end: Optional[date] = None) -> PortfolioResult:
    pc = cfg["portfolio"]
    costs = CostModel(cfg["costs"])
    init = float(pc["INITIAL_CAPITAL"])
    cal = [d for d in calendar if (start is None or d >= start) and (end is None or d <= end)]
    rej: Counter = Counter()
    if cands is None or len(cands) == 0:
        return PortfolioResult(pd.DataFrame(), _daily(pd.DataFrame(), cal, init), rej, init, start, end)

    c = cands
    if start is not None or end is not None:
        dd = c["date"]
        m = np.ones(len(c), bool)
        if start is not None:
            m &= (dd >= start).to_numpy()
        if end is not None:
            m &= (dd <= end).to_numpy()
        c = c[m]
    c = c.reset_index(drop=True)
    if pc["ranking"] == "score":
        key = -c["score"].fillna(0).to_numpy()
    elif pc["ranking"] == "random":
        key = np.random.default_rng(int(pc.get("random_seed", 42))).random(len(c))
    else:
        key = np.zeros(len(c))
    order = np.lexsort((c["symbol"].to_numpy(), key, c["entry_ts"].to_numpy()))
    c = c.iloc[order].reset_index(drop=True)

    entry_ts = c["entry_ts"].to_numpy()
    exit_ts = c["exit_ts"].to_numpy()
    reasons = c["exit_reason"].to_numpy()
    dates = c["date"].to_numpy()
    sym = c["symbol"].to_numpy()
    side = c["side"].to_numpy()
    epx, xpx = c["entry_px"].to_numpy(), c["exit_px"].to_numpy()
    eraw, xraw = c["entry_raw"].to_numpy(), c["exit_raw"].to_numpy()
    rps = c["risk_ps"].to_numpy()
    vol = c["entry_bar_volume"].to_numpy()

    max_open = int(pc["MAX_OPEN_POSITIONS"])
    max_trades = int(pc["MAX_DAILY_TRADES"])
    risk_frac = float(pc["RISK_PER_TRADE"])
    max_loss = float(pc["MAX_DAILY_LOSS"])
    loss_mode = pc["daily_loss_mode"]
    expo = float(pc["MAX_PORTFOLIO_EXPOSURE"])
    pos_pct = float(pc["MAX_POSITION_VALUE_PCT"])
    pos_abs = pc.get("MAX_POSITION_VALUE_ABS")
    part = float(pc["MAX_PARTICIPATION"])
    fixed = pc["sizing_equity"] == "initial"
    cost_pct = costs.pct_estimate()

    equity = init
    heap: list = []          # (exit_ts, seq, k)
    open_syms: dict = {}
    open_risk = 0.0
    open_value = 0.0
    cur_day = None
    day_start_eq = equity
    realized_today = 0.0
    trades_today = 0
    out = {k: np.full(len(c), np.nan) for k in ("qty", "gross_pnl", "theoretical_pnl", "charges", "net_pnl",
                                                "r_multiple", "equity_at_entry", "position_value")}
    charge_cols = {k: np.full(len(c), np.nan) for k in ("brokerage", "stt", "exchange", "sebi", "ipft",
                                                        "stamp", "gst")}
    accepted = np.zeros(len(c), bool)
    capped = np.zeros(len(c), bool)
    seq = 0

    def release(k):
        nonlocal equity, open_risk, open_value, realized_today
        equity += out["net_pnl"][k]
        realized_today += out["net_pnl"][k]
        open_risk -= out["qty"][k] * rps[k]
        open_value -= out["position_value"][k]
        open_syms.pop(sym[k], None)

    for k in range(len(c)):
        t = entry_ts[k]
        while heap and (heap[0][0] < t or (heap[0][0] == t and reasons[heap[0][2]] in OPEN_TIME_EXITS)):
            release(heapq.heappop(heap)[2])
        if dates[k] != cur_day:
            while heap:
                release(heapq.heappop(heap)[2])
            cur_day, day_start_eq, realized_today, trades_today = dates[k], equity, 0.0, 0
        if sym[k] in open_syms:
            rej["symbol_already_open"] += 1
            continue
        if len(heap) >= max_open:
            rej["max_open_positions"] += 1
            continue
        if trades_today >= max_trades:
            rej["max_daily_trades"] += 1
            continue
        if loss_mode == "realized" and -realized_today >= max_loss * day_start_eq:
            rej["daily_loss_limit"] += 1
            continue
        size_eq = init if fixed else equity
        if size_eq <= 0:
            rej["no_equity"] += 1
            continue
        max_val = pos_pct * size_eq
        if pos_abs:
            max_val = min(max_val, float(pos_abs))
        q = risk_based_quantity(size_eq * risk_frac, epx[k], epx[k] - side[k] * rps[k], cost_pct * epx[k], max_val)
        room = expo * equity - open_value
        q_room = int(max(room, 0) // epx[k])
        if q_room < q:
            q = q_room
            if q < 1:
                rej["portfolio_exposure_limit"] += 1
                continue
        cap = participation_cap(vol[k], part)
        if q > cap:
            q = int(cap)
            capped[k] = True
        if q < 1:
            rej["position_size_zero"] += 1
            continue
        if loss_mode == "realized_plus_open_risk" and \
                (-realized_today + open_risk + q * rps[k]) > max_loss * day_start_eq:
            rej["daily_loss_limit"] += 1
            continue
        buy, sell = (epx[k] * q, xpx[k] * q) if side[k] == 1 else (xpx[k] * q, epx[k] * q)
        ch = costs.round_trip(buy, sell)
        gross = side[k] * (xpx[k] - epx[k]) * q
        out["qty"][k] = q
        out["gross_pnl"][k] = gross
        out["theoretical_pnl"][k] = side[k] * (xraw[k] - eraw[k]) * q
        out["charges"][k] = ch["total"]
        out["net_pnl"][k] = gross - ch["total"]
        out["r_multiple"][k] = (gross - ch["total"]) / (q * rps[k]) if rps[k] > 0 else np.nan
        out["equity_at_entry"][k] = equity
        out["position_value"][k] = epx[k] * q
        for kk in charge_cols:
            charge_cols[kk][k] = ch[kk]
        accepted[k] = True
        open_syms[sym[k]] = k
        open_risk += q * rps[k]
        open_value += epx[k] * q
        trades_today += 1
        heapq.heappush(heap, (exit_ts[k], seq, k))
        seq += 1
    while heap:
        release(heapq.heappop(heap)[2])

    res = c.copy()
    for kk, v in {**out, **charge_cols}.items():
        res[kk] = v
    res["volume_capped"] = capped
    trades = res[accepted].sort_values(["entry_ts", "symbol"]).reset_index(drop=True)
    rej["accepted"] = int(accepted.sum())
    rej["candidates"] = int(len(c))
    rej["volume_capped_quantity"] = int(capped[accepted].sum())
    trades.insert(0, "trade_id", np.arange(1, len(trades) + 1))
    return PortfolioResult(trades, _daily(trades, cal, init), rej, init, start, end)


def _daily(trades: pd.DataFrame, cal: List[date], init: float) -> pd.DataFrame:
    if not cal:
        return pd.DataFrame(columns=["date", "net_pnl", "gross_pnl", "trades", "equity", "return"])
    idx = pd.Index(cal, name="date")
    if len(trades):
        xdate = pd.to_datetime(trades["exit_ts"]).dt.date
        g = trades.groupby(xdate)
        net = g["net_pnl"].sum().reindex(idx, fill_value=0.0)
        gross = g["gross_pnl"].sum().reindex(idx, fill_value=0.0)
        n = trades.groupby(trades["date"])["trade_id"].count().reindex(idx, fill_value=0)
    else:
        net = pd.Series(0.0, index=idx)
        gross = pd.Series(0.0, index=idx)
        n = pd.Series(0, index=idx)
    eq = init + net.cumsum()
    prev = eq.shift(1).fillna(init)
    d = pd.DataFrame({"net_pnl": net.values, "gross_pnl": gross.values, "trades": n.values,
                      "equity": eq.values, "return": (net / prev).values}, index=idx).reset_index()
    return d
