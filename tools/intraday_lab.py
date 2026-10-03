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
from datetime import date, datetime, time, timedelta, timezone
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
    side: int              # +1 long, -1 short
    entry_time: str
    entry: float
    stop: float            # initial stop
    qty: int = 0
    exit_time: str = ""
    exit: float = 0.0      # quantity-weighted average exit price
    reason: str = ""
    pnl: float = 0.0       # net of costs and slippage, rupees
    costs: float = 0.0
    r: float = 0.0         # net P&L / initial price risk
    open_qty: int = 0
    exit_value: float = 0.0


@dataclass
class Config:
    """Every default below is the value written in STRATEGIES.md."""
    capital: float = 500000.0
    risk_pct: float = 0.005              # 0.5% of equity per trade
    lot: int = 65                        # Nifty lot from Jan 2026
    segment: str = "futures"
    slippage_pts: float = 1.0            # per side, in price points
    fixed_lots: int = 0                  # >0: always trade this many lots (ignores risk sizing)
    expiry_rule: str = "none"            # "weekly_tue" (Nifty) / "monthly_last_tue" (Bank Nifty)
    start: Optional[date] = None         # only take trades on/after this date
    end: Optional[date] = None
    or_minutes: int = 15                 # 09:15-09:30 opening range
    flat_time: time = time(15, 10)
    atr_len: int = 14                    # daily ATR length
    max_cost_frac: float = 0.25          # skip if cost + slippage > 25% of price risk
    daily_max_losses: int = 3
    daily_loss_r: float = 3.0            # -3R = -1.5% at 0.5% risk
    # A  ORB-VWAP
    buffer_atr: float = 0.05
    or_min_atr: float = 0.25
    or_max_atr: float = 0.8
    wide_day_atr: float = 1.5
    gap_skip_atr: float = 1.0
    orb_stop_atr: float = 0.5
    orb_last_entry: time = time(11, 30)
    # B  Holy Grail
    ema_len: int = 20
    adx_len: int = 14
    adx_min: float = 30.0                # on 15-minute bars
    hg_target_r: float = 2.0
    hg_max_pullbacks: int = 2
    hg_last_entry: time = time(14, 30)
    # C  Failed breakout
    fade_breach_atr: float = 0.1
    fade_window_bars: int = 6            # 30 minutes
    fade_stop_buf_atr: float = 0.05
    fade_min_rr: float = 1.5
    fade_time_stop_bars: int = 12        # 1 hour
    fade_last_entry: time = time(14, 30)
    # D  NR7
    nr_lookback: int = 7
    vol_mult: float = 0.5
    nr7_stop_atr: float = 0.5
    nr7_last_entry: time = time(12, 0)
    expiry_last_entry: time = time(12, 0)  # B and C: no expiry-day afternoon entries
    trades: List[Trade] = field(default_factory=list)
    skipped: Dict[str, int] = field(default_factory=dict)


_ALIASES = {
    "datetime": ["datetime", "date_time", "timestamp", "time_stamp", "date", "datetime_ist", "time"],
    "open": ["open", "o", "open_price"],
    "high": ["high", "h", "high_price"],
    "low": ["low", "l", "low_price"],
    "close": ["close", "c", "close_price", "ltp", "last"],
    "volume": ["volume", "vol", "v", "qty", "traded_qty", "volume_traded"],
}
_DT_FORMATS = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M",
               "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M",
               "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M",
               "%Y%m%d %H:%M:%S", "%Y%m%d %H:%M"]
_IST = timezone(timedelta(hours=5, minutes=30))


def _norm(name: str) -> str:
    return name.strip().lstrip("\ufeff").strip('"').lower().replace(" ", "_")


