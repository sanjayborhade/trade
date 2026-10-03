"""
intraday_lab.py -- cost model, position sizing, risk math and a minimal
bar-by-bar backtester for the Indian intraday strategies described in
research/indian_intraday_playbook.md.

Standard library only (Python 3.9+). Nothing here is investment advice;
every default is a starting assumption that must be checked against your
broker's contract note and backtested before use.

Usage (or use run.sh / run.bat in the repo root):
    python3 tools/intraday_lab.py costs        # round-trip cost tables
    python3 tools/intraday_lab.py risk         # loss-streak / sizing tables
    python3 tools/intraday_lab.py ruin         # Monte Carlo drawdown odds
    python3 tools/intraday_lab.py selftest     # sanity check on random data
    python3 tools/intraday_lab.py sample       # write a synthetic CSV to show the format
    python3 tools/intraday_lab.py backtest data.csv --instrument nifty --capital 1000000 --risk 0.5
    python3 tools/intraday_lab.py trades   data.csv --strategy orb --out trades.csv
    Strategies: orb (A), holygrail (B), fade (C), nr7 (D). Instruments: nifty, banknifty, stock.

CSV format for backtest (one row per 5-minute bar, IST, sorted):
    datetime,open,high,low,close,volume
    2026-01-05 09:15:00,26150,26180,26120,26170,123456
"""

from __future__ import annotations

import csv
import math
import os
import random
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# 1. Cost model
# ---------------------------------------------------------------------------
# Rates as published on a discount broker's charges page (checked Oct 2026)
# and the Budget 2026 STT revision effective 1 Apr 2026. Exchange charges
# are revised periodically -- verify against the latest NSE circular.

RATES = {
    "equity_intraday": {
        "stt_sell": 0.00025,        # 0.025% on sell side
        "txn": 0.0000307,           # NSE 0.00307% of turnover (both legs)
        "stamp_buy": 0.00003,       # 0.003% buy side
    },
    "futures": {
        "stt_sell": 0.0005,         # 0.05% on sell side (from 1 Apr 2026)
        "txn": 0.0000183,           # NSE 0.00183%
        "stamp_buy": 0.00002,       # 0.002% buy side
    },
    "options": {                    # all % are of PREMIUM turnover
        "stt_sell": 0.0015,         # 0.15% on sell side (from 1 Apr 2026)
        "txn": 0.0003553,           # NSE 0.03553%
        "stamp_buy": 0.00003,       # 0.003% buy side
    },
}
SEBI_FEE = 10 / 1e7                 # Rs 10 per crore
GST = 0.18                          # on brokerage + txn + SEBI fee
BROKERAGE_CAP = 20.0                # Rs per executed order (discount broker)
BROKERAGE_PCT = 0.0003              # 0.03% (intraday/futures), whichever lower


def round_trip_cost(segment: str, buy_value: float, sell_value: float) -> Dict[str, float]:
    """Statutory + brokerage cost of one buy and one sell order."""
    r = RATES[segment]
    if segment == "options":
        brk = 2 * BROKERAGE_CAP
    else:
        brk = min(BROKERAGE_CAP, BROKERAGE_PCT * buy_value) + \
            min(BROKERAGE_CAP, BROKERAGE_PCT * sell_value)
    turnover = buy_value + sell_value
    stt = r["stt_sell"] * sell_value
    txn = r["txn"] * turnover
    sebi = SEBI_FEE * turnover
    stamp = r["stamp_buy"] * buy_value
    gst = GST * (brk + txn + sebi)
    total = brk + stt + txn + sebi + stamp + gst
    return {"brokerage": brk, "stt": stt, "txn": txn, "sebi": sebi,
            "stamp": stamp, "gst": gst, "total": total}


# ---------------------------------------------------------------------------
# 2. Position sizing and risk math
# ---------------------------------------------------------------------------

