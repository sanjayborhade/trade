"""
orb_first_candle.py -- backtest of the "first 5-minute candle breakout" strategy
on a universe of stocks (e.g. Nifty 500). Standard library only.

RULES (as given, with the assumptions needed to make them testable):
  Setup    : the first 5-min candle (09:15-09:20) sets the range.
             It stays valid only while its low is still the day's low and its
             high is still the day's high (i.e. neither side broken yet).
  Filter   : day gain vs previous close is between --min-gain and --max-gain
             (default 3% to 10%), measured AT THE MOMENT OF ENTRY.
  Entry    : buy-stop when price crosses above the first candle's high
             (fill at the high + 1 tick, or at the bar open if it gaps above),
             between 09:20 and --last-entry (default 14:30).
  Stop     : the first candle's low (fill at the worse of stop and bar open).
  Exit     : stop, or square-off at --flat (default 15:10).  [assumption: no
             target was specified]
  Ambiguity: if the entry candle also trades below the stop, it is counted as
             entered AND stopped (worst case).
  One trade per stock per day; long only.

    python3 tools/orb_first_candle.py --data data --daily data/daily --start 2026-09-01
    python3 tools/orb_first_candle.py --data data --variants      # also test exit variants

Not investment advice.
"""

import argparse
import contextlib
import csv
import glob
import io
import os
import statistics
import sys
from datetime import date, time
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import intraday_lab as L  # noqa: E402

TICK = 0.05


def load_daily_closes(path: str) -> Dict[date, float]:
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            k = {x.lower(): v for x, v in r.items()}
            try:
                out[date.fromisoformat((k.get("date") or k.get("datetime"))[:10])] = float(k["close"])
            except (KeyError, ValueError, TypeError):
                continue
    return out


def simulate_stock(sym: str, days: List[List[L.Bar]], prev_close: Dict[date, float], a) -> List[dict]:
    trades = []
    closes_by_day = {d[0].dt.date(): d[-1].c for d in days}
    ordered = sorted(closes_by_day)
    for di, day in enumerate(days):
        d = day[0].dt.date()
        if (a.start and d < a.start) or (a.end and d > a.end) or di == 0:
            continue
        first = day[0]
        if first.dt.time() != time(9, 15):
            continue  # missing first candle -> cannot define the setup
        pc = prev_close.get(max((x for x in prev_close if x < d), default=None)) if prev_close else None
        if pc is None:
            pc = closes_by_day[ordered[ordered.index(d) - 1]]
        orh, orl = first.h, first.l
        trig = orh + TICK
        pos = None
        for b in day[1:]:
            t = b.dt.time()
            if pos is None:
                if t > a.last_entry:
                    break
                broke_high = b.h >= trig
                broke_low = b.l < orl
                if not broke_high:
                    if broke_low:
                        break  # day low moved below the candle low -> setup void
                    continue
                raw = max(trig, b.o)
                gain = raw / pc - 1
                if not (a.min_gain <= gain * 100 <= a.max_gain):
                    break  # high broken outside the gain band -> setup gone for the day
                entry = raw * (1 + a.slip)
                risk_ps = entry - orl
                if risk_ps <= 0:
                    break
                qty = int(a.risk_rs / risk_ps)
                qty = min(qty, int(a.max_notional / entry))
                if qty <= 0:
                    break
                pos = {"sym": sym, "day": str(d), "entry_time": str(t), "entry": entry, "stop": orl,
                       "qty": qty, "risk_ps": risk_ps, "gain_at_entry": round(gain * 100, 2),
                       "or_pct": round((orh - orl) / orl * 100, 2), "be": False}
                # same-bar ambiguity: entry candle also below stop -> worst case
                if broke_low:
                    _exit(pos, min(b.o, orl), b, "stop(same bar)", a)
                    trades.append(pos); pos = None
                    break
                continue
            # manage
            if t >= a.flat:
                _exit(pos, b.o, b, "time_15:10", a); break
            if b.l <= pos["stop"]:
                _exit(pos, min(b.o, pos["stop"]), b, "stop" if not pos["be"] else "breakeven", a); break
            if a.variant == "target2R" and b.h >= pos["entry"] + 2 * pos["risk_ps"]:
                _exit(pos, max(b.o, pos["entry"] + 2 * pos["risk_ps"]), b, "target_2R", a); break
            if a.variant == "be1R" and not pos["be"] and b.h >= pos["entry"] + pos["risk_ps"]:
                pos["stop"], pos["be"] = pos["entry"] * (1 + 2 * a.slip + 0.0006), True
        else:
            if pos:
                _exit(pos, day[-1].c, day[-1], "eod", a)
        if pos and "exit" in pos:
            trades.append(pos)
    return trades


def _exit(pos: dict, raw: float, b: L.Bar, why: str, a) -> None:
    px = raw * (1 - a.slip)
    q = pos["qty"]
    cost = L.round_trip_cost("equity_intraday", pos["entry"] * q, px * q)["total"]
    pos.update(exit=px, exit_time=str(b.dt.time()), reason=why, costs=cost,
               pnl=(px - pos["entry"]) * q - cost)
    pos["r"] = pos["pnl"] / (pos["risk_ps"] * q)


