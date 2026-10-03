"""Stage 1: turn one strategy's signals on one stock into candidate trades.

For every signal (evaluated at a candle's close) the simulator places the order,
finds the fill on a LATER candle (see execution.py), then walks forward through
that day's candles to the first exit event. Candidate trades are per-share
economics; capital, quantity and portfolio limits are applied in stage 2
(portfolio.py), which is why the per-stock work can run in parallel.

Exit events, in the order they are resolved within one candle:
  at the OPEN of a candle : square-off time, max-holding time, strategy/trailing exits
                            signalled at the previous close
  inside the candle       : stop-loss (incl. trailing / breakeven stop) and target;
                            if both, `same_bar_stop_target` decides (stop first by default)
"""

from __future__ import annotations

from collections import Counter
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from ..config import parse_hhmm
from ..features import Frame
from ..strategies.base import BaseStrategy, Signals
from .costs import CostModel
from .execution import ExecutionModel

INF = 1 << 30


def _first(mask: np.ndarray) -> int:
    if mask.size == 0:
        return INF
    k = int(np.argmax(mask))
    return k if mask[k] else INF


class Simulator:
    def __init__(self, cfg: dict):
        self.exe = ExecutionModel(cfg)
        self.costs = CostModel(cfg["costs"])
        self.sq_min = parse_hhmm(cfg["execution"]["square_off_time"]) - 555
        self.validity = int(cfg["execution"]["order_validity_minutes"])
        self.friction = self.costs.friction_pct()

    # ------------------------------------------------------------------ public
    def run(self, f: Frame, strat: BaseStrategy, day_ok: np.ndarray) -> Tuple[pd.DataFrame, Counter]:
        diag: Counter = Counter()
        sig = strat.generate_signal(f)
        self._score_arr = sig.score
        L, S = strat.sides(np.asarray(sig.long, bool), np.asarray(sig.short, bool))
        ok = strat.window(f) & day_ok[f.day] & (f.minute < self.sq_min)
        L, S = L & ok, S & ok
        idx = np.flatnonzero(L | S)
        diag["signals"] = int(len(idx))
        rows: List[dict] = []
        if len(idx) == 0:
            return pd.DataFrame(rows), diag
        maxpd = int(strat.p.get("max_trades_per_day", 1))
        days = f.day[idx]
        cuts = np.flatnonzero(np.diff(days)) + 1
        for grp in np.split(idx, cuts):
            d = f.day[grp[0]]
            e = int(f.day_end[d])
            next_free, taken = 0, 0
            for i in grp:
                if i < next_free:
                    continue
                if taken >= maxpd:
                    break
                sides = ([1] if L[i] else []) + ([-1] if S[i] else [])
                fill, end_idx, status = self._enter(f, sig, int(i), e, sides)
                diag[status] += 1
                if fill is None:
                    next_free = end_idx
                    continue
                side, j, raw, otype = fill
                if not strat.accept_fill(f, int(i), j, side, raw):
                    diag["rejected_by_strategy_at_fill"] += 1
                    next_free = j + 1
                    continue
                stop = float(strat.calculate_stop(f, int(i), side, raw))
                if not np.isfinite(stop) or stop <= 0:
                    diag["invalid_stop"] += 1
                    next_free = j + 1
                    continue
                target = float(strat.calculate_target(f, int(i), side, raw, stop))
                row, k = self._manage(f, strat, sig, side, int(i), j, e, raw, stop, target, otype)
                rows.append(row)
                taken += 1
                next_free = k
        df = pd.DataFrame(rows)
        if len(df):
            df.insert(0, "symbol", f.symbol)
            df.insert(1, "strategy", strat.name)
        diag["trades"] = len(df)
        return df, diag

    # ------------------------------------------------------------------ entry
    def _enter(self, f: Frame, sig: Signals, i: int, e: int, sides: List[int]):
        j0 = i + 1
        if j0 >= e:
            return None, e, "no_next_candle"
        last_fill = self.sq_min if sig.last_fill_minute is None else min(self.sq_min, sig.last_fill_minute)
        if sig.order_type == "market":
            if len(sides) != 1:
                return None, j0, "ambiguous_both_sides"
            if f.minute[j0] >= last_fill:
                return None, j0, "too_late_to_fill"
            return (sides[0], j0, float(f.open[j0]), "market"), j0, "filled"

        valid = sig.valid_minutes if sig.valid_minutes is not None else self.validity
        end_min = min(int(f.minute[i]) + int(valid), last_fill)
        jend = j0 + int(np.searchsorted(f.minute[j0:e], end_min, side="left"))
        if jend <= j0:
            return None, j0, "order_expired"
        h, l, o = f.high[j0:jend], f.low[j0:jend], f.open[j0:jend]
        thr = self.exe.through
        best = (INF, 0, 0.0)
        cancel_at = INF
        tie = False
        for side in sides:
            lvl = (sig.level_long if side == 1 else sig.level_short)[i]
            if not np.isfinite(lvl):
                continue
            if sig.order_type == "stop":
                trig = h >= lvl + thr if side == 1 else l <= lvl - thr
            else:  # limit
                trig = l <= lvl - thr if side == 1 else h >= lvl + thr
            t = _first(trig)
            canc_arr = sig.cancel_long if side == 1 else sig.cancel_short
            c = INF
            if canc_arr is not None and np.isfinite(canc_arr[i]):
                c = _first(l <= canc_arr[i]) if side == 1 else _first(h >= canc_arr[i])
            if c < t:  # cancelled before it triggered (same candle -> treated as filled: conservative)
                cancel_at = min(cancel_at, c)
                continue
            if t < best[0]:
                best, tie = (t, side, lvl), False
            elif t == best[0] and t < INF:
                tie = True
        t, side, lvl = best
        if t >= INF:
            if cancel_at < INF:
                return None, j0 + cancel_at, "order_cancelled"
            return None, jend, "order_expired"
        if tie:
            return None, j0 + t + 1, "ambiguous_both_sides"
        j = j0 + t
        if sig.order_type == "stop":
            raw = max(o[t], lvl) if side == 1 else min(o[t], lvl)
        else:
            raw = min(o[t], lvl) if side == 1 else max(o[t], lvl)
        return (side, j, float(raw), sig.order_type), j, "filled"

    # ------------------------------------------------------------------ manage
    def _manage(self, f: Frame, strat: BaseStrategy, sig: Signals, side: int, i: int, j: int, e: int,
                entry_raw: float, stop0: float, target: float, otype: str):
        p = strat.p
        exe = self.exe
        entry_px = entry_raw if otype == "limit" else exe.adverse(entry_raw, side, True)
        o, h, l, c = f.open[j:e], f.high[j:e], f.low[j:e], f.close[j:e]
        mins = f.minute[j:e]
        n = e - j

        if side * (entry_raw - stop0) <= 0:   # filled at/through the stop (gap): out immediately
            return self._row(f, strat, side, i, j, j, entry_raw, entry_px, stop0, target, entry_raw,
                             "stop_at_entry", otype, h[:1], l[:1]), j + 1

        # open-time events
        t_sq = _first(mins >= self.sq_min)
        t_hold = INF
        mh = int(p.get("max_hold_minutes") or 0)
        if mh > 0:
            hold = (mins - mins[0]) >= mh
            hold[0] = False
            t_hold = _first(hold)
        cond = np.zeros(n, bool)
        ex = sig.exit_long if side == 1 else sig.exit_short
        if ex is not None:
            cond |= np.asarray(ex[j:e], bool)
        tr = p.get("trailing", "none")
        if tr == "vwap":
            cond |= (c < f.vwap[j:e]) if side == 1 else (c > f.vwap[j:e])
        elif tr == "ema":
            em = f.ema(int(p.get("trail_ema", 20)))[j:e]
            cond |= (c < em) if side == 1 else (c > em)
        t_ind = _first(cond)
        t_ind = t_ind + 1 if t_ind < INF else INF       # signalled at close -> next open
        if t_ind >= n:
            t_ind = INF

        # stop path (only information up to the previous close moves the stop)
        stop_path = np.full(n, stop0)
        if tr == "atr" and np.isfinite(f.atr[j]):
            dist = float(p.get("trail_atr", 0.5)) * f.atr[j]
            if side == 1:
                best = np.maximum.accumulate(c - dist)
                stop_path[1:] = np.maximum(stop0, best[:-1])
            else:
                best = np.minimum.accumulate(c + dist)
                stop_path[1:] = np.minimum(stop0, best[:-1])
        be_r = float(p.get("breakeven_r") or 0)
        if be_r > 0:
            risk = abs(entry_raw - stop0)
            lvl = entry_raw + side * be_r * risk
            be_px = entry_px * (1 + side * self.friction)
            if side == 1:
                reached = np.r_[False, (np.maximum.accumulate(h) >= lvl)[:-1]]
                stop_path = np.where(reached, np.maximum(stop_path, be_px), stop_path)
            else:
                reached = np.r_[False, (np.minimum.accumulate(l) <= lvl)[:-1]]
                stop_path = np.where(reached, np.minimum(stop_path, be_px), stop_path)
        stop_hit = (l <= stop_path) if side == 1 else (h >= stop_path)
        t_stop = _first(stop_hit)
        t_tgt = INF
        if np.isfinite(target):
            hit = (h >= target + exe.through) if side == 1 else (l <= target - exe.through)
            hit[0] = False  # path inside the entry candle unknown: no target on the entry candle
            t_tgt = _first(hit)

        t_open = min(t_sq, t_hold, t_ind)
        t_intra = min(t_stop, t_tgt)
        if t_open < INF and t_open <= t_intra:
            k = t_open
            reason = "square_off" if k == t_sq else ("time_exit" if k == t_hold else
                                                      ("trailing_exit" if tr in ("vwap", "ema") and ex is None
                                                       else "strategy_exit"))
            raw = o[k]
        elif t_intra < INF:
            k = t_intra
            use_stop = t_stop < t_tgt or (t_stop == t_tgt and exe.stop_first)
            if use_stop:
                sp = stop_path[k]
                raw = min(o[k], sp) if side == 1 else max(o[k], sp)
                reason = "stop" if sp == stop0 else ("trailing_stop" if tr == "atr" else "breakeven_stop")
            else:
                raw = max(o[k], target) if side == 1 else min(o[k], target)
                reason = "target"
        else:
            k = n - 1
            raw = c[k]
            reason = "end_of_data_day"
        row = self._row(f, strat, side, i, j, j + k, entry_raw, entry_px, stop0, target, float(raw), reason, otype,
                        h[: k + 1], l[: k + 1])
        return row, j + k

    def _row(self, f: Frame, strat, side, i, j, x, entry_raw, entry_px, stop0, target, exit_raw, reason, otype,
             hs, ls) -> dict:
        exe = self.exe
        exit_px = exit_raw if reason == "target" else exe.adverse(exit_raw, side, False)
        risk = abs(entry_raw - stop0)
        if side == 1:
            mfe, mae = (hs.max() - entry_raw), (ls.min() - entry_raw)
        else:
            mfe, mae = (entry_raw - ls.min()), (entry_raw - hs.max())
        return {
            "side": side, "date": f.dates[f.day[i]],
            "signal_ts": f.ts[i], "entry_ts": f.ts[j], "exit_ts": f.ts[x],
            "entry_raw": entry_raw, "entry_px": entry_px, "stop": stop0,
            "target": target, "exit_raw": exit_raw, "exit_px": exit_px, "exit_reason": reason,
            "order_type": otype, "hold_minutes": int(f.minute[x] - f.minute[j]),
            "entry_minute": int(f.minute[j]), "risk_ps": abs(entry_px - stop0),
            "mfe_r": mfe / risk if risk > 0 else np.nan, "mae_r": mae / risk if risk > 0 else np.nan,
            "gap_pct": f.gap_pct[i], "rvol_cum": f.rvol_cum[i], "atr": f.atr[i],
            "entry_bar_volume": f.volume[j], "prev_close": f.prev_close[i],
            "score": self._score(i),
        }

    def _score(self, i):
        s = self._score_arr
        if s is None:
            return 0.0
        v = float(s[i])
        return v if np.isfinite(v) else 0.0
