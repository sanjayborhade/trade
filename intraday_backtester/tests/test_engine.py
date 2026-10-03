"""Unit tests for execution timing, fills, exits, costs and portfolio limits.

Run:  python -m pytest -q
"""

import os
import sys
from datetime import date

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ibt.config import load_config  # noqa: E402
from ibt.engine.costs import CostModel  # noqa: E402
from ibt.engine.portfolio import simulate_portfolio  # noqa: E402
from ibt.engine.simulator import Simulator  # noqa: E402
from ibt.features import Frame  # noqa: E402
from ibt.strategies.base import BaseStrategy, Signals  # noqa: E402

CFG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml")


@pytest.fixture
def cfg():
    c = load_config(CFG_PATH)
    c["costs"]["SLIPPAGE"] = 0.0
    c["costs"]["SPREAD"] = 0.0
    c["execution"]["trade_through_ticks"] = 0
    return c


def flat_day(day="2024-01-02", price=100.0, n=375):
    ts = pd.Timestamp(day) + pd.Timedelta(hours=9, minutes=15) + pd.to_timedelta(np.arange(n), unit="m")
    return pd.DataFrame({"ts": ts, "open": price, "high": price + 0.1, "low": price - 0.1, "close": price,
                         "volume": 1000.0})


class Scripted(BaseStrategy):
    """Signal on chosen candle indices; fixed stop/target."""
    name = "scripted"
    default_params = {"entry_start": "09:15", "entry_cutoff": "15:00", "target_r": 0.0}

    def __init__(self, idx, side=1, order="market", level=None, stop=95.0, target=np.nan, **kw):
        super().__init__(kw)
        self.idx, self.side, self.order, self.level, self._stop, self._tgt = idx, side, order, level, stop, target

    def generate_signal(self, f):
        s = np.zeros(f.n, bool)
        s[self.idx] = True
        z = np.zeros(f.n, bool)
        lv = np.full(f.n, self.level if self.level is not None else np.nan)
        return Signals(long=s if self.side == 1 else z, short=s if self.side == -1 else z, order_type=self.order,
                       level_long=lv, level_short=lv, valid_minutes=60)

    def calculate_stop(self, f, i, side, fill):
        return self._stop

    def calculate_target(self, f, i, side, fill, stop):
        return self._tgt


def run(cfg, bars, strat):
    f = Frame("T", bars)
    return Simulator(cfg).run(f, strat, np.ones(f.n_days, bool))[0]


def test_market_order_fills_next_candle_open(cfg):
    b = flat_day()
    b.loc[11, "open"] = 101.0                        # candle after the signal candle
    t = run(cfg, b, Scripted(10))
    assert t.loc[0, "entry_ts"] == b.loc[11, "ts"]   # NOT the signal candle
    assert t.loc[0, "entry_raw"] == 101.0


def test_slippage_and_spread_worsen_fills(cfg):
    cfg["costs"]["SLIPPAGE"] = 0.001
    cfg["costs"]["SPREAD"] = 0.002
    t = run(cfg, flat_day(), Scripted(10))
    assert t.loc[0, "entry_px"] == pytest.approx(100.0 * 1.002)      # +0.1 % slip +0.1 % half-spread
    assert t.loc[0, "exit_px"] == pytest.approx(100.0 * 0.998)


def test_stop_entry_gap_fills_at_open_not_level(cfg):
    b = flat_day()
    b.loc[20, ["open", "high", "low", "close"]] = [103.0, 103.5, 102.8, 103.2]
    t = run(cfg, b, Scripted(10, order="stop", level=101.0))
    assert t.loc[0, "entry_ts"] == b.loc[20, "ts"]
    assert t.loc[0, "entry_raw"] == 103.0             # gapped through 101 -> filled at the open


def test_stop_first_when_stop_and_target_in_same_candle(cfg):
    b = flat_day()
    b.loc[30, ["open", "high", "low", "close"]] = [100, 106, 94, 100]
    t = run(cfg, b, Scripted(10, stop=95.0, target=105.0))
    assert t.loc[0, "exit_reason"] == "stop"
    assert t.loc[0, "exit_raw"] == 95.0


def test_stop_gap_fills_at_worse_open(cfg):
    b = flat_day()
    b.loc[40, ["open", "high", "low", "close"]] = [90, 91, 89, 90]
    t = run(cfg, b, Scripted(10, stop=95.0))
    assert t.loc[0, "exit_raw"] == 90.0               # gap below stop -> worse price


