"""Strategy D - Intraday relative strength vs. a benchmark (Nifty 50 / Nifty 500 / equal-weight universe).

RS = stock return since today's open - benchmark return since today's open (both at the
same candle close). Long the strongest stocks when they make a new day high above VWAP;
short the weakest at a new day low below VWAP.
"""

import numpy as np

from .base import BaseStrategy, Signals


class RelativeStrength(BaseStrategy):
    name = "relative_strength"
    title = "Relative Strength Momentum"
    research_basis = ("Relative strength for stock selection (playbook section 2.1); intraday momentum "
                      "(Gao, Han, Li & Zhou 2018, JFE)")
    needs_benchmark = True
    default_params = {
        "benchmark": "auto",       # auto (NIFTY500 > NIFTY50 > UNIVERSE_EW) | NIFTY50 | NIFTY500 | UNIVERSE_EW
        "rs_min_pct": 1.0,         # stock must beat the benchmark by >= x percentage points since the open
        "volume_mult": 1.0,
        "stop_atr": 0.75,
        "entry_start": "09:45",
        "entry_cutoff": "12:00",
        "target_r": 2.0,
        "trailing": "vwap",
    }

    def generate_signal(self, f):
        p = self.p
        bench = f.bench_ret(p["benchmark"])
        rs = f.ret_open_pct - bench
        vm = float(p["volume_mult"])
        vol_ok = np.nan_to_num(f.rvol_cum >= vm, nan=0).astype(bool) if vm > 0 else np.ones(f.n, bool)
        ok = np.isfinite(rs) & np.isfinite(f.atr) & vol_ok
        thr = float(p["rs_min_pct"])
        long = ok & (rs >= thr) & (f.close > f.vwap) & (f.close > f.day_high_before)
        short = ok & (rs <= -thr) & (f.close < f.vwap) & (f.close < f.day_low_before)
        return Signals(long=long, short=short, order_type="market", score=np.nan_to_num(np.abs(rs), nan=0.0))

    def calculate_stop(self, f, i, side, fill):
        return fill - side * float(self.p["stop_atr"]) * f.atr[i]
