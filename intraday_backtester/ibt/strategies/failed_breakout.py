"""Strategy E - Failed breakout reversal (mean reversion).

Research justification: Raschke's "Turtle Soup" (fade failed N-period breakouts),
Larry Williams' "Oops" (gap beyond yesterday's extreme that fails) and Fisher's
failed "A" / "C" reversal. Defined objectively:

  Short: price trades above yesterday's high by >= breach_atr x ATR, then within
         `window_minutes` a candle CLOSES back below yesterday's high.
  Stop : today's high so far + buffer.   Target: VWAP at the signal.
  Skip : if VWAP is less than min_rr x risk away, or the 15-min ADX shows a strong trend.
  Mirror image for longs at yesterday's low.
"""

import numpy as np
import pandas as pd

from .base import BaseStrategy, Signals


class FailedBreakout(BaseStrategy):
    name = "failed_breakout"
    title = "Failed Breakout Reversal (Turtle Soup / Oops)"
    research_basis = "Raschke Turtle Soup; Williams Oops; Fisher failed-A (playbook strategy C)"
    default_params = {
        "breach_atr": 0.1,
        "window_minutes": 30,
        "stop_buffer_atr": 0.05,
        "min_rr": 1.5,
        "adx_tf": 15,
        "adx_max": 30.0,           # no fades when 15-min ADX >= this (0 = off)
        "entry_start": "09:20",
        "entry_cutoff": "14:30",
        "target_r": 0.0,           # target is VWAP, not an R multiple
        "max_hold_minutes": 60,
    }

    def generate_signal(self, f):
        p = self.p
        atr = f.atr
        idx = np.arange(f.n, dtype=float)
        big = np.inf
        b = float(p["breach_atr"]) * atr
        up_breach = f.high >= f.prev_high + b
        dn_breach = f.low <= f.prev_low - b
        first_up = pd.Series(np.where(up_breach, idx, big)).groupby(f.day).cummin().to_numpy()
        first_dn = pd.Series(np.where(dn_breach, idx, big)).groupby(f.day).cummin().to_numpy()
        w = int(p["window_minutes"])
        min_up = pd.Series(np.where(up_breach, f.minute, 10_000)).groupby(f.day).cummin().to_numpy()
        min_dn = pd.Series(np.where(dn_breach, f.minute, 10_000)).groupby(f.day).cummin().to_numpy()
        ok = np.isfinite(atr) & np.isfinite(f.prev_high)
        if float(p["adx_max"]) > 0:
            adx = f.tf_map(int(p["adx_tf"]), f.tf_adx(int(p["adx_tf"])))
            ok &= ~(adx >= float(p["adx_max"]))
        short = ok & (first_up < big) & (f.minute - min_up <= w) & f.first_cross_below(f.close, f.prev_high)
        short |= ok & (first_up == idx) & (f.close < f.prev_high)    # poke and fail inside one candle
        long = ok & (first_dn < big) & (f.minute - min_dn <= w) & f.first_cross_above(f.close, f.prev_low)
        long |= ok & (first_dn == idx) & (f.close > f.prev_low)
        rr = np.abs(f.close - f.vwap) / np.maximum(atr, 1e-9)
        return Signals(long=long, short=short, order_type="market", score=np.nan_to_num(rr, nan=0.0))

    def calculate_stop(self, f, i, side, fill):
        buf = float(self.p["stop_buffer_atr"]) * f.atr[i]
        return f.day_high[i] + buf if side == -1 else f.day_low[i] - buf

    def calculate_target(self, f, i, side, fill, stop):
        return f.vwap[i]

    def accept_fill(self, f, i, j, side, fill):
        stop = self.calculate_stop(f, i, side, fill)
        tgt = f.vwap[i]
        risk = abs(fill - stop)
        reward = side * (tgt - fill)
        return risk > 0 and reward >= float(self.p["min_rr"]) * risk
