"""Strategy F - NR7 volatility breakout (Crabel 1990 contraction -> expansion; Williams volatility breakout).

Only on days after an NR7 day (yesterday's range was the narrowest of the last 7).
Levels = today's open +/- level_mult x yesterday's range. Enter on the first candle
CLOSE beyond a level, stop = nearer of stop_atr x ATR or the opposite level, exit at
square-off, stop to breakeven at +1R.
"""

import numpy as np

from .base import BaseStrategy, Signals


class NR7Breakout(BaseStrategy):
    name = "nr7_breakout"
    title = "NR7 Volatility Breakout"
    research_basis = "Toby Crabel NR7 (1990); Larry Williams volatility breakout (1999)"
    default_params = {
        "level_mult": 0.5,
        "stop_atr": 0.5,
        "require_inside_day": False,
        "max_gap_atr": 1.0,        # skip if today's gap > x ATR
        "entry_start": "09:20",
        "entry_cutoff": "12:00",
        "target_r": 0.0,
        "breakeven_r": 1.0,
    }

    def generate_signal(self, f):
        p = self.p
        day_ok = f.d_nr7.copy()
        if p["require_inside_day"]:
            day_ok &= f.d_inside
        ok = day_ok[f.day] & np.isfinite(f.atr)
        ok &= np.abs(f.day_open - f.prev_close) <= float(p["max_gap_atr"]) * f.atr
        rng = f.prev_range * float(p["level_mult"])
        self._up = f.day_open + rng
        self._dn = f.day_open - rng
        long = ok & f.first_cross_above(f.close, self._up)
        short = ok & f.first_cross_below(f.close, self._dn)
        mom = np.abs(f.ret_open_pct)
        return Signals(long=long, short=short, order_type="market", score=np.nan_to_num(mom, nan=0.0))

    def calculate_stop(self, f, i, side, fill):
        d = float(self.p["stop_atr"]) * f.atr[i]
        if side == 1:
            return max(fill - d, self._dn[i])
        return min(fill + d, self._up[i])