def units_for_risk(capital: float, risk_pct: float, stop_distance: float,
                   cost_per_unit: float = 0.0, lot: int = 1) -> int:
    """Units (rounded DOWN to whole lots) so that stop-out loss incl. costs
    does not exceed capital * risk_pct. Returns 0 if even one lot is too big."""
    per_unit = stop_distance + cost_per_unit
    if per_unit <= 0:
        return 0
    raw = (capital * risk_pct) / per_unit
    return int(raw // lot) * lot


def streak_loss(capital: float, risk_pct: float, n: int, compounding: bool = True) -> float:
    """Rupee loss after n consecutive full-risk losses."""
    if compounding:  # risk recomputed on shrinking equity
        return capital * (1 - (1 - risk_pct) ** n)
    return capital * risk_pct * n


def required_gain_to_recover(drawdown: float) -> float:
    return drawdown / (1 - drawdown)


def prob_streak(p_loss: float, streak: int, n_trades: int, sims: int = 20000,
                seed: int = 7) -> float:
    """Monte Carlo probability of at least one losing streak >= `streak`
    within `n_trades` trades."""
    rng = random.Random(seed)
    hit = 0
    for _ in range(sims):
        run = 0
        for _ in range(n_trades):
            if rng.random() < p_loss:
                run += 1
                if run >= streak:
                    hit += 1
                    break
            else:
                run = 0
    return hit / sims


def drawdown_odds(win_rate: float, rr: float, risk_pct: float, n_trades: int,
                  dd_limit: float, sims: int = 10000, seed: int = 11) -> Dict[str, float]:
    """Monte Carlo: probability that equity ever falls dd_limit below its
    running peak within n_trades; also median final equity multiple.
    rr = average win / average loss (in R). Costs should already be inside rr."""
    rng = random.Random(seed)
    breaches, finals = 0, []
    for _ in range(sims):
        eq, peak, breached = 1.0, 1.0, False
        for _ in range(n_trades):
            if rng.random() < win_rate:
                eq *= 1 + risk_pct * rr
            else:
                eq *= 1 - risk_pct
            peak = max(peak, eq)
            if not breached and eq <= peak * (1 - dd_limit):
                breached = True
        breaches += breached
        finals.append(eq)
    return {"p_breach": breaches / sims, "median_final": statistics.median(finals),
            "p_loss_overall": sum(f < 1 for f in finals) / sims}


# ---------------------------------------------------------------------------
# 3. Minimal intraday backtester (5-minute bars, one instrument, long/short)
# ---------------------------------------------------------------------------

@dataclass
class Bar:
    dt: datetime
    o: float
    h: float
    l: float
    c: float
    v: float


@dataclass
class Trade:
    day: str
    side: int          # +1 long, -1 short
    entry_time: str
    entry: float
    stop: float
    exit_time: str = ""
    exit: float = 0.0
    qty: int = 0
    reason: str = ""
    pnl: float = 0.0   # net of costs and slippage, rupees
    r: float = 0.0     # net P&L in units of initial risk


@dataclass
class Config:
    capital: float = 500000.0
    risk_pct: float = 0.005              # 0.5% of equity per trade
    lot: int = 65                        # Nifty lot from Jan 2026
    segment: str = "futures"
    slippage_pts: float = 1.0            # per side, in price points
    or_minutes: int = 15                 # 09:15-09:30 opening range
    last_entry: time = time(13, 30)
    flat_time: time = time(15, 10)
    max_trades_per_day: int = 2
    daily_loss_r: float = 2.0            # stop for the day after -2R
    atr_len: int = 14                    # daily ATR length
    buffer_atr: float = 0.05             # breakout buffer as fraction of daily ATR
    min_rr_target: float = 2.0
    max_cost_frac: float = 0.25          # skip if round-trip cost > 25% of rupee risk
    ema_len: int = 20
    adx_len: int = 14
    adx_min: float = 30.0                # Raschke Holy Grail threshold
    nr_lookback: int = 7                 # Crabel NR7
    vol_mult: float = 0.5                # Williams-style % of prior range
    trades: List[Trade] = field(default_factory=list)


def load_csv(path: str) -> List[Bar]:
    out = []
    with open(path) as f:
        for row in csv.DictReader(f):
            out.append(Bar(datetime.fromisoformat(row["datetime"]), float(row["open"]),
                           float(row["high"]), float(row["low"]), float(row["close"]),
                           float(row.get("volume") or 0)))
    return out


def group_days(bars: List[Bar]) -> List[List[Bar]]:
    days, cur, d = [], [], None
    for b in bars:
        if b.dt.date() != d:
            if cur:
                days.append(cur)
            cur, d = [], b.dt.date()
        cur.append(b)
    if cur:
        days.append(cur)
    return days


def daily_atr(days: List[List[Bar]], i: int, n: int) -> Optional[float]:
    """ATR of the n COMPLETED days before day i (no look-ahead)."""
    if i < n + 1:
        return None
    trs = []
    for k in range(i - n, i):
        hi = max(b.h for b in days[k]); lo = min(b.l for b in days[k])
        pc = days[k - 1][-1].c
        trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
    return sum(trs) / n


def ema(vals: List[float], n: int) -> List[float]:
    k, out = 2 / (n + 1), []
    for v in vals:
        out.append(v if not out else out[-1] + k * (v - out[-1]))
    return out


def adx(bars: List[Bar], n: int) -> List[float]:
    """Wilder ADX on the given bar list (computed causally)."""
    out = [0.0] * len(bars)
    trs, pdm, ndm = 0.0, 0.0, 0.0
    dx_s = 0.0
    for i in range(1, len(bars)):
        b, p = bars[i], bars[i - 1]
        tr = max(b.h - b.l, abs(b.h - p.c), abs(b.l - p.c))
        up, dn = b.h - p.h, p.l - b.l
        pd = up if up > dn and up > 0 else 0.0
        nd = dn if dn > up and dn > 0 else 0.0
        trs = trs - trs / n + tr
        pdm = pdm - pdm / n + pd
        ndm = ndm - ndm / n + nd
        if trs == 0:
            continue
        pdi, ndi = 100 * pdm / trs, 100 * ndm / trs
        dx = 100 * abs(pdi - ndi) / (pdi + ndi) if pdi + ndi else 0.0
        dx_s = dx_s - dx_s / n + dx / n if i > n else dx
        out[i] = dx_s
    return out


def run_backtest(bars: List[Bar], strategy: str, cfg: Config) -> Config:
    days = group_days(bars)
    equity = cfg.capital
    all_closes = [b.c for b in bars]
    ema_all = ema(all_closes, cfg.ema_len)
    adx_all = adx(bars, cfg.adx_len)
    idx = 0
    for di, day in enumerate(days):
        start_idx = idx
        idx += len(day)
        atr = daily_atr(days, di, cfg.atr_len)
        if atr is None:
            continue
        prev = days[di - 1]
        p_hi, p_lo = max(b.h for b in prev), min(b.l for b in prev)
        prev_ranges = [max(b.h for b in d) - min(b.l for b in d)
                       for d in days[max(0, di - cfg.nr_lookback):di]]
        is_nr = len(prev_ranges) == cfg.nr_lookback and prev_ranges[-1] == min(prev_ranges)

        or_bars = [b for b in day if b.dt.time() < _add_min(time(9, 15), cfg.or_minutes)]
        if not or_bars:
            continue
        or_hi, or_lo = max(b.h for b in or_bars), min(b.l for b in or_bars)
        buf = cfg.buffer_atr * atr
        cum_pv = cum_v = 0.0
        pos: Optional[Trade] = None
        trades_today, day_r = 0, 0.0
        broke_up = broke_dn = False
        sides_used = set()  # ORB rule: at most one long and one short per day

        for j, b in enumerate(day):
            gi = start_idx + j
            tp = (b.h + b.l + b.c) / 3
            cum_pv += tp * (b.v or 1); cum_v += (b.v or 1)
            vwap = cum_pv / cum_v
            t = b.dt.time()

            # ---- manage open position (stop checked before target: conservative)
            if pos:
                hit_stop = (b.l <= pos.stop) if pos.side > 0 else (b.h >= pos.stop)
                if hit_stop:
                    # gap through stop fills at the worse of open and stop
                    px = min(b.o, pos.stop) if pos.side > 0 else max(b.o, pos.stop)
                    equity, day_r = _close(pos, b, px, "stop", cfg, equity, day_r)
                    pos = None
                elif t >= cfg.flat_time:
                    equity, day_r = _close(pos, b, b.c, "time", cfg, equity, day_r)
                    pos = None
                elif strategy == "orb" and ((pos.side > 0 and b.c < vwap) or
                                            (pos.side < 0 and b.c > vwap)):
                    equity, day_r = _close(pos, b, b.c, "vwap_trail", cfg, equity, day_r)
                    pos = None
                elif strategy == "fade":
                    tgt = vwap
                    if (pos.side > 0 and b.h >= tgt) or (pos.side < 0 and b.l <= tgt):
                        equity, day_r = _close(pos, b, tgt, "target_vwap", cfg, equity, day_r)
                        pos = None
                elif strategy == "holygrail":
                    risk = abs(pos.entry - pos.stop)
                    tgt = pos.entry + pos.side * cfg.min_rr_target * risk
                    if (pos.side > 0 and b.h >= tgt) or (pos.side < 0 and b.l <= tgt):
                        equity, day_r = _close(pos, b, tgt, "target_2R", cfg, equity, day_r)
                        pos = None
                continue

            # ---- entry gates
            if (t < _add_min(time(9, 15), cfg.or_minutes) or t > cfg.last_entry
                    or trades_today >= cfg.max_trades_per_day or day_r <= -cfg.daily_loss_r):
                continue

            side, stop = 0, 0.0
            if strategy == "orb":
                if b.c > or_hi + buf and b.c > vwap:
                    side, stop = 1, max(or_lo, b.c - 0.5 * atr)
                elif b.c < or_lo - buf and b.c < vwap:
                    side, stop = -1, min(or_hi, b.c + 0.5 * atr)
            elif strategy == "nr7":
                if is_nr:
                    lvl_up = day[0].o + cfg.vol_mult * (p_hi - p_lo)
                    lvl_dn = day[0].o - cfg.vol_mult * (p_hi - p_lo)
                    if b.c > lvl_up:
                        side, stop = 1, b.c - 0.5 * atr
                    elif b.c < lvl_dn:
                        side, stop = -1, b.c + 0.5 * atr
            elif strategy == "holygrail" and gi > 0:
                prevb = bars[gi - 1]
                e = ema_all[gi - 1]
                if adx_all[gi - 1] >= cfg.adx_min:
                    if ema_all[gi - 1] > ema_all[max(0, gi - 6)] and prevb.l <= e and b.c > prevb.h:
                        side, stop = 1, prevb.l
                    elif ema_all[gi - 1] < ema_all[max(0, gi - 6)] and prevb.h >= e and b.c < prevb.l:
                        side, stop = -1, prevb.h
            elif strategy == "fade":
                # failed breakout of prior-day high/low: poke outside, close back inside
                if b.h > p_hi:
                    broke_up = True
                if b.l < p_lo:
                    broke_dn = True
                if broke_up and b.c < p_hi and b.c > vwap:
                    side, stop = -1, max(x.h for x in day[:j + 1])
                elif broke_dn and b.c > p_lo and b.c < vwap:
                    side, stop = 1, min(x.l for x in day[:j + 1])

            if side == 0 or (stop - b.c) * side >= 0:
                continue
            if strategy == "orb" and side in sides_used:
                continue
            entry = b.c + side * cfg.slippage_pts  # fill at close of signal bar + slippage
            dist = abs(entry - stop)
            cost_unit = round_trip_cost(cfg.segment, entry * cfg.lot, entry * cfg.lot)["total"] / cfg.lot
            if cost_unit + 2 * cfg.slippage_pts > cfg.max_cost_frac * dist:
                continue  # stop too tight relative to friction: edge eaten by costs
            qty = units_for_risk(equity, cfg.risk_pct, dist + cfg.slippage_pts, cost_unit, cfg.lot)
            if qty == 0:
                continue  # one lot already exceeds the risk budget -> no trade
            pos = Trade(str(b.dt.date()), side, str(t), entry, stop, qty=qty)
            trades_today += 1
            sides_used.add(side)
            cfg.trades.append(pos)

        if pos:  # safety: flatten at last bar
            equity, day_r = _close(pos, day[-1], day[-1].c, "eod", cfg, equity, day_r)
    return cfg


def _add_min(t: time, m: int) -> time:
    tot = t.hour * 60 + t.minute + m
    return time(tot // 60, tot % 60)


def _close(pos: Trade, b: Bar, px: float, reason: str, cfg: Config, equity: float, day_r: float):
    px = px - pos.side * cfg.slippage_pts
    buy_px, sell_px = (pos.entry, px) if pos.side > 0 else (px, pos.entry)
    gross = (px - pos.entry) * pos.side * pos.qty
    cost = round_trip_cost(cfg.segment, buy_px * pos.qty, sell_px * pos.qty)["total"]
    pos.exit, pos.exit_time, pos.reason = px, str(b.dt.time()), reason
    pos.pnl = gross - cost
    risk_rs = abs(pos.entry - pos.stop) * pos.qty
    pos.r = pos.pnl / risk_rs if risk_rs else 0.0
    return equity + pos.pnl, day_r + pos.r


# ---------------------------------------------------------------------------
# 4. Statistics
# ---------------------------------------------------------------------------

def stats(cfg: Config) -> Dict[str, float]:
    tr = [t for t in cfg.trades if t.exit_time]
    if not tr:
        return {"trades": 0}
    pnl = [t.pnl for t in tr]
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p <= 0]
    eq, peak, mdd, run, max_run = cfg.capital, cfg.capital, 0.0, 0, 0
    daily: Dict[str, float] = {}
    for t in tr:
        eq += t.pnl
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)
        run = run + 1 if t.pnl <= 0 else 0
        max_run = max(max_run, run)
        daily[t.day] = daily.get(t.day, 0.0) + t.pnl
    drets = [v / cfg.capital for v in daily.values()]
    mu = statistics.mean(drets)
    sd = statistics.pstdev(drets) or float("nan")
    dsd = math.sqrt(sum(min(0, r) ** 2 for r in drets) / len(drets)) or float("nan")
    return {
        "trades": len(tr),
        "win_rate": len(wins) / len(tr),
        "avg_win": statistics.mean(wins) if wins else 0.0,
        "avg_loss": statistics.mean(losses) if losses else 0.0,
        "profit_factor": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else float("inf"),
        "expectancy_rs": statistics.mean(pnl),
        "expectancy_R": statistics.mean(t.r for t in tr),
        "net_pnl": sum(pnl),
        "max_drawdown": mdd,
        "max_consec_losses": max_run,
        # per traded day, annualised with 245 sessions; non-trading days excluded
        "sharpe_traded_days": mu / sd * math.sqrt(245) if sd == sd else float("nan"),
        "sortino_traded_days": mu / dsd * math.sqrt(245) if dsd == dsd else float("nan"),
    }