def _parse_dt(text: str) -> datetime:
    t = text.strip().strip('"')
    if t.isdigit() and len(t) >= 10:  # unix epoch seconds or milliseconds
        sec = int(t) / (1000 if len(t) >= 13 else 1)
        return datetime.fromtimestamp(sec, _IST).replace(tzinfo=None)
    try:
        dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        dt = None
        for fmt in _DT_FORMATS:
            try:
                dt = datetime.strptime(t, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            raise ValueError(f"unrecognised date/time format: {text!r}")
    if dt.tzinfo is not None:  # e.g. 2024-01-01T09:15:00+05:30 or UTC -> IST, drop tz
        dt = dt.astimezone(_IST).replace(tzinfo=None)
    return dt


def _num(x: str) -> float:
    return float(x.replace(",", "").strip().strip('"')) if x and x.strip() else 0.0


def load_csv(path: str) -> List[Bar]:
    """Load OHLCV bars. Accepts common header variants (Date/Time/Timestamp,
    Open/High/Low/Close/Volume in any case), separate date + time columns,
    ISO / dd-mm-yyyy / epoch timestamps, tz-aware times (converted to IST)
    and ignores extra columns such as indicators. 1-minute data is
    resampled to 5-minute automatically."""
    if not os.path.isfile(path):
        raise SystemExit(f"\nFile not found (or it is a folder): {path}\n"
                         f"  Check the path; put it in double quotes if it contains spaces.")
    try:
        f = open(path, newline="", encoding="utf-8-sig")
        sample = f.read(4096)
    except PermissionError:
        raise SystemExit(
            f"\nWindows refused to read {path} (Permission denied).\n"
            f"  Most likely the file is OPEN IN EXCEL, which locks CSV files. Close it and re-run.\n"
            f"  Otherwise: make it available offline in Google Drive/OneDrive, or copy it to a local folder.")
    except UnicodeDecodeError:
        f = open(path, newline="", encoding="latin-1")
        sample = f.read(4096)
    with f:
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(f, dialect)
        header = next(reader)
        cols = {_norm(h): i for i, h in enumerate(header)}

        def find(key: str) -> Optional[int]:
            for a in _ALIASES[key]:
                if a in cols:
                    return cols[a]
            return None

        idx = {k: find(k) for k in ("open", "high", "low", "close", "volume")}
        date_i = cols.get("date")
        time_i = cols.get("time")
        dt_i = None
        for a in ("datetime", "date_time", "timestamp", "time_stamp", "datetime_ist"):
            if a in cols:
                dt_i = cols[a]
                break
        if dt_i is None and date_i is None and time_i is not None:
            dt_i = time_i  # single "time" column holding full timestamps
        missing = [k for k in ("open", "high", "low", "close") if idx[k] is None]
        if missing or (dt_i is None and date_i is None):
            raise SystemExit(
                f"\nCould not find required columns in {path}.\n"
                f"  Columns found : {header}\n"
                f"  Need          : a date/time column (datetime, timestamp, date [+ time]) "
                f"and open, high, low, close (volume optional).\n"
                f"  Missing       : {missing + ([] if dt_i is not None or date_i is not None else ['datetime'])}\n"
                f"  Rename the headers in Excel, or ask for an alias to be added.")

        out: List[Bar] = []
        bad = 0
        for row in reader:
            if not row or all(not c.strip() for c in row):
                continue
            try:
                if dt_i is not None:
                    dt = _parse_dt(row[dt_i])
                elif time_i is not None:
                    dt = _parse_dt(f"{row[date_i].strip()} {row[time_i].strip()}")
                else:
                    dt = _parse_dt(row[date_i])
                vol = _num(row[idx["volume"]]) if idx["volume"] is not None else 0.0
                out.append(Bar(dt, _num(row[idx["open"]]), _num(row[idx["high"]]),
                               _num(row[idx["low"]]), _num(row[idx["close"]]), vol))
            except (ValueError, IndexError):
                bad += 1
        if bad:
            print(f"  note: skipped {bad} unreadable row(s) in {path}")
    if not out:
        raise SystemExit(f"No usable rows in {path}.")
    out.sort(key=lambda b: b.dt)
    # keep regular NSE session only
    out = [b for b in out if time(9, 15) <= b.dt.time() < time(15, 30)]
    if not out:
        raise SystemExit("No rows between 09:15 and 15:30 IST. Are timestamps in IST (or tz-aware)?")
    step = _bar_minutes(out)
    if step >= 24 * 60 or step == 0:
        raise SystemExit("This looks like DAILY data. The backtester needs intraday (1- or 5-minute) candles.")
    if step < 5:
        print(f"  note: {step}-minute data detected -> resampled to 5-minute bars")
        out = _resample(out, 5)
    elif step > 5:
        print(f"  note: {step}-minute bars detected. Strategies were designed for 5-minute bars; "
              f"results will differ.")
    print(f"  loaded {len(out):,} bars, {out[0].dt:%Y-%m-%d} to {out[-1].dt:%Y-%m-%d}")
    return out


def _bar_minutes(bars: List[Bar]) -> int:
    diffs = {}
    for a, b in zip(bars, bars[1:]):
        if a.dt.date() == b.dt.date():
            m = int((b.dt - a.dt).total_seconds() // 60)
            diffs[m] = diffs.get(m, 0) + 1
    return max(diffs, key=diffs.get) if diffs else 24 * 60


def _resample(bars: List[Bar], minutes: int) -> List[Bar]:
    out: List[Bar] = []
    for b in bars:
        mins = (b.dt.hour * 60 + b.dt.minute - 555) // minutes * minutes + 555  # anchor 09:15
        key = b.dt.replace(hour=mins // 60, minute=mins % 60, second=0, microsecond=0)
        if out and out[-1].dt == key:
            o = out[-1]
            o.h, o.l, o.c, o.v = max(o.h, b.h), min(o.l, b.l), b.c, o.v + b.v
        else:
            out.append(Bar(key, b.o, b.h, b.l, b.c, b.v))
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


def _adx15(bars: List[Bar], n: int):
    """15-minute bars built from 5-minute bars (anchored 09:15) and, for every
    5-minute bar, the index of the latest COMPLETED 15-minute bar."""
    b15: List[Bar] = []
    keys: List[tuple] = []
    done = [-1] * len(bars)
    last_done = -1
    for i, b in enumerate(bars):
        m = b.dt.hour * 60 + b.dt.minute - 555
        key = (b.dt.date(), m // 15)
        if keys and keys[-1] == key:
            x = b15[-1]
            x.h, x.l, x.c, x.v = max(x.h, b.h), min(x.l, b.l), b.c, x.v + b.v
        else:
            last_done = max(last_done, len(b15) - 1)   # previous slot is complete
            keys.append(key)
            b15.append(Bar(b.dt, b.o, b.h, b.l, b.c, b.v))
        if m % 15 == 10:                               # third 5-min bar closes the slot
            last_done = len(b15) - 1
        done[i] = last_done
    return adx(b15, n), done


def _is_expiry(d: date, rule: str) -> bool:
    if rule == "weekly_tue":
        return d.weekday() == 1
    if rule == "monthly_last_tue":
        return d.weekday() == 1 and (d + timedelta(days=7)).month != d.month
    return False


def run_backtest(bars: List[Bar], strategy: str, cfg: Config) -> Config:
    """Bar-by-bar simulation of one strategy (orb / holygrail / fade / nr7) with
    the rules in STRATEGIES.md. Signals use CLOSED bars only; entry fills at the
    signal bar's close plus slippage; stops fill at the worse of stop and open."""
    days = group_days(bars)
    ema_all = ema([b.c for b in bars], cfg.ema_len)
    adx15, done15 = _adx15(bars, cfg.adx_len)
    equity = cfg.capital
    sk = cfg.skipped

    seen: set = set()

    def skip(reason: str) -> None:  # counted once per day per reason
        key = (cur_day[0], reason)
        if key not in seen:
            seen.add(key)
            sk[reason] = sk.get(reason, 0) + 1

    cur_day = [None]

    def adx_at(gi: int) -> Optional[float]:
        k = done15[gi]
        return adx15[k] if k >= 2 * cfg.adx_len else None

    def adx_falling3(gi: int) -> bool:
        k = done15[gi]
        return k >= 3 and adx15[k] < adx15[k - 1] < adx15[k - 2] < adx15[k - 3]

    gstart = 0
    for di, day in enumerate(days):
        base = gstart
        gstart += len(day)
        atr = daily_atr(days, di, cfg.atr_len)
        d = day[0].dt.date()
        cur_day[0] = d
        if atr is None or (cfg.start and d < cfg.start) or (cfg.end and d > cfg.end):
            continue
        prev = days[di - 1]
        p_hi, p_lo, p_c = max(b.h for b in prev), min(b.l for b in prev), prev[-1].c
        p_rng = p_hi - p_lo
        rngs = [max(b.h for b in x) - min(b.l for b in x) for x in days[di - cfg.nr_lookback:di]]
        is_nr7 = len(rngs) == cfg.nr_lookback and p_rng <= min(rngs)
        day_open = day[0].o
        gap = abs(day_open - p_c)
        expiry = _is_expiry(d, cfg.expiry_rule)
        or_end = _add_min(time(9, 15), cfg.or_minutes)
        or_bars = [b for b in day if b.dt.time() < or_end]
        if not or_bars:
            continue
        or_hi, or_lo = max(b.h for b in or_bars), min(b.l for b in or_bars)
        or_w = or_hi - or_lo

        # ---- day-level permissions
        allowed = True
        if strategy == "orb":
            if expiry:
                allowed = False; skip("A: expiry day")
            elif gap > cfg.gap_skip_atr * atr:
                allowed = False; skip("A: gap > 1 ATR")
            elif p_rng >= cfg.wide_day_atr * atr:
                allowed = False; skip("A: yesterday wide-range day")
            elif not (cfg.or_min_atr * atr <= or_w <= cfg.or_max_atr * atr):
                allowed = False; skip("A: OR width outside 0.25-0.8 ATR")
        elif strategy == "nr7":
            if not is_nr7:
                allowed = False
            elif gap > cfg.gap_skip_atr * atr:
                allowed = False; skip("D: NR7 but gap > 1 ATR")
        if not allowed:
            continue

        lvl_up, lvl_dn = day_open + cfg.vol_mult * p_rng, day_open - cfg.vol_mult * p_rng
        cum_pv = cum_v = 0.0
        vwap_prev = None
        pos: Optional[Trade] = None
        st: Dict[str, float] = {}
        n_trades, losses, day_r = 0, 0, 0.0
        sides_used: set = set()
        breach_up = breach_dn = None
        day_hi, day_lo = -math.inf, math.inf
        pb_up = pb_dn = 0

        def finish(px_raw: float, qty: int, b: Bar, why: str) -> None:
            nonlocal equity, day_r, losses, pos
            px = px_raw - pos.side * cfg.slippage_pts
            buy, sell = (pos.entry, px) if pos.side > 0 else (px, pos.entry)
            cost = round_trip_cost(cfg.segment, buy * qty, sell * qty)["total"]
            net = (px - pos.entry) * pos.side * qty - cost
            pos.pnl += net; pos.costs += cost; equity += net
            pos.open_qty -= qty; pos.exit_value += px * qty
            pos.reason = why if not pos.reason else pos.reason + "+" + why
            if pos.open_qty == 0:
                pos.exit_time = str(b.dt.time())
                pos.exit = pos.exit_value / pos.qty
                risk = abs(pos.entry - pos.stop) * pos.qty
                pos.r = pos.pnl / risk if risk else 0.0
                day_r += pos.r
                losses += pos.pnl <= 0
                pos = None

        for j, b in enumerate(day):
            gi = base + j
            t = b.dt.time()
            w = b.v if b.v > 0 else 1.0           # index data has no volume -> TWAP
            cum_pv += (b.h + b.l + b.c) / 3 * w
            cum_v += w
            vwap = cum_pv / cum_v                  # known at this bar's close

            # ---------------- manage open position
            if pos:
                side = pos.side
                if t >= cfg.flat_time:
                    finish(b.o, pos.open_qty, b, "time_15:10")
                else:
                    stop = st["stop"]
                    if (side > 0 and b.l <= stop) or (side < 0 and b.h >= stop):
                        px = min(b.o, stop) if side > 0 else max(b.o, stop)
                        finish(px, pos.open_qty, b, "stop" if not st.get("be") else "breakeven_stop")
                    elif strategy == "holygrail" and not st.get("partial") and (
                            (side > 0 and b.h >= st["target"]) or (side < 0 and b.l <= st["target"])):
                        lots = pos.open_qty // cfg.lot
                        if lots >= 2:
                            finish(st["target"], (lots // 2) * cfg.lot, b, "half_at_2R")
                            st["partial"] = True
                        else:
                            finish(st["target"], pos.open_qty, b, "target_2R(1 lot)")
                    elif strategy == "fade" and vwap_prev is not None and (
                            (side > 0 and b.h >= vwap_prev) or (side < 0 and b.l <= vwap_prev)):
                        finish(vwap_prev, pos.open_qty, b, "target_vwap")
                    elif strategy == "orb" and ((side > 0 and b.c < vwap) or (side < 0 and b.c > vwap)):
                        finish(b.c, pos.open_qty, b, "vwap_trail")
                    elif strategy == "holygrail" and st.get("partial") and (
                            (side > 0 and b.c < ema_all[gi]) or (side < 0 and b.c > ema_all[gi])):
                        finish(b.c, pos.open_qty, b, "ema_trail")
                    elif strategy == "fade" and j - st["j"] >= cfg.fade_time_stop_bars:
                        finish(b.c, pos.open_qty, b, "time_stop_1h")
                    # breakeven at +1R (A, D) -- effective from the next bar
                    if pos and strategy in ("orb", "nr7") and not st.get("be"):
                        r1 = pos.entry + side * st["risk"]
                        if (side > 0 and b.h >= r1) or (side < 0 and b.l <= r1):
                            st["stop"] = pos.entry + side * (st["cost_unit"] + cfg.slippage_pts)
                            st["be"] = True

            # ---------------- state that must update every bar
            prev_touch_up = gi > 0 and bars[gi - 1].l <= ema_all[gi - 1]
            prev_touch_dn = gi > 0 and bars[gi - 1].h >= ema_all[gi - 1]
            a15 = adx_at(gi)
            if strategy == "fade":
                if breach_up is None and b.h >= p_hi + cfg.fade_breach_atr * atr:
                    breach_up = j
                if breach_dn is None and b.l <= p_lo - cfg.fade_breach_atr * atr:
                    breach_dn = j
            day_hi, day_lo = max(day_hi, b.h), min(day_lo, b.l)

            # ---------------- entries
            side, stop, target = 0, 0.0, 0.0
            if pos is None and t < cfg.flat_time and n_trades < 2 \
                    and losses < cfg.daily_max_losses and day_r > -cfg.daily_loss_r:
                if strategy == "orb" and or_end <= t <= cfg.orb_last_entry:
                    buf = cfg.buffer_atr * atr
                    if b.c > or_hi + buf and b.c > vwap and 1 not in sides_used:
                        side, stop = 1, max(or_lo, b.c - cfg.orb_stop_atr * atr)
                    elif b.c < or_lo - buf and b.c < vwap and -1 not in sides_used:
                        side, stop = -1, min(or_hi, b.c + cfg.orb_stop_atr * atr)
                elif strategy == "holygrail" and or_end <= t <= (
                        cfg.expiry_last_entry if expiry else cfg.hg_last_entry):
                    if a15 is not None and a15 >= cfg.adx_min and not adx_falling3(gi) and gi >= 6:
                        pb = bars[gi - 1]
                        up = ema_all[gi - 1] > ema_all[gi - 6]
                        if up and prev_touch_up and 1 <= pb_up <= cfg.hg_max_pullbacks and b.c > pb.h:
                            side, stop = 1, pb.l
                        elif not up and prev_touch_dn and 1 <= pb_dn <= cfg.hg_max_pullbacks and b.c < pb.l:
                            side, stop = -1, pb.h
                elif strategy == "fade" and n_trades == 0 and time(9, 20) <= t <= (
                        cfg.expiry_last_entry if expiry else cfg.fade_last_entry):
                    trending = a15 is not None and a15 >= cfg.adx_min
                    if not trending:
                        if breach_up is not None and j - breach_up <= cfg.fade_window_bars and b.c < p_hi:
                            side, stop, target = -1, day_hi + cfg.fade_stop_buf_atr * atr, vwap
                        elif breach_dn is not None and j - breach_dn <= cfg.fade_window_bars and b.c > p_lo:
                            side, stop, target = 1, day_lo - cfg.fade_stop_buf_atr * atr, vwap
                        if side and (target - b.c) * side < cfg.fade_min_rr * abs(stop - b.c):
                            skip("C: VWAP < 1.5R away"); side = 0
                elif strategy == "nr7" and n_trades == 0 and time(9, 20) <= t <= cfg.nr7_last_entry:
                    if b.c > lvl_up:
                        side, stop = 1, max(b.c - cfg.nr7_stop_atr * atr, lvl_dn)
                    elif b.c < lvl_dn:
                        side, stop = -1, min(b.c + cfg.nr7_stop_atr * atr, lvl_up)

            if side and (stop - b.c) * side < 0:
                entry = b.c + side * cfg.slippage_pts
                dist = abs(entry - stop)
                cost_unit = round_trip_cost(cfg.segment, entry * cfg.lot, entry * cfg.lot)["total"] / cfg.lot
                if cost_unit + 2 * cfg.slippage_pts > cfg.max_cost_frac * dist:
                    skip(f"{strategy}: stop too tight for costs")
                else:
                    qty = cfg.fixed_lots * cfg.lot if cfg.fixed_lots else units_for_risk(
                        equity, cfg.risk_pct, dist + cfg.slippage_pts, cost_unit, cfg.lot)
                    if qty == 0:
                        skip(f"{strategy}: 1 lot exceeds risk budget")
                    else:
                        pos = Trade(str(d), side, str(t), entry, stop, qty=qty, open_qty=qty)
                        st = {"stop": stop, "risk": dist, "cost_unit": cost_unit, "j": j,
                              "target": entry + side * cfg.hg_target_r * dist}
                        cfg.trades.append(pos)
                        n_trades += 1
                        sides_used.add(side)

            # pullback episode counting (Holy Grail): reset when ADX < 30 or at day start
            if strategy == "holygrail":
                if a15 is None or a15 < cfg.adx_min:
                    pb_up = pb_dn = 0
                else:
                    if b.l <= ema_all[gi] and not prev_touch_up:
                        pb_up += 1
                    if b.h >= ema_all[gi] and not prev_touch_dn:
                        pb_dn += 1
            vwap_prev = vwap

        if pos:  # data ended before 15:10
            finish(day[-1].c, pos.open_qty, day[-1], "eod")
    return cfg


def _add_min(t: time, m: int) -> time:
    tot = t.hour * 60 + t.minute + m
    return time(tot // 60, tot % 60)


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
        "total_R": sum(t.r for t in tr),
        "costs_paid": sum(t.costs for t in tr),
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
    ap.add_argument("--start", help="only trade from this date, YYYY-MM-DD (earlier data warms up ATR/ADX)")
    ap.add_argument("--end", help="only trade until this date, YYYY-MM-DD")
    ap.add_argument("--lots", type=int, default=0,
                    help="always trade exactly N lots (ignores --capital/--risk sizing)")
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

    presets = {"nifty": (65, "futures", 1.0, "weekly_tue"),
               "banknifty": (30, "futures", 2.0, "monthly_last_tue"),
               "stock": (1, "equity_intraday", 0.1, "none")}
    lot, seg, slip, expiry_rule = presets[a.instrument]
    if a.command == "selftest":
        bars = _synthetic()
    else:
        if not a.csv:
            ap.error("give a CSV path, e.g. run.sh backtest my_nifty_5min.csv")
        bars = load_csv(a.csv)
    start = date.fromisoformat(a.start) if a.start else None
    end = date.fromisoformat(a.end) if a.end else None
    strategies = list(STRATEGY_NAMES) if a.strategy == "all" else [a.strategy]
    if a.command == "trades" and os.path.exists(a.out):
        os.remove(a.out)
    sizing = f"fixed {a.lots} lot(s)" if a.lots else f"capital Rs {a.capital:,.0f}, risk {a.risk}%/trade"
    print(f"  instrument={a.instrument} lot={lot} slippage={a.slippage if a.slippage is not None else slip}"
          f"/side, {sizing}, window={start or 'all'} to {end or 'all'}\n")
    print(f"  {'Strategy':28s} {'Trades':>6s} {'Win%':>6s} {'Net Rs':>10s} {'Total R':>8s} "
          f"{'Exp R':>7s} {'PF':>6s} {'MaxDD':>6s} {'Costs Rs':>9s}")
    all_trades = []
    for s in strategies:
        cfg = Config(capital=a.capital, risk_pct=a.risk / 100, lot=lot, segment=seg,
                     slippage_pts=a.slippage if a.slippage is not None else slip,
                     fixed_lots=a.lots, expiry_rule=expiry_rule, start=start, end=end)
        if a.command == "selftest":
            cfg.capital = 2500000.0
        cfg = run_backtest(bars, s, cfg)
        r = stats(cfg)
        if r.get("trades", 0) == 0:
            print(f"  {STRATEGY_NAMES[s]:28s} {0:>6d}   no qualifying trades")
        else:
            pf = r["profit_factor"]
            print(f"  {STRATEGY_NAMES[s]:28s} {r['trades']:>6d} {r['win_rate'] * 100:>5.0f}% "
                  f"{r['net_pnl']:>10,.0f} {r['total_R']:>8.2f} {r['expectancy_R']:>7.2f} "
                  f"{(f'{pf:.2f}' if pf != float('inf') else 'inf'):>6s} {r['max_drawdown'] * 100:>5.1f}% "
                  f"{r['costs_paid']:>9,.0f}")
        if cfg.skipped:
            print("      filtered out (days): " + "; ".join(f"{k} x{v}" for k, v in sorted(cfg.skipped.items())))
        done = [t for t in cfg.trades if t.exit_time]
        all_trades += [(s, t) for t in done]
        if a.command == "trades":
            _write_trades(a.out, done, s)
    if all_trades:
        tot = sum(t.pnl for _, t in all_trades)
        print(f"\n  ALL STRATEGIES: {len(all_trades)} trades, net Rs {tot:,.0f}, "
              f"total {sum(t.r for _, t in all_trades):.2f}R")
    if a.command == "trades":
        print(f"  Trade list written to {a.out}")
    if a.command == "selftest":
        print("  (synthetic random-walk data: every strategy should LOSE roughly its costs)")


if __name__ == "__main__":
    main(sys.argv)