def summarize(tr: List[dict], label: str) -> Dict[str, float]:
    if not tr:
        print(f"  {label:42s} no trades"); return {}
    pn = [t["pnl"] for t in tr]
    wins = [p for p in pn if p > 0]
    loss = [p for p in pn if p <= 0]
    pf = sum(wins) / -sum(loss) if loss and sum(loss) < 0 else float("inf")
    gross = sum(t["pnl"] + t["costs"] for t in tr)
    print(f"  {label:42s} {len(tr):5d} {100 * len(wins) / len(tr):5.0f}% {sum(pn):>11,.0f} "
          f"{sum(t['r'] for t in tr):>8.1f} {statistics.mean(t['r'] for t in tr):>6.2f} {pf:>5.2f} "
          f"{gross:>11,.0f} {sum(t['costs'] for t in tr):>9,.0f}")
    return {"n": len(tr), "net": sum(pn)}


def account_view(tr: List[dict], max_per_day: int, max_losses: int) -> List[dict]:
    """First N signals each day by entry time; stop for the day after N losses."""
    out, by_day = [], {}
    for t in sorted(tr, key=lambda x: (x["day"], x["entry_time"], x["sym"])):
        by_day.setdefault(t["day"], []).append(t)
    for d, lst in by_day.items():
        taken, losses = 0, 0
        for t in lst:
            if taken >= max_per_day or losses >= max_losses:
                break
            out.append(t); taken += 1
            losses += t["pnl"] <= 0
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="data", help="folder of <SYMBOL>_5m.csv files")
    ap.add_argument("--daily", default=None, help="folder of <SYMBOL>_1d.csv (official prev close); "
                    "default <data>/daily, falls back to the previous day's last 5-min close")
    ap.add_argument("--start"); ap.add_argument("--end")
    ap.add_argument("--min-gain", type=float, default=3.0)
    ap.add_argument("--max-gain", type=float, default=10.0)
    ap.add_argument("--last-entry", default="14:30")
    ap.add_argument("--flat", default="15:10")
    ap.add_argument("--capital", type=float, default=500000)
    ap.add_argument("--risk", type=float, default=0.5, help="%% of capital risked per trade")
    ap.add_argument("--max-notional", type=float, default=None, help="per trade, default = capital")
    ap.add_argument("--slippage", type=float, default=0.05, help="%% per side (default 0.05)")
    ap.add_argument("--max-per-day", type=int, default=3)
    ap.add_argument("--variants", action="store_true", help="also test target-2R and breakeven-at-1R exits")
    ap.add_argument("--out", default="orb_first_candle_trades.csv")
    a = ap.parse_args()
    a.start = date.fromisoformat(a.start) if a.start else None
    a.end = date.fromisoformat(a.end) if a.end else None
    a.last_entry = time.fromisoformat(a.last_entry)
    a.flat = time.fromisoformat(a.flat)
    a.slip = a.slippage / 100
    a.risk_rs = a.capital * a.risk / 100
    a.max_notional = a.max_notional or a.capital
    daily_dir = a.daily or os.path.join(a.data, "daily")

    files = sorted(glob.glob(os.path.join(a.data, "*_5m.csv")))
    files = [f for f in files if not os.path.basename(f).startswith(("NIFTY_", "BANKNIFTY_"))]
    if not files:
        raise SystemExit(f"No *_5m.csv files in {a.data}. Run: python3 tools/fetch_yahoo.py --nifty500")
    print(f"Universe: {len(files)} stocks | window {a.start or 'all'} to {a.end or 'all'} | gain band "
          f"{a.min_gain}-{a.max_gain}% | risk Rs {a.risk_rs:,.0f}/trade, max notional Rs {a.max_notional:,.0f}"
          f" | slippage {a.slippage}%/side\n")
    loaded = []
    for fpath in files:
        sym = os.path.basename(fpath)[:-7]
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                bars = L.load_csv(fpath)
            except SystemExit:
                continue
        loaded.append((sym, L.group_days(bars), load_daily_closes(os.path.join(daily_dir, f"{sym}_1d.csv"))))

    variants = ["rules"] + (["target2R", "be1R"] if a.variants else [])
    labels = {"rules": "Your rules (stop or 15:10 exit)", "target2R": "Variant: + target at 2R",
              "be1R": "Variant: + stop to breakeven at +1R"}
    hdr = (f"  {'':42s} {'Trades':>5s} {'Win%':>5s} {'Net Rs':>11s} {'TotalR':>8s} {'AvgR':>6s} "
           f"{'PF':>5s} {'Gross Rs':>11s} {'Costs':>9s}")
    for v in variants:
        a.variant = v
        allt = []
        for sym, days, pcs in loaded:
            allt += simulate_stock(sym, days, pcs, a)
        print(labels[v]); print(hdr)
        summarize(allt, "every signal (all stocks, independent)")
        acct = account_view(allt, a.max_per_day, 3)
        summarize(acct, f"account: first {a.max_per_day}/day, stop after 3 losses")
        if v == "rules" and allt:
            ndays = len({t['day'] for t in allt})
            print(f"  signals on {ndays} days; reasons: " + ", ".join(
                f"{r} {sum(t['reason'] == r for t in allt)}" for r in sorted({t['reason'] for t in allt})))
            with open(a.out, "w", newline="") as f:
                w = csv.writer(f)
                cols = ["day", "sym", "entry_time", "entry", "stop", "qty", "gain_at_entry", "or_pct",
                        "exit_time", "exit", "reason", "costs", "pnl", "r"]
                w.writerow(cols)
                for t in sorted(allt, key=lambda x: (x["day"], x["entry_time"])):
                    w.writerow([round(t[c], 2) if isinstance(t[c], float) else t[c] for c in cols])
            print(f"  all trades written to {a.out}")
        print()


if __name__ == "__main__":
    main()
