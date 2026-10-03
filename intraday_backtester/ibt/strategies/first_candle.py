"""Strategy G - First-candle breakout of strong stocks (user's strategy).

Setup : the first `candle_minutes` candle. It stays valid while its low is still the
        day's low (a lower low cancels the pending order).
Filter: gain vs previous close between min_gain_pct and max_gain_pct AT THE FILL.
Entry : buy-stop at the candle high (+ trade-through), until entry_cutoff.
Stop  : candle low.  Exit: square-off (no target unless target_r > 0).
Mirror short side available with direction: both (loss of -min..-max %).
"""

import numpy as np

from .base import BaseStrategy, Signals


class FirstCandleBreakout(BaseStrategy):
    name = "first_candle_breakout"
    title = "First-Candle Breakout (3-10 % gainers)"
    research_basis = "User-supplied rules; opening-range breakout family (Crabel)"
    default_params = {
        "candle_minutes": 5,
        "min_gain_pct": 3.0,
        "max_gain_pct": 10.0,
        "direction": "long",
        "entry_start": "09:15",
        "entry_cutoff": "14:30",
        "target_r": 0.0,
    }

    def generate_signal(self, f):
        n = int(self.p["candle_minutes"])
        h, l = f.opening_range(n)
        self._h, self._l = h, l
        at_end = (np.arange(f.n) == f.opening_range_end_index(n)) & np.isfinite(h) & np.isfinite(f.prev_close)
        tick = 0.05
        return Signals(long=at_end, short=at_end.copy(), order_type="stop", level_long=h, level_short=l,
                       cancel_long=l - tick, cancel_short=h + tick, valid_minutes=10_000,
                       last_fill_minute=self.entry_cutoff_min + 1,
                       score=np.nan_to_num(np.abs(f.gap_pct), nan=0.0))

    def accept_fill(self, f, i, j, side, fill):
        g = (fill / f.prev_close[i] - 1) * 100 * side
        return float(self.p["min_gain_pct"]) <= g <= float(self.p["max_gain_pct"])

    def calculate_stop(self, f, i, side, fill):
        return self._l[i] if side == 1 else self._h[i]