# ---------------------------------------------------------------------------
# 5. CLI
# ---------------------------------------------------------------------------

def _print_costs():
    print("Round-trip costs (hypothetical prices; verify with your contract note)\n")
    cases = [
        ("Nifty futures, 1 lot (65) @ 25,000", "futures", 65 * 25000, 65),
        ("Bank Nifty futures, 1 lot (30) @ 56,000", "futures", 30 * 56000, 30),
        ("Stock intraday, Rs 2 lakh notional", "equity_intraday", 200000, None),
        ("Stock intraday, Rs 10 lakh notional", "equity_intraday", 1000000, None),
        ("Nifty option, 1 lot @ Rs 150 premium", "options", 65 * 150, 65),
    ]
    for name, seg, val, lot in cases:
        c = round_trip_cost(seg, val, val)
        line = f"{name:42s} total Rs {c['total']:8.2f}  ({c['total'] / val * 100:.4f}% of notional/premium)"
        if lot:
            line += f"  = {c['total'] / lot:.2f} pts/unit"
        print(line)
        print("    " + ", ".join(f"{k} {v:.2f}" for k, v in c.items() if k != "total"))


def _print_risk():
    print("Loss after N consecutive full-risk losses (compounding on shrinking equity)\n")
    caps = [100000, 500000, 1000000, 2500000]
    for rp in (0.0025, 0.005, 0.01, 0.02):
        print(f"Risk per trade {rp * 100:.2f}%")
        for cap in caps:
            cells = []
            for n in (1, 3, 5, 10):
                loss = streak_loss(cap, rp, n)
                cells.append(f"{n:>2}L: Rs {loss:>9,.0f} ({loss / cap * 100:4.1f}%)")
            print(f"  Rs {cap:>10,}  " + " | ".join(cells))
        print()
    print("Gain needed to recover a drawdown:")
    for dd in (0.05, 0.1, 0.2, 0.3, 0.5):
        print(f"  {dd * 100:.0f}% DD -> +{required_gain_to_recover(dd) * 100:.1f}%")
    print("\nProbability of at least one losing streak in 250 trades:")
    for wr in (0.35, 0.45, 0.55):
        cells = [f">={k}: {prob_streak(1 - wr, k, 250):.0%}" for k in (5, 8, 10)]
        print(f"  win rate {wr:.0%}: " + ", ".join(cells))


