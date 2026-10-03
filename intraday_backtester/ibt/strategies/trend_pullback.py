"""Strategy C - Trend pullback ("Holy Grail", Raschke & Connors, Street Smarts 1995), intraday version.

Trend    : ADX on 15-min candles >= adx_min and the 5-min EMA20 sloping in the trade direction.
Pullback : a completed 5-min candle whose low (high for shorts) touches the 5-min EMA20.
           Only the first `max_pullbacks` touches since the trend qualified (per day) count.
Trigger  : buy-stop above the touch candle's high, valid for `trigger_valid_minutes`,
           cancelled if price first breaks the touch candle's low.
Stop     : touch candle's low - buffer.  Target: R multiple.
"""

import numpy as np

from .base import BaseStrategy, Signals


class TrendPullback(BaseStrategy):
    name = "trend_pullback"
    title = "Trend Pullback (Holy Grail)"
    research_basis = "Linda Raschke - Holy Grail (ADX > 30, first pullback to 20 EMA), Street Smarts (1995)"
    default_params = {
        "tf_minutes": 5,
        "ema_len": 20,
        "adx_tf": 15,
        "adx_len": 14,
        "adx_min": 30.0,           # Raschke's daily-chart value; intraday value must be tested
        "slope_bars": 3,           # EMA higher than N tf-candles ago = up-trend
        "max_pullbacks": 2,
        "trigger_valid_minutes": 15,
        "stop_buffer_atr": 0.02,
        "entry_start": "09:45",
        "entry_cutoff": "14:30",
        "target_r": 2.0,
        "max_trades_per_day": 2,
    }

    def generate_signal(self, f):
        p = self.p
        tf = int(p["tf_minutes"])
        t = f.tf(tf)
        ema = f.tf_ema(tf, int(p["ema_len"]))
        adx_1m = f.tf_map(int(p["adx_tf"]), f.tf_adx(int(p["adx_tf"]), int(p["adx_len"])))
        end_idx = t["end"] - 1                     # 1-min candle completing each tf candle
        adx = adx_1m[end_idx]                      # ADX known when the tf candle completes
        sb = int(p["slope_bars"])
        prev_ema = np.r_[np.full(sb, np.nan), ema[:-sb]]
        up = (ema > prev_ema) & (adx >= float(p["adx_min"]))
        dn = (ema < prev_ema) & (adx >= float(p["adx_min"]))
        touch_up = up & (t["low"] <= ema)
        touch_dn = dn & (t["high"] >= ema)
        n_tf = len(ema)
        ok_up = np.zeros(n_tf, bool)
        ok_dn = np.zeros(n_tf, bool)
        maxpb = int(p["max_pullbacks"])
        cu = cd = 0
        prev_tu = prev_td = False
        tday = t["day"]
        for k in range(n_tf):
            if k == 0 or tday[k] != tday[k - 1] or not (adx[k] >= float(p["adx_min"])):
                cu = cd = 0
                prev_tu = prev_td = False
            if touch_up[k] and not prev_tu:
                cu += 1
                ok_up[k] = cu <= maxpb
            if touch_dn[k] and not prev_td:
                cd += 1
                ok_dn[k] = cd <= maxpb
            prev_tu, prev_td = bool(touch_up[k]), bool(touch_dn[k])
        n = f.n
        long = np.zeros(n, bool)
        short = np.zeros(n, bool)
        lvl_l = np.full(n, np.nan)
        lvl_s = np.full(n, np.nan)
        can_l = np.full(n, np.nan)
        can_s = np.full(n, np.nan)
        atr = f.atr
        buf = float(p["stop_buffer_atr"])
        self._stop_l = np.full(n, np.nan)
        self._stop_s = np.full(n, np.nan)
        iu = end_idx[ok_up]
        long[iu] = True
        lvl_l[iu] = t["high"][ok_up]
        can_l[iu] = t["low"][ok_up]
        self._stop_l[iu] = t["low"][ok_up] - buf * atr[iu]
        idn = end_idx[ok_dn]
        short[idn] = True
        lvl_s[idn] = t["low"][ok_dn]
        can_s[idn] = t["high"][ok_dn]
        self._stop_s[idn] = t["high"][ok_dn] + buf * atr[idn]
        return Signals(long=long, short=short, order_type="stop", level_long=lvl_l, level_short=lvl_s,
                       cancel_long=can_l, cancel_short=can_s, valid_minutes=int(p["trigger_valid_minutes"]),
                       score=np.nan_to_num(adx_1m, nan=0.0))

    def calculate_stop(self, f, i, side, fill):
        return self._stop_l[i] if side == 1 else self._stop_s[i]
