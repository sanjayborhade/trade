"""Per-symbol feature frame.

LOOK-AHEAD RULES (enforced here, tested in tests/test_lookahead.py):
  * A value attached to candle i uses only candles 0..i (it is known at the CLOSE of candle i).
  * Daily features (previous close/high/low, ATR, NR7, averages) use COMPLETED days only
    (shifted by one day).
  * Opening-range levels are NaN until the opening range has finished.
  * Higher-timeframe bars (5/15-min) become visible only on the 1-minute candle that completes them.
  * "Normal volume for this minute" uses the PREVIOUS N days, never today.
  * Strategies evaluate signals at candle close; the engine executes on the NEXT candle.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

SESSION_MINUTES = 375  # 09:15 .. 15:29 one-minute candles


class Frame:
    """Numpy-array view of one symbol plus causal features (computed lazily and cached)."""

    def __init__(self, symbol: str, bars: pd.DataFrame, bench=None,
                 normal_volume_days: int = 20):
        if bars.empty:
            raise ValueError(f"{symbol}: no bars")
        self.symbol = symbol
        ts = pd.to_datetime(bars["ts"])
        self.ts = ts.to_numpy()
        self.open = bars["open"].to_numpy(dtype="float64")
        self.high = bars["high"].to_numpy(dtype="float64")
        self.low = bars["low"].to_numpy(dtype="float64")
        self.close = bars["close"].to_numpy(dtype="float64")
        self.volume = bars["volume"].to_numpy(dtype="float64")
        self.n = len(bars)
        self.minute = (ts.dt.hour * 60 + ts.dt.minute - 555).to_numpy().astype(np.int32)
        codes, uniq = pd.factorize(ts.dt.normalize(), sort=True)
        self.day = codes.astype(np.int32)
        self.dates: List[date] = [d.date() for d in pd.DatetimeIndex(uniq)]
        self.n_days = len(self.dates)
        self.day_start = np.r_[0, np.flatnonzero(np.diff(self.day)) + 1].astype(np.int64)
        self.day_end = np.r_[self.day_start[1:], self.n].astype(np.int64)
        self.pos_in_day = np.arange(self.n) - self.day_start[self.day]
        self.has_volume = bool((self.volume > 0).mean() > 0.5)
        self._cache: Dict[str, object] = {}
        self._daily()
        self._intraday(normal_volume_days)
        self._bench(bench)

    # ------------------------------------------------------------------ daily (completed days)
    def _daily(self):
        s, e = self.day_start, self.day_end
        dO = self.open[s]
        dH = np.maximum.reduceat(self.high, s)
        dL = np.minimum.reduceat(self.low, s)
        dC = self.close[e - 1]
        dV = np.add.reduceat(self.volume, s)
        dVal = np.add.reduceat(self.volume * (self.high + self.low + self.close) / 3, s)
        prevC = np.r_[np.nan, dC[:-1]]
        tr = np.nanmax(np.vstack([dH - dL, np.abs(dH - prevC), np.abs(dL - prevC)]), axis=0)
        rng = dH - dL
        sh = lambda a: np.r_[np.nan, a[:-1]]  # noqa: E731 - value of the previous day
        self.d_open, self.d_high, self.d_low, self.d_close = dO, dH, dL, dC  # FULL days: never use same-day
        self.d_prev_close = prevC
        self.d_prev_high = sh(dH)
        self.d_prev_low = sh(dL)
        self.d_prev_range = sh(rng)
        self.d_atr = pd.Series(tr).rolling(14, min_periods=5).mean().shift(1).to_numpy()
        r7 = pd.Series(rng).rolling(7, min_periods=7).min().to_numpy()
        self.d_nr7 = np.r_[False, (rng[:-1] <= r7[:-1] + 1e-12)]
        self.d_inside = np.r_[False, False, (dH[1:-1] < dH[:-2]) & (dL[1:-1] > dL[:-2])]
        self.d_adv_vol = pd.Series(dV).rolling(20, min_periods=5).mean().shift(1).to_numpy()
        self.d_adv_value = pd.Series(dVal).rolling(20, min_periods=5).mean().shift(1).to_numpy()
        self.d_gap_pct = (dO / prevC - 1) * 100
        self.d_hist_days = np.arange(self.n_days)
        d = self.day
        self.prev_close = self.d_prev_close[d]
        self.prev_high = self.d_prev_high[d]
        self.prev_low = self.d_prev_low[d]
        self.prev_range = self.d_prev_range[d]
        self.atr = self.d_atr[d]
        self.day_open = dO[d]
        self.gap_pct = self.d_gap_pct[d]

    # ------------------------------------------------------------------ intraday running values
    def _intraday(self, nv_days: int):
        d, s = self.day, self.day_start
        v = self.volume if self.has_volume else np.ones(self.n)
        tp = (self.high + self.low + self.close) / 3
        self.cum_vol = _day_cumsum(v, d, s)
        self.vwap = _day_cumsum(tp * v, d, s) / self.cum_vol
        self.ret_open_pct = (self.close / self.day_open - 1) * 100
        hi = pd.Series(self.high).groupby(d).cummax().to_numpy()
        lo = pd.Series(self.low).groupby(d).cummin().to_numpy()
        self.day_high = hi                              # including this candle
        self.day_low = lo
        first = self.pos_in_day == 0
        self.day_high_before = np.where(first, np.nan, np.r_[np.nan, hi[:-1]])   # before this candle
        self.day_low_before = np.where(first, np.nan, np.r_[np.nan, lo[:-1]])
        # normal volume for this minute of the day, from PREVIOUS days only
        m = np.clip(self.minute, 0, SESSION_MINUTES - 1)
        cum = np.full((self.n_days, SESSION_MINUTES), np.nan)
        bar = np.full((self.n_days, SESSION_MINUTES), np.nan)
        cum[d, m] = self.cum_vol if self.has_volume else np.nan
        bar[d, m] = self.volume if self.has_volume else np.nan
        cum = pd.DataFrame(cum).ffill(axis=1).to_numpy()
        avg_cum = pd.DataFrame(cum).rolling(nv_days, min_periods=5).mean().shift(1).to_numpy()
        avg_bar = pd.DataFrame(bar).rolling(nv_days, min_periods=5).mean().shift(1).to_numpy()
        with np.errstate(divide="ignore", invalid="ignore"):
            self.rvol_cum = np.where(avg_cum[d, m] > 0, self.cum_vol / avg_cum[d, m], np.nan)
            self.rvol_bar = np.where(avg_bar[d, m] > 0, self.volume / avg_bar[d, m], np.nan)
        if not self.has_volume:
            self.rvol_cum[:] = np.nan
            self.rvol_bar[:] = np.nan

    def _bench(self, benches):
        """benches: dict name -> DataFrame(ts, open, close) or None. Aligned by timestamp; a benchmark
        value is used only if it is from the same or an earlier minute (backward as-of match)."""
        self.bench_names: List[str] = []
        self._bench_ret: Dict[str, np.ndarray] = {}
        if benches is None:
            return
        if isinstance(benches, pd.DataFrame):
            benches = {"BENCH": benches}
        left = pd.DataFrame({"ts": pd.to_datetime(self.ts)})
        for name, b in benches.items():
            if b is None or b.empty:
                continue
            b = b[["ts", "open", "close"]].sort_values("ts")
            b = b.assign(bts=b["ts"])
            m = pd.merge_asof(left, b, on="ts", direction="backward", tolerance=pd.Timedelta(minutes=5))
            m_day = left["ts"].dt.normalize()
            bopen = b.assign(day=b["ts"].dt.normalize()).groupby("day")["open"].first()
            r = (m["close"].to_numpy() / m_day.map(bopen).to_numpy() - 1) * 100
            # the benchmark's own candle for that minute must belong to the same day
            same_day = (pd.to_datetime(m["bts"]).dt.normalize() == m_day).to_numpy()
            self._bench_ret[name] = np.where(same_day, r, np.nan)
            self.bench_names.append(name)

    def bench_ret(self, name: str = "auto") -> np.ndarray:
        """Benchmark return since today's open (%) at each candle's close, NaN if unavailable."""
        if name == "auto":
            for cand in ("NIFTY500", "NIFTY50", "UNIVERSE_EW"):
                if cand in self._bench_ret:
                    return self._bench_ret[cand]
            return np.full(self.n, np.nan)
        return self._bench_ret.get(name, np.full(self.n, np.nan))

    # ------------------------------------------------------------------ lazily computed helpers
    def opening_range(self, minutes: int) -> Tuple[np.ndarray, np.ndarray]:
        """High/low of the first `minutes` candles; NaN on candles before the range is complete."""
        key = f"or{minutes}"
        if key not in self._cache:
            inside = self.minute < minutes
            h = pd.Series(np.where(inside, self.high, np.nan)).groupby(self.day).transform("max").to_numpy()
            l = pd.Series(np.where(inside, self.low, np.nan)).groupby(self.day).transform("min").to_numpy()
            # known only from the close of the last opening-range candle onwards
            last_or = pd.Series(np.where(inside, np.arange(self.n), -1)).groupby(self.day).transform("max")
            known = np.arange(self.n) >= last_or.to_numpy()
            known &= last_or.to_numpy() >= 0
            self._cache[key] = (np.where(known, h, np.nan), np.where(known, l, np.nan), last_or.to_numpy())
        h, l, _ = self._cache[key]
        return h, l

    def opening_range_end_index(self, minutes: int) -> np.ndarray:
        self.opening_range(minutes)
        return self._cache[f"or{minutes}"][2]

    def rolling_high_before(self, n: int) -> np.ndarray:
        """Highest high of the previous n candles of the same day (excludes the current candle)."""
        key = f"rhb{n}"
        if key not in self._cache:
            r = pd.Series(self.high).rolling(n).max().shift(1).to_numpy()
            self._cache[key] = np.where(self.pos_in_day >= n, r, np.nan)
        return self._cache[key]

    def rolling_low_before(self, n: int) -> np.ndarray:
        key = f"rlb{n}"
        if key not in self._cache:
            r = pd.Series(self.low).rolling(n).min().shift(1).to_numpy()
            self._cache[key] = np.where(self.pos_in_day >= n, r, np.nan)
        return self._cache[key]

    def rolling_low_incl(self, n: int) -> np.ndarray:
        """Lowest low of the last n candles of the same day including the current one."""
        key = f"rli{n}"
        if key not in self._cache:
            r = pd.Series(self.low).groupby(self.day).rolling(n, min_periods=1).min()
            self._cache[key] = r.reset_index(level=0, drop=True).sort_index().to_numpy()
        return self._cache[key]

    def rolling_high_incl(self, n: int) -> np.ndarray:
        key = f"rhi{n}"
        if key not in self._cache:
            r = pd.Series(self.high).groupby(self.day).rolling(n, min_periods=1).max()
            self._cache[key] = r.reset_index(level=0, drop=True).sort_index().to_numpy()
        return self._cache[key]

    def ema(self, n: int) -> np.ndarray:
        key = f"ema{n}"
        if key not in self._cache:
            self._cache[key] = pd.Series(self.close).ewm(span=n, adjust=False).mean().to_numpy()
        return self._cache[key]

    def rsi(self, n: int = 14) -> np.ndarray:
        key = f"rsi{n}"
        if key not in self._cache:
            d = np.diff(self.close, prepend=self.close[0])
            up = pd.Series(np.clip(d, 0, None)).ewm(alpha=1 / n, adjust=False).mean()
            dn = pd.Series(np.clip(-d, 0, None)).ewm(alpha=1 / n, adjust=False).mean()
            with np.errstate(divide="ignore", invalid="ignore"):
                rs = up / dn
            self._cache[key] = (100 - 100 / (1 + rs)).fillna(50).to_numpy()
        return self._cache[key]

    # ------------------------------------------------------------------ higher timeframe (causal)
    def tf(self, minutes: int) -> dict:
        """Resample to `minutes` candles anchored at 09:15 and map back to 1-minute candles.

        Returns dict with tf arrays and `done`: for every 1-minute candle the index of the
        latest tf candle that is COMPLETE at that candle's close (-1 if none)."""
        key = f"tf{minutes}"
        if key not in self._cache:
            bucket = self.day.astype(np.int64) * 1000 + self.minute // minutes
            starts = np.r_[0, np.flatnonzero(np.diff(bucket)) + 1]
            ends = np.r_[starts[1:], self.n]
            o = self.open[starts]
            h = np.maximum.reduceat(self.high, starts)
            l = np.minimum.reduceat(self.low, starts)
            c = self.close[ends - 1]
            done = np.full(self.n, -1, dtype=np.int64)
            done[ends - 1] = np.arange(len(starts))
            done = np.maximum.accumulate(done)
            self._cache[key] = {"open": o, "high": h, "low": l, "close": c, "start": starts, "end": ends,
                                "day": self.day[starts], "done": done}
        return self._cache[key]

    def tf_map(self, minutes: int, values: np.ndarray) -> np.ndarray:
        t = self.tf(minutes)
        idx = t["done"]
        out = np.full(self.n, np.nan)
        ok = idx >= 0
        out[ok] = values[idx[ok]]
        return out

    def tf_ema(self, minutes: int, n: int) -> np.ndarray:
        key = f"tfema{minutes}_{n}"
        if key not in self._cache:
            t = self.tf(minutes)
            self._cache[key] = pd.Series(t["close"]).ewm(span=n, adjust=False).mean().to_numpy()
        return self._cache[key]

    def tf_adx(self, minutes: int, n: int = 14) -> np.ndarray:
        key = f"tfadx{minutes}_{n}"
        if key not in self._cache:
            t = self.tf(minutes)
            h, l, c = t["high"], t["low"], t["close"]
            pc = np.r_[c[0], c[:-1]]
            tr = np.maximum.reduce([h - l, np.abs(h - pc), np.abs(l - pc)])
            up = np.r_[0, h[1:] - h[:-1]]
            dn = np.r_[0, l[:-1] - l[1:]]
            pdm = np.where((up > dn) & (up > 0), up, 0.0)
            ndm = np.where((dn > up) & (dn > 0), dn, 0.0)
            a = 1 / n
            atr = pd.Series(tr).ewm(alpha=a, adjust=False).mean()
            with np.errstate(divide="ignore", invalid="ignore"):
                pdi = 100 * pd.Series(pdm).ewm(alpha=a, adjust=False).mean() / atr
                ndi = 100 * pd.Series(ndm).ewm(alpha=a, adjust=False).mean() / atr
                dx = (100 * (pdi - ndi).abs() / (pdi + ndi)).fillna(0)
            adx = dx.ewm(alpha=a, adjust=False).mean().to_numpy().copy()
            adx[: 2 * n] = np.nan  # warm-up
            self._cache[key] = adx
        return self._cache[key]

    def first_cross_above(self, x: np.ndarray, level: np.ndarray) -> np.ndarray:
        """True on the candle whose close is above `level` while the previous close of the same day was not."""
        above = x > level
        prev = np.r_[False, above[:-1]]
        prev[self.day_start] = False
        return above & ~prev

    def first_cross_below(self, x: np.ndarray, level: np.ndarray) -> np.ndarray:
        below = x < level
        prev = np.r_[False, below[:-1]]
        prev[self.day_start] = False
        return below & ~prev


