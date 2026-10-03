"""Strategy registry. To add a strategy: create a module with a BaseStrategy subclass and add it here."""

from .base import BaseStrategy, Signals
from .failed_breakout import FailedBreakout
from .first_candle import FirstCandleBreakout
from .gap_reversal import GapReversal
from .nr7_breakout import NR7Breakout
from .orb import OpeningRangeBreakout
from .pdh_breakout import PDHBreakout
from .relative_strength import RelativeStrength
from .trend_pullback import TrendPullback
from .vwap_momentum import VWAPMomentum
from .vwap_reversion import VWAPReversion

REGISTRY = {cls.name: cls for cls in (
    OpeningRangeBreakout, VWAPMomentum, TrendPullback, RelativeStrength, FailedBreakout,
    NR7Breakout, FirstCandleBreakout, PDHBreakout, GapReversal, VWAPReversion,
)}

ALIASES = {"vwap": "vwap_momentum", "pullback": "trend_pullback", "rs": "relative_strength",
           "mean_reversion": "failed_breakout", "nr7": "nr7_breakout", "first_candle": "first_candle_breakout",
           "pdh": "pdh_breakout", "gap": "gap_reversal"}


def resolve_names(spec: str, include_experimental: bool = False):
    if spec in ("all", "*"):
        return [n for n, c in REGISTRY.items() if include_experimental or not c.experimental]
    out = []
    for s in spec.split(","):
        s = s.strip().lower()
        s = ALIASES.get(s, s)
        if s not in REGISTRY:
            raise ValueError(f"Unknown strategy '{s}'. Available: {', '.join(REGISTRY)} (or 'all')")
        out.append(s)
    return out


def build(name: str, cfg: dict, overrides=None) -> BaseStrategy:
    scfg = (cfg.get("strategies") or {}).get(name) or {}
    params = dict(scfg.get("params") or {})
    params.update(overrides or {})
    return REGISTRY[name](params, allow_short=bool(cfg["portfolio"].get("allow_short", True)))
