"""Strategy H - Previous-day high / low breakout with volume (market-structure breakout)."""

import numpy as np

from .base import BaseStrategy, Signals


class PDHBreakout(BaseStrategy):
    name = "pdh_breakout"
    title = "Previous-Day High/Low Breakout"
    research_basis = "Market-structure levels (Raschke, Williams, Fisher pivots); volume confirmation"
    default_params = {
        "buffer_pct": 0.0,
        "volume_mult": 1.5,
        "stop_atr": 0.5,
        "entry_start": "09:30",
        "entry_cutoff": "13:00",
        "target_r": 2.0,
        "trailing": "vwap",
    }

    def generate_signal(self, f):
        p = self.p
        b = float(p["buffer_pct"]) / 100
        vm = float(p["volume_mult"])
        vol_ok = np.nan_to_num(f.rvol_cum >= vm, nan=0).astype(bool) if vm > 0 else np.ones(f.n, bool)
        ok = np.isfinite(f.atr) & vol_ok
        up_lvl, dn_lvl = f.prev_high * (1 + b), f.prev_low * (1 - b)
        long = ok & (f.day_open < up_lvl) & f.first_cross_above(f.close, up_lvl) & (f.close > f.vwap)
        short = ok & (f.day_open > dn_lvl) & f.first_cross_below(f.close, dn_lvl) & (f.close < f.vwap)
        return Signals(long=long, short=short, order_type="market", score=np.nan_to_num(f.rvol_cum, nan=0.0))

    def calculate_stop(self, f, i, side, fill):
        return fill - side * float(self.p["stop_atr"]) * f.atr[i]
