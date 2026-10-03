"""Strategy J - VWAP mean reversion  [EXPERIMENTAL].

The earlier research found only weak justification for pure intraday mean
reversion to VWAP, so this strategy is marked experimental and is NOT included
in `--strategy all` unless --include-experimental is given.

Short when price is stretched >= dev_atr x ATR above VWAP, RSI(14, 1-min) >= rsi_extreme,
and the 15-min ADX is low (range day). Target VWAP, stop stop_atr x ATR, 60-minute time stop.
"""

import numpy as np

from .base import BaseStrategy, Signals


class VWAPReversion(BaseStrategy):
    name = "vwap_reversion"
    title = "VWAP Mean Reversion"
    research_basis = "Weak - included only as an experiment (see playbook section 11-E)"
    experimental = True
    default_params = {
        "dev_atr": 0.6,
        "rsi_len": 14,
        "rsi_extreme": 80.0,
        "adx_tf": 15,
        "adx_max": 20.0,
        "stop_atr": 0.3,
        "min_rr": 1.0,
        "entry_start": "10:00",
        "entry_cutoff": "14:30",
        "target_r": 0.0,
        "max_hold_minutes": 60,
    }

    def generate_signal(self, f):
        p = self.p
        dev = (f.close - f.vwap) / f.atr
        rsi = f.rsi(int(p["rsi_len"]))
        adx = f.tf_map(int(p["adx_tf"]), f.tf_adx(int(p["adx_tf"])))
        ok = np.isfinite(dev) & (adx < float(p["adx_max"]))
        x = float(p["rsi_extreme"])
        short = ok & (dev >= float(p["dev_atr"])) & (rsi >= x)
        long = ok & (dev <= -float(p["dev_atr"])) & (rsi <= 100 - x)
        pv = np.r_[False, short[:-1]]
        pl = np.r_[False, long[:-1]]
        return Signals(long=long & ~pl, short=short & ~pv, order_type="market",
                       score=np.nan_to_num(np.abs(dev), nan=0.0))

    def calculate_stop(self, f, i, side, fill):
        return fill - side * float(self.p["stop_atr"]) * f.atr[i]

    def calculate_target(self, f, i, side, fill, stop):
        return f.vwap[i]

    def accept_fill(self, f, i, j, side, fill):
        stop = self.calculate_stop(f, i, side, fill)
        risk = abs(fill - stop)
        return risk > 0 and side * (f.vwap[i] - fill) >= float(self.p["min_rr"]) * risk
