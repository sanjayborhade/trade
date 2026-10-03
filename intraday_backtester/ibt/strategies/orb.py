"""Strategy A - Opening Range Breakout (Crabel 1990; Fisher ACD; Zarattini & Aziz 2023)."""

import numpy as np

from .base import BaseStrategy, Signals


class OpeningRangeBreakout(BaseStrategy):
    name = "orb"
    title = "Opening Range Breakout"
    research_basis = ("Toby Crabel (1990) ORB + contraction; Mark Fisher ACD (opening range + offset); "
                      "Zarattini & Aziz (2023, SSRN 4416622) 5-min ORB on US stocks")
    default_params = {
        "or_minutes": 15,          # opening range length: 5 / 15 / 30
        "entry_type": "stop",      # stop  = stop orders at the range, placed when the range completes (OCO)
                                   # close = a candle CLOSES beyond the range (+buffer) -> market at next open
        "buffer_atr": 0.0,         # breakout buffer as a fraction of daily ATR
        "volume_mult": 0.0,        # 0 = off. close-entry: breakout candle volume >= x * normal for that minute
                                   #          stop-entry: volume during the range >= x * normal
        "stop_mode": "or",         # or (other side of range) | or_mid | atr
        "stop_atr": 1.0,           # used when stop_mode = atr
        "max_stop_atr": 1.0,       # stop never further than this x ATR (0 = no cap)
        "min_or_atr": 0.0,         # skip day if range narrower than x ATR (0 = off)
        "max_or_atr": 0.0,         # skip day if range wider than x ATR (0 = off)
        "entry_start": "09:15",
        "entry_cutoff": "11:30",
        "target_r": 2.0,
    }

    def generate_signal(self, f):
        p = self.p
        n = int(p["or_minutes"])
        orh, orl = f.opening_range(n)
        atr = f.atr
        buf = float(p["buffer_atr"]) * atr
        width = orh - orl
        ok = np.isfinite(orh) & np.isfinite(atr) & (width > 0)
        if p["min_or_atr"]:
            ok &= width >= float(p["min_or_atr"]) * atr
        if p["max_or_atr"]:
            ok &= width <= float(p["max_or_atr"]) * atr
        vm = float(p["volume_mult"])
        self._orh, self._orl = orh, orl
        if p["entry_type"] == "stop":
            at_end = np.arange(f.n) == f.opening_range_end_index(n)
            vol_ok = (f.rvol_cum >= vm) if vm > 0 else np.ones(f.n, bool)
            sig = at_end & ok & np.nan_to_num(vol_ok, nan=0).astype(bool)
            return Signals(long=sig, short=sig.copy(), order_type="stop", level_long=orh + buf,
                           level_short=orl - buf, valid_minutes=10_000,
                           last_fill_minute=self.entry_cutoff_min + 1,
                           score=np.nan_to_num(f.rvol_cum, nan=0.0))
        if p["entry_type"] != "close":
            raise ValueError("orb.entry_type must be stop | close")
        after = f.minute >= n
        vol_ok = (f.rvol_bar >= vm) if vm > 0 else np.ones(f.n, bool)
        vol_ok = np.nan_to_num(vol_ok, nan=0).astype(bool)
        long = f.first_cross_above(f.close, orh + buf) & after & ok & vol_ok
        short = f.first_cross_below(f.close, orl - buf) & after & ok & vol_ok
        return Signals(long=long, short=short, order_type="market", score=np.nan_to_num(f.rvol_bar, nan=0.0))

    def calculate_stop(self, f, i, side, fill):
        p, atr = self.p, f.atr[i]
        mode = p["stop_mode"]
        if mode == "or":
            stop = self._orl[i] if side == 1 else self._orh[i]
        elif mode == "or_mid":
            stop = (self._orl[i] + self._orh[i]) / 2
        elif mode == "atr":
            stop = fill - side * float(p["stop_atr"]) * atr
        else:
            raise ValueError("orb.stop_mode must be or | or_mid | atr")
        cap = float(p["max_stop_atr"])
        if cap > 0 and np.isfinite(atr) and abs(fill - stop) > cap * atr:
            stop = fill - side * cap * atr
        return stop