def test_square_off_at_configured_time(cfg):
    t = run(cfg, flat_day(), Scripted(10, stop=50.0))
    assert t.loc[0, "exit_reason"] == "square_off"
    assert pd.Timestamp(t.loc[0, "exit_ts"]).strftime("%H:%M") == "15:15"


def test_no_target_on_entry_candle_for_stop_orders(cfg):
    b = flat_day()
    b.loc[20, ["open", "high", "low", "close"]] = [100.5, 110, 100.4, 109]   # triggers and passes target
    t = run(cfg, b, Scripted(10, order="stop", level=101.0, stop=95.0, target=105.0))
    assert t.loc[0, "exit_reason"] != "target" or pd.Timestamp(t.loc[0, "exit_ts"]) > b.loc[20, "ts"]


def test_costs_equity_intraday_round_trip(cfg):
    c = CostModel(load_config(CFG_PATH)["costs"])
    r = c.round_trip(100_000, 100_000)
    assert r["brokerage"] == pytest.approx(40.0)         # min(0.03 %, Rs 20) x 2
    assert r["stt"] == pytest.approx(25.0)               # 0.025 % sell side
    assert r["stamp"] == pytest.approx(3.0)              # 0.003 % buy side
    assert r["exchange"] == pytest.approx(6.14)
    assert r["gst"] == pytest.approx(0.18 * (40 + 6.14 + 0.2))
    assert r["total"] == pytest.approx(40 + 25 + 6.14 + 0.2 + 3 + 0.18 * 46.34)


def _cand(sym, minute, exit_minute, net=0.0, side=1):
    ts = pd.Timestamp("2024-01-02 09:15") + pd.Timedelta(minutes=minute)
    xs = pd.Timestamp("2024-01-02 09:15") + pd.Timedelta(minutes=exit_minute)
    return {"symbol": sym, "strategy": "x", "side": side, "date": date(2024, 1, 2), "signal_ts": ts, "entry_ts": ts,
            "exit_ts": xs, "entry_raw": 100.0, "entry_px": 100.0, "stop": 99.0, "target": np.nan,
            "exit_raw": 100.0 + net, "exit_px": 100.0 + net, "exit_reason": "stop" if net < 0 else "square_off",
            "order_type": "market", "hold_minutes": exit_minute - minute, "entry_minute": minute, "risk_ps": 1.0,
            "mfe_r": 0, "mae_r": 0, "gap_pct": 0, "rvol_cum": 1, "atr": 1, "entry_bar_volume": 1e9,
            "prev_close": 100, "score": 0.0}


def test_portfolio_respects_max_open_positions(cfg):
    cfg["portfolio"]["MAX_OPEN_POSITIONS"] = 2
    c = pd.DataFrame([_cand(f"S{k}", 30, 300) for k in range(5)])
    res = simulate_portfolio(c, cfg, [date(2024, 1, 2)])
    assert len(res.trades) == 2
    assert res.rejections["max_open_positions"] == 3


def test_portfolio_daily_loss_limit_blocks_new_trades(cfg):
    cfg["portfolio"]["MAX_DAILY_LOSS"] = 0.01
    cfg["portfolio"]["RISK_PER_TRADE"] = 0.005
    cfg["portfolio"]["MAX_POSITION_VALUE_PCT"] = 10
    cfg["portfolio"]["MAX_PORTFOLIO_EXPOSURE"] = 10
    rows = [_cand("A", 10, 20, net=-1.0), _cand("B", 30, 40, net=-1.0), _cand("C", 50, 60, net=-1.0)]
    res = simulate_portfolio(pd.DataFrame(rows), cfg, [date(2024, 1, 2)])
    # after two full 0.5 % losses the 1 % daily limit is reached -> third trade refused
    assert len(res.trades) == 2
    assert res.rejections["daily_loss_limit"] == 1


def test_position_size_from_risk(cfg):
    cfg["portfolio"]["MAX_POSITION_VALUE_PCT"] = 10
    cfg["portfolio"]["MAX_PORTFOLIO_EXPOSURE"] = 10
    res = simulate_portfolio(pd.DataFrame([_cand("A", 10, 20)]), cfg, [date(2024, 1, 2)])
    eq, risk = cfg["portfolio"]["INITIAL_CAPITAL"], cfg["portfolio"]["RISK_PER_TRADE"]
    q = res.trades.loc[0, "qty"]
    assert q * 1.0 <= eq * risk                      # stop distance 1.0 per share
    assert q > 0.9 * eq * risk / 1.1
