"""Strategy I - Opening-gap reversal (Williams "Oops" family, mean reversion).

Gap up of gap_min..gap_max %: after the opening range, a close below the range low
-> short, stop above today's high, target = yesterday's close (gap fill).
Mirror for gap downs. Skipped if the target is less than min_rr x risk away.
"""

import numpy as np

from .base import BaseStrategy, Signals


class GapReversal(BaseStrategy):
    name = "gap_reversal"
    title = "Gap Reversal (gap fill)"
    research_basis = "Larry Williams 'Oops' gap reversal; failed-breakout logic (playbook strategy C)"
    default_params = {
        "gap_min_pct": 1.0,
        "gap_max_pct": 4.0,
        "or_minutes": 15,
        "min_rr": 1.0,
        "stop_buffer_atr": 0.05,
        "entry_start": "09:30",
        "entry_cutoff": "11:00",
        "target_r": 0.0,
    }

    def generate_signal(self, f):
        p = self.p
        orh, orl = f.opening_range(int(p["or_minutes"]))
        g = f.gap_pct
        up = (g >= float(p["gap_min_pct"])) & (g <= float(p["gap_max_pct"]))
        dn = (g <= -float(p["gap_min_pct"])) & (g >= -float(p["gap_max_pct"]))
        ok = np.isfinite(f.atr) & np.isfinite(orh)
        short = ok & up & f.first_cross_below(f.close, orl)
        long = ok & dn & f.first_cross_above(f.close, orh)
        return Signals(long=long, short=short, order_type="market", score=np.nan_to_num(np.abs(g), nan=0.0))

    def calculate_stop(self, f, i, side, fill):
        buf = float(self.p["stop_buffer_atr"]) * f.atr[i]
        return f.day_high[i] + buf if side == -1 else f.day_low[i] - buf

    def calculate_target(self, f, i, side, fill, stop):
        return f.prev_close[i]

    def accept_fill(self, f, i, j, side, fill):
        stop = self.calculate_stop(f, i, side, fill)
        risk = abs(fill - stop)
        return risk > 0 and side * (f.prev_close[i] - fill) >= float(self.p["min_rr"]) * risk
