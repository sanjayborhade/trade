"""Strategy interface.

A strategy is a small class with:
  default_params          every tunable number lives here (overridable from config.yaml / CLI)
  generate_signal(f)      vectorised entry signals evaluated at candle CLOSE -> Signals
  calculate_stop(...)     initial stop for one trade
  calculate_target(...)   target for one trade (NaN = none)
  accept_fill(...)        optional veto at fill time (e.g. "gain must be 3-10 % when it triggers")
  calculate_position_size default risk-based sizing (used by the portfolio)

The engine handles order execution, exits, trailing stops, costs and the portfolio,
so strategies only describe WHAT to trade, never HOW fills happen.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

import numpy as np

from ..config import parse_hhmm
from ..features import Frame

COMMON_PARAMS: Dict[str, Any] = {
    "direction": "both",          # both | long | short
    "entry_start": "09:30",       # first candle (start time) whose close may generate a signal
    "entry_cutoff": "14:30",      # last candle whose close may generate a signal
    "max_trades_per_day": 1,      # per stock
    "target_r": 2.0,              # target = R multiple of the initial risk (0 = no target)
    "trailing": "none",           # none | atr | vwap | ema   (see engine/simulator.py)
    "trail_atr": 0.5,             # ATR multiple for the ATR (chandelier) trail, on closes
    "trail_ema": 20,              # EMA length (1-minute) for the EMA trail
    "breakeven_r": 0.0,           # move stop to breakeven (+costs) once price reaches this R (0 = off)
    "max_hold_minutes": 0,        # time exit (0 = off)
}


@dataclass
class Signals:
    long: np.ndarray                         # bool, signal on the close of this candle
    short: np.ndarray
    order_type: str = "market"               # market | stop | limit
    level_long: Optional[np.ndarray] = None  # entry level for stop/limit orders
    level_short: Optional[np.ndarray] = None
    cancel_long: Optional[np.ndarray] = None   # pending long order is cancelled if low <= this
    cancel_short: Optional[np.ndarray] = None  # pending short order is cancelled if high >= this
    valid_minutes: Optional[int] = None      # lifetime of stop/limit orders
    last_fill_minute: Optional[int] = None   # no fills at/after this minute-of-session
    score: Optional[np.ndarray] = None       # ranking when several stocks signal together (higher first)
    exit_long: Optional[np.ndarray] = None   # strategy exit condition at candle close (-> next open)
    exit_short: Optional[np.ndarray] = None


class BaseStrategy:
    name = "base"
    title = "Base"
    research_basis = ""
    experimental = False
    needs_benchmark = False
    default_params: Dict[str, Any] = {}

    def __init__(self, params: Optional[Dict[str, Any]] = None, allow_short: bool = True):
        merged = dict(COMMON_PARAMS)
        merged.update(self.default_params)
        unknown = set(params or {}) - set(merged)
        if unknown:
            raise ValueError(f"{self.name}: unknown parameter(s) {sorted(unknown)}. "
                             f"Valid: {sorted(merged)}")
        merged.update(params or {})
        self.p = merged
        self.allow_short = allow_short
        self.entry_start_min = parse_hhmm(self.p["entry_start"]) - 555
        self.entry_cutoff_min = parse_hhmm(self.p["entry_cutoff"]) - 555
        self.validate()

    # -- helpers --------------------------------------------------------------
    def validate(self) -> None:
        if self.p["direction"] not in ("both", "long", "short"):
            raise ValueError(f"{self.name}: direction must be both | long | short")
        if self.p["trailing"] not in ("none", "atr", "vwap", "ema"):
            raise ValueError(f"{self.name}: trailing must be none | atr | vwap | ema")
        if self.entry_cutoff_min < self.entry_start_min:
            raise ValueError(f"{self.name}: entry_cutoff is before entry_start")

    def window(self, f: Frame) -> np.ndarray:
        return (f.minute >= self.entry_start_min) & (f.minute <= self.entry_cutoff_min)

    def sides(self, long: np.ndarray, short: np.ndarray):
        d = self.p["direction"]
        if d == "short":
            long = np.zeros_like(long)
        if d == "long" or not self.allow_short:
            short = np.zeros_like(short)
        return long, short

    @staticmethod
    def b(x) -> np.ndarray:
        """NaN-safe boolean array."""
        return np.nan_to_num(np.asarray(x, dtype=float), nan=0.0).astype(bool) if np.asarray(x).dtype != bool \
            else np.asarray(x)

    # -- interface ------------------------------------------------------------
    def generate_signal(self, f: Frame) -> Signals:
        raise NotImplementedError

    def calculate_stop(self, f: Frame, i: int, side: int, fill: float) -> float:
        raise NotImplementedError

    def calculate_target(self, f: Frame, i: int, side: int, fill: float, stop: float) -> float:
        r = float(self.p.get("target_r") or 0)
        if r <= 0 or not np.isfinite(stop):
            return np.nan
        return fill + side * r * abs(fill - stop)

    def accept_fill(self, f: Frame, i: int, j: int, side: int, fill: float) -> bool:
        return True

    def calculate_position_size(self, equity: float, risk_fraction: float, entry: float, stop: float,
                                cost_per_share: float, max_value: float) -> int:
        from ..engine.risk import risk_based_quantity
        return risk_based_quantity(equity * risk_fraction, entry, stop, cost_per_share, max_value)

    def describe(self) -> str:
        return f"{self.name}: {self.title}" + (" [EXPERIMENTAL]" if self.experimental else "")