def _print_ruin():
    print("Monte Carlo, 250 trades, probability of a 20% peak-to-trough drawdown\n")
    for wr, rr in ((0.35, 2.0), (0.40, 1.8), (0.50, 1.2), (0.55, 1.0)):
        exp_r = wr * rr - (1 - wr)
        for rp in (0.005, 0.01, 0.02):
            d = drawdown_odds(wr, rr, rp, 250, 0.20, sims=4000)
            print(f"  WR {wr:.0%} RR {rr:.1f} (E={exp_r:+.2f}R) risk {rp * 100:.1f}%: "
                  f"P(DD>=20%)={d['p_breach']:.1%}, P(net loss)={d['p_loss_overall']:.1%}, "
                  f"median x{d['median_final']:.2f}")


def _synthetic(n_days: int = 300, seed: int = 3) -> List[Bar]:
    rng = random.Random(seed)
    bars, px = [], 25000.0
    d0 = datetime(2025, 1, 1)
    day = 0
    while len(bars) < n_days * 75:
        dt = datetime.fromordinal(d0.toordinal() + day); day += 1
        if dt.weekday() >= 5:
            continue
        px *= 1 + rng.gauss(0, 0.004)  # overnight gap
        drift = 0.0  # pure random walk: any "edge" found here signals a bug or look-ahead
        for k in range(75):
            m = 9 * 60 + 15 + 5 * k
            o = px
            c = o * (1 + drift + rng.gauss(0, 0.0012))
            h = max(o, c) * (1 + abs(rng.gauss(0, 0.0005)))
            l = min(o, c) * (1 - abs(rng.gauss(0, 0.0005)))
            bars.append(Bar(dt.replace(hour=m // 60, minute=m % 60), o, h, l, c, rng.randint(1000, 5000)))
            px = c
    return bars


STRATEGY_NAMES = {"orb": "A  ORB-VWAP Trend", "holygrail": "B  Holy Grail Pullback",
                  "fade": "C  Failed-Breakout Reversal", "nr7": "D  NR7 Volatility Breakout"}


def _write_trades(path: str, trades: List[Trade], strategy: str) -> None:
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["strategy", "day", "side", "entry_time", "entry", "stop", "qty",
                        "exit_time", "exit", "reason", "net_pnl_rs", "r_multiple"])
        for t in trades:
            w.writerow([strategy, t.day, "LONG" if t.side > 0 else "SHORT", t.entry_time,
                        round(t.entry, 2), round(t.stop, 2), t.qty, t.exit_time,
                        round(t.exit, 2), t.reason, round(t.pnl, 2), round(t.r, 3)])


def main(argv: List[str]) -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Indian intraday research toolkit (not investment advice)")
    ap.add_argument("command", nargs="?", default="help",
                    choices=["costs", "risk", "ruin", "selftest", "sample", "backtest", "trades", "help"])
    ap.add_argument("csv", nargs="?", help="5-minute OHLCV CSV (datetime,open,high,low,close,volume)")
    ap.add_argument("--strategy", default="all", choices=["all"] + list(STRATEGY_NAMES))
    ap.add_argument("--instrument", default="nifty", choices=["nifty", "banknifty", "stock"],
                    help="sets lot size, cost segment and default slippage")
    ap.add_argument("--capital", type=float, default=1000000.0)
    ap.add_argument("--risk", type=float, default=0.5, help="risk per trade in %% of equity (default 0.5)")
    ap.add_argument("--slippage", type=float, default=None, help="per side, in price points/rupees")
    ap.add_argument("--out", default="trades_out.csv", help="CSV file for the trades command")
    a = ap.parse_args(argv[1:])

    if a.command == "costs":
        _print_costs(); return
    if a.command == "risk":
        _print_risk(); return
    if a.command == "ruin":
        _print_ruin(); return
    if a.command == "sample":
        path = a.csv or "sample_data/synthetic_5min.csv"
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["datetime", "open", "high", "low", "close", "volume"])
            for b in _synthetic(120):
                w.writerow([b.dt.isoformat(sep=" "), round(b.o, 2), round(b.h, 2),
                            round(b.l, 2), round(b.c, 2), int(b.v)])
        print(f"Wrote SYNTHETIC random-walk data to {path} (format example only, not market data)")
        return
    if a.command == "help":
        ap.print_help(); print(__doc__); return

    presets = {"nifty": (65, "futures", 1.0), "banknifty": (30, "futures", 2.0),
               "stock": (1, "equity_intraday", 0.1)}
    lot, seg, slip = presets[a.instrument]
    if a.command == "selftest":
        bars = _synthetic()
    else:
        if not a.csv:
            ap.error("give a CSV path, e.g. run.sh backtest my_nifty_5min.csv")
        bars = load_csv(a.csv)
    strategies = list(STRATEGY_NAMES) if a.strategy == "all" else [a.strategy]
    if a.command == "trades" and os.path.exists(a.out):
        os.remove(a.out)
    for s in strategies:
        cfg = Config(capital=a.capital, risk_pct=a.risk / 100, lot=lot, segment=seg,
                     slippage_pts=a.slippage if a.slippage is not None else slip)
        if a.command == "selftest":
            cfg.capital = 2500000.0
        cfg = run_backtest(bars, s, cfg)
        res = stats(cfg)
        print(f"{STRATEGY_NAMES[s]:30s}", {k: (round(v, 3) if isinstance(v, float) else v)
                                          for k, v in res.items()})
        if res.get("trades", 0) == 0:
            print("    0 trades: no setup qualified, OR one lot's risk exceeded your budget / the "
                  "cost filter. Try --instrument stock, a larger --capital, or check data.")
        if a.command == "trades":
            _write_trades(a.out, [t for t in cfg.trades if t.exit_time], s)
    if a.command == "trades":
        print(f"Trade list written to {a.out}")
    if a.command == "selftest":
        print("(synthetic random-walk data: every strategy should LOSE roughly its costs)")


if __name__ == "__main__":
    main(sys.argv)
