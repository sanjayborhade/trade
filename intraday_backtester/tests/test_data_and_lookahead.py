"""Data parsing, cleaning, and look-ahead tests.  Run: python -m pytest -q"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ibt.config import load_config  # noqa: E402
from ibt.data.loader import clean_bars, to_ist  # noqa: E402
from ibt.engine.simulator import Simulator  # noqa: E402
from ibt.features import Frame, eligibility  # noqa: E402
from ibt.strategies import REGISTRY, build  # noqa: E402
from ibt.validation import perturbation_test  # noqa: E402
from tests.synthetic import make_symbol, trading_days  # noqa: E402

CFG = os.path.join(ROOT, "config.yaml")


@pytest.mark.parametrize("vals,dayfirst,expect", [
    (["05-01-2024 09:15"], True, "2024-01-05 09:15"),
    (["2024-01-05 09:15:00"], True, "2024-01-05 09:15"),          # ISO is never day-first
    (["20240105 09:15"], True, "2024-01-05 09:15"),
    (["01/05/2024 09:15"], False, "2024-01-05 09:15"),
    (["2024-01-05T09:15:00+05:30"], True, "2024-01-05 09:15"),
    (["2024-01-05T03:45:00Z"], True, "2024-01-05 09:15"),         # UTC -> IST
    (["05-Jan-2024 09:15"], True, "2024-01-05 09:15"),
])
def test_timestamp_formats(vals, dayfirst, expect):
    assert str(to_ist(pd.Series(vals), None, dayfirst, "Asia/Kolkata").iloc[0])[:16] == expect


def test_epoch_seconds_and_ms():
    s = to_ist(pd.Series([1704426300]), None, True, "Asia/Kolkata").iloc[0]
    ms = to_ist(pd.Series([1704426300000]), None, True, "Asia/Kolkata").iloc[0]
    assert str(s)[:16] == str(ms)[:16] == "2024-01-05 09:15"


def test_cleaning_counts_every_problem():
    cfg = load_config(CFG)
    d = make_symbol(1, trading_days("2024-01-01", 3))
    bad = d.copy()
    bad = pd.concat([bad, bad.iloc[[5, 6]]])                         # 2 duplicates
    bad.loc[bad.index[10], "close"] = -1                             # bad price
    bad.loc[bad.index[20], "high"] = bad.loc[bad.index[20], "low"] - 1  # invalid OHLC
    bad.loc[bad.index[30], "volume"] = np.nan                         # missing volume
    extra = d.iloc[[0]].copy()
    extra["ts"] = extra["ts"] - pd.Timedelta(minutes=30)              # 08:45 pre-open candle
    bad = pd.concat([bad, extra])
    clean, st = clean_bars(bad, "X", cfg["data"])
    assert st.duplicates == 2 and st.nonpositive_or_missing_price == 1 and st.ohlc_invalid >= 1
    assert st.missing_volume == 1 and st.outside_session == 1
    assert (clean["high"] >= clean[["open", "close"]].max(axis=1)).all()
    assert clean["ts"].is_monotonic_increasing and not clean["ts"].duplicated().any()


def test_bar_end_timestamps_are_detected_and_shifted():
    cfg = load_config(CFG)
    d = make_symbol(1, trading_days("2024-01-01", 5))
    d["ts"] = d["ts"] + pd.Timedelta(minutes=1)                      # 09:16 .. 15:30 vendor style
    clean, st = clean_bars(d, "X", cfg["data"])
    assert st.bar_end_shift_applied
    assert clean["ts"].dt.strftime("%H:%M").min() == "09:15"


def test_opening_range_not_known_before_it_completes():
    f = Frame("X", make_symbol(2, trading_days("2024-01-01", 10)))
    h, _ = f.opening_range(15)
    assert np.isnan(h[f.minute < 14]).all()       # candles 09:15..09:28 cannot see the 15-min range
    assert np.isfinite(h[f.minute >= 14]).all()


def test_daily_features_use_only_previous_days():
    f = Frame("X", make_symbol(3, trading_days("2024-01-01", 30)))
    d = 10
    i = f.day_start[d]
    assert f.prev_high[i] == f.d_high[d - 1] and f.prev_close[i] == f.d_close[d - 1]
    assert np.isnan(f.atr[f.day_start[0]])


RELAX = {"pdh_breakout": {"volume_mult": 0.0}, "vwap_reversion": {"rsi_extreme": 60, "dev_atr": 0.2, "adx_max": 100},
         "failed_breakout": {"min_rr": 0.1}, "vwap_momentum": {"volume_mult": 0.0},
         "relative_strength": {"volume_mult": 0.0, "rs_min_pct": 0.2},
         "first_candle_breakout": {"min_gain_pct": -50, "max_gain_pct": 50}}


def test_no_lookahead_in_any_strategy():
    cfg = load_config(CFG)
    cfg["universe"]["min_trading_days"] = 20
    days = trading_days("2023-01-02", 80)
    fails = perturbation_test("S", make_symbol(5, days), list(REGISTRY), cfg,
                              {"UNIVERSE_EW": make_symbol(6, days)}, n_cuts=6, seed=3, overrides=RELAX)
    assert fails == [], "\n".join(fails[:10])


def test_detector_catches_a_planted_leak():
    from ibt.strategies.base import BaseStrategy, Signals

    class Leak(BaseStrategy):
        name = "leak"

        def generate_signal(self, f):
            nxt = np.r_[f.close[1:], np.nan]                     # peeks at the NEXT candle
            return Signals(long=nxt > f.close, short=np.zeros(f.n, bool))

        def calculate_stop(self, f, i, side, fill):
            return fill * 0.99

    REGISTRY["leak"] = Leak
    try:
        cfg = load_config(CFG)
        cfg["universe"]["min_trading_days"] = 5
        fails = perturbation_test("S", make_symbol(5, trading_days("2023-01-02", 30)), ["leak"], cfg, n_cuts=3)
        assert fails, "the look-ahead detector failed to catch an obvious leak"
    finally:
        del REGISTRY["leak"]


def test_random_walk_has_no_edge_after_costs():
    """On a random walk no strategy may show a significant profit (would indicate a bug)."""
    cfg = load_config(CFG)
    cfg["universe"]["min_trading_days"] = 20
    days = trading_days("2023-01-02", 250)
    total_r, n = 0.0, 0
    for seed in range(3):
        f = Frame("S", make_symbol(100 + seed, days))
        ok = eligibility(f, cfg, "all")
        for name in ("orb", "trend_pullback", "vwap_momentum"):
            t, _ = Simulator(cfg).run(f, build(name, cfg, RELAX.get(name)), ok)
            if len(t):
                r = (t["side"] * (t["exit_px"] - t["entry_px"]) / t["risk_ps"]).to_numpy()
                total_r += r.sum()
                n += len(r)
    assert n > 200
    assert total_r / n < 0.05, f"suspicious positive expectancy {total_r / n:.3f}R on random data"
