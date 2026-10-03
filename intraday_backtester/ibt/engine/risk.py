"""Position sizing and risk limits.

quantity = floor( (equity x RISK_PER_TRADE) / (stop distance + estimated costs per share) )
then capped by: max position value, remaining portfolio exposure, and the share of the
fill candle's volume you could realistically get (MAX_PARTICIPATION).
"""

from __future__ import annotations

import math


def risk_based_quantity(risk_rupees: float, entry: float, stop: float, cost_per_share: float,
                        max_value: float) -> int:
    per_share = abs(entry - stop) + max(cost_per_share, 0.0)
    if not (per_share > 0 and math.isfinite(per_share) and entry > 0):
        return 0
    q = int(risk_rupees // per_share)
    if max_value is not None and math.isfinite(max_value):
        q = min(q, int(max_value // entry))
    return max(q, 0)


def participation_cap(volume: float, max_participation: float) -> float:
    """Max shares fillable from a candle's volume. inf if volume unknown or cap disabled."""
    if max_participation <= 0 or not (volume and math.isfinite(volume) and volume > 0):
        return math.inf
    return volume * max_participation
