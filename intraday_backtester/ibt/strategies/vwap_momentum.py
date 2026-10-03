"""Strategy B - VWAP momentum (trend-of-day filter + range breakout + volume).

VWAP is NOT central to any of the five researched traders; it is included because
of institutional-execution relevance and Zarattini, Barbon & Aziz (2024), who use
VWAP as a trailing stop. Treat as a hypothesis to test.
"""

import numpy as np

from .base import BaseStrategy, Signals


def _edge(f, cond):
    prev = np.r_[False, cond[:-1]]
    prev[f.day_start] = False
    return cond & ~prev


class VWAPMomentum(BaseStrategy):
    name = "vwap_momentum"
    title = "VWAP Momentum"
    research_basis = "VWAP trend filter/trail (Zarattini, Barbon & Aziz 2024); breakout + volume confirmation"
    default_params = {
        "tf_minutes": 5,           # timeframe of the EMA trend filter
        "ema_fast": 9,
        "ema_slow": 21,
        "lookback": 15,            # breakout of the highest high of the previous N one-minute candles
        "volume_mult": 1.2,        # cumulative volume today >= x * normal by this time (0 = off)
        "vwap_buffer_pct": 0.0,    # close must be this % beyond VWAP
        "stop_mode": "swing",      # swing (lowest low of last N candles) | atr
        "stop_atr": 0.5,
        "swing_buffer_atr": 0.05,
        "min_stop_atr": 0.1,       # stop at least this x ATR away (avoid noise-tight stops)
        "entry_start": "09:45",
        "entry_cutoff": "14:00",
        "target_r": 2.0,
        "trailing": "vwap",        # exit on a close back across VWAP
        "max_trades_per_day": 2,
    }

    def generate_signal(self, f):
        p = self.p
        tf = int(p["tf_minutes"])
        ef = f.tf_map(tf, f.tf_ema(tf, int(p["ema_fast"])))
        es = f.tf_map(tf, f.tf_ema(tf, int(p["ema_slow"])))
        n = int(p["lookback"])
        hh, ll = f.rolling_high_before(n), f.rolling_low_before(n)
        b = float(p["vwap_buffer_pct"]) / 100
        vm = float(p["volume_mult"])
        vol_ok = np.nan_to_num(f.rvol_cum >= vm, nan=0).astype(bool) if vm > 0 else np.ones(f.n, bool)
        ok = np.isfinite(f.atr) & vol_ok
        long = ok & (f.close > f.vwap * (1 + b)) & (ef > es) & (f.close > hh)
        short = ok & (f.close < f.vwap * (1 - b)) & (ef < es) & (f.close < ll)
        return Signals(long=_edge(f, long), short=_edge(f, short), order_type="market",
                       score=np.nan_to_num(f.rvol_cum, nan=0.0))

    def calculate_stop(self, f, i, side, fill):
        p, atr = self.p, f.atr[i]
        n = int(p["lookback"])
        if p["stop_mode"] == "swing":
            buf = float(p["swing_buffer_atr"]) * atr
            stop = f.rolling_low_incl(n)[i] - buf if side == 1 else f.rolling_high_incl(n)[i] + buf
        else:
            stop = fill - side * float(p["stop_atr"]) * atr
        min_d = float(p["min_stop_atr"]) * atr
        if abs(fill - stop) < min_d or side * (fill - stop) <= 0:
            stop = fill - side * min_d
        return stop