def _day_cumsum(x: np.ndarray, day: np.ndarray, starts: np.ndarray) -> np.ndarray:
    cs = np.cumsum(x)
    offset = cs[starts] - x[starts]
    return cs - offset[day]


def eligibility(f: Frame, cfg: dict, mode: str, membership=None, ca_dates: Optional[List[str]] = None) -> np.ndarray:
    """Per-day boolean: may this symbol be traded today? Uses information up to yesterday only."""
    u = cfg["universe"]
    ok = f.d_hist_days >= int(u.get("min_trading_days", 60))
    pc = f.d_prev_close
    ok &= np.nan_to_num(pc, nan=0) >= float(u.get("min_price", 0))
    ok &= np.nan_to_num(pc, nan=np.inf) <= float(u.get("max_price", 1e12))
    if mode == "liquid":
        ok &= np.nan_to_num(f.d_adv_value, nan=0) >= float(u.get("min_avg_traded_value", 0))
        ok &= np.nan_to_num(f.d_adv_vol, nan=0) >= float(u.get("min_avg_daily_volume", 0))
    if ca_dates:
        excl = int(cfg["data"].get("ca_exclusion_days", 20))
        idx = {d: i for i, d in enumerate(f.dates)}
        for s in ca_dates:
            d0 = date.fromisoformat(s)
            i = idx.get(d0)
            if i is None:  # gap day itself may have been excluded; start at next available day
                later = [k for k, dd in enumerate(f.dates) if dd >= d0]
                if not later:
                    continue
                i = later[0]
            ok[i: i + excl] = False
    if membership is not None:
        spans = membership.get(f.symbol, [])
        mem = np.array([any(a <= d <= b for a, b in spans) for d in f.dates], dtype=bool)
        ok &= mem
    return ok
