"""Execution model: how orders become fills.

TIMING (no look-ahead):
  1. A strategy evaluates its signal on the CLOSE of candle i (all of candle i is known).
  2. The order is created after that close.
  3. It can only execute on candle i+1 or later ("next available candle"; if candles are
     missing, the next one that exists that day).

FILL PRICES:
  market order   open of candle i+1, worsened by slippage + half the spread
  stop entry     first candle j > i with high >= level + trade-through ticks (long);
                 fill = max(open_j, level)  -> a gap through the level fills at the open
                 then worsened by slippage + half spread
  limit entry    first candle j > i with low <= level - trade-through ticks (long);
                 fill = min(open_j, level); no slippage (you set the price)
  stop-loss      candle low <= stop (long): fill = min(open, stop) - slippage/spread
  target         candle high >= target + trade-through: fill at target (or open if it gaps above)
  timed exits    square-off, max-holding time and indicator exits execute at the OPEN of the
                 next candle, with slippage + spread.
  same candle    if stop and target are both inside one candle the order of events is
                 unknown -> stop first by default (conservative).
  entry candle   for stop/limit entries the path inside the entry candle is unknown, so only
                 the stop is checked there (a loss is assumed if the candle also hits it);
                 targets are checked from the next candle.
"""

from __future__ import annotations


class ExecutionModel:
    def __init__(self, cfg: dict):
        c, e = cfg["costs"], cfg["execution"]
        self.slip = float(c["SLIPPAGE"])
        self.half_spread = float(c["SPREAD"]) / 2
        self.tick = float(e["tick_size"])
        self.through = int(e["trade_through_ticks"]) * self.tick
        self.stop_first = e["same_bar_stop_target"] == "stop_first"

    def adverse(self, raw: float, side: int, entering: bool) -> float:
        """Price after slippage + half spread. side=+1 long, -1 short."""
        sign = side if entering else -side
        return raw * (1 + sign * (self.slip + self.half_spread))
