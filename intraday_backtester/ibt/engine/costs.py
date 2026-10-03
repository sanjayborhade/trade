"""Indian equity INTRADAY (MIS) transaction costs on NSE.

Which charges apply to an intraday cash-equity round trip (one buy + one sell):

  charge               side        default (edit in config.yaml -> costs)
  -------------------  ----------  ---------------------------------------------------
  Brokerage            both        min(0.03 % x order value, Rs 20) per executed order
  STT                  SELL only   0.025 % of sell value
  Exchange txn charge  both        0.00307 % of turnover (NSE)
  SEBI turnover fee    both        Rs 10 per crore = 0.0001 %
  Stamp duty           BUY only    0.003 % of buy value
  GST                  -           18 % on (brokerage + exchange charge + SEBI fee)
  IPFT (optional)      both        set if your contract note shows it separately

Slippage and the bid/ask spread are NOT charges: they are applied to the fill
prices by the execution model, so "gross P&L" already includes them while
"theoretical P&L" (signal prices) does not.
"""

from __future__ import annotations

from typing import Dict


class CostModel:
    def __init__(self, costs_cfg: dict):
        c = costs_cfg
        self.brk_pct = float(c["BROKERAGE_PCT"])
        self.brk_cap = float(c["BROKERAGE_MAX_PER_ORDER"])
        self.stt = float(c["STT"])
        self.exch = float(c["EXCHANGE_CHARGES"])
        self.sebi = float(c["SEBI_CHARGES"])
        self.stamp = float(c["STAMP_DUTY"])
        self.gst = float(c["GST"])
        self.ipft = float(c.get("IPFT", 0.0))
        self.slippage = float(c["SLIPPAGE"])
        self.half_spread = float(c["SPREAD"]) / 2

    def brokerage(self, value: float) -> float:
        if value <= 0:
            return 0.0
        b = self.brk_pct * value
        return min(b, self.brk_cap) if self.brk_cap > 0 else b

    def round_trip(self, buy_value: float, sell_value: float) -> Dict[str, float]:
        turnover = buy_value + sell_value
        brk = self.brokerage(buy_value) + self.brokerage(sell_value)
        stt = self.stt * sell_value
        exch = self.exch * turnover
        sebi = self.sebi * turnover
        ipft = self.ipft * turnover
        stamp = self.stamp * buy_value
        gst = self.gst * (brk + exch + sebi)
        total = brk + stt + exch + sebi + ipft + stamp + gst
        return {"brokerage": brk, "stt": stt, "exchange": exch, "sebi": sebi, "ipft": ipft, "stamp": stamp,
                "gst": gst, "total": total}

    def total(self, buy_value: float, sell_value: float) -> float:
        return self.round_trip(buy_value, sell_value)["total"]

    def pct_estimate(self) -> float:
        """Approximate round-trip charges as a fraction of one side's value (no brokerage cap)."""
        return (2 * self.brk_pct * (1 + self.gst) + self.stt + 2 * self.exch * (1 + self.gst)
                + 2 * self.sebi * (1 + self.gst) + 2 * self.ipft + self.stamp)

    def friction_pct(self) -> float:
        """Charges + slippage + spread for a round trip, as a fraction of price."""
        return self.pct_estimate() + 2 * (self.slippage + self.half_spread)
