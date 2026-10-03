# Backtest: September 2026 (1 month), all 4 strategies

*Run on 3 Oct 2026 with `tools/intraday_lab.py`. The rules are exactly as in [`STRATEGIES.md`](../STRATEGIES.md). Not investment advice.*

## Setup

| Item | Value |
|---|---|
| Test window | **1 Sep – 1 Oct 2026: 22 trading days.** Data starts 13 Jul, so ATR and ADX warm up before the window. |
| Data | Yahoo Finance 5-minute candles (`tools/fetch_yahoo.py`). Nifty 50 and Bank Nifty **spot index** (no futures data available). 10 liquid stocks: RELIANCE, HDFCBANK, ICICIBANK, INFY, TCS, SBIN, AXISBANK, KOTAKBANK, LT, BHARTIARTL. |
| Index sizing | **1 lot** (Nifty 65, Bank Nifty 30). Full post-April-2026 futures costs. Slippage 1 pt / 2 pts per side. |
| Stock sizing | ₹5 lakh capital per stock, 0.5% risk per trade, cash intraday costs, slippage 0.03% per side |
| Market context | **Strong down month.** Nifty 24,077 → 22,422 (−6.9%). Bank Nifty 57,560 → 54,451 (−5.4%). Average daily range: Nifty 190 pts, Bank Nifty 578 pts. |

## Results: September 2026

**Nifty (1 lot)**

| Strategy | Trades | Win % | Net ₹ | Total R | Avg R/trade | Costs ₹ |
|---|---|---|---|---|---|---|
| A ORB-VWAP | 6 | 33% | **−8,069** | −1.46 | −0.24 | 5,469 |
| B Holy Grail | 0 | – | 0 | – | – | – |
| C Reversal | 0 | – | 0 | – | – | – |
| D NR7 | 2 | 100% | **+5,254** | +0.92 | +0.46 | 1,831 |
| **Total** | **8** | | **−2,815** | **−0.54** | | |

**Bank Nifty (1 lot)**

| Strategy | Trades | Win % | Net ₹ | Total R | Avg R/trade | Costs ₹ |
|---|---|---|---|---|---|---|
| A ORB-VWAP | 10 | 30% | **−2,833** | −0.35 | −0.03 | 10,051 |
| B Holy Grail | 0 | – | 0 | – | – | – |
| C Reversal | 1 | 0% | −4,409 | −0.82 | −0.82 | 1,005 |
| D NR7 | 1 | 100% | **+12,269** | +1.38 | +1.38 | 978 |
| **Total** | **12** | | **+5,028** | **+0.21** | | |

**10 stocks (₹5L, 0.5% risk)**

| Strategy | Trades | Win % | Net ₹ | Total R | Avg R/trade | Costs ₹ |
|---|---|---|---|---|---|---|
| A ORB-VWAP | 120 | 23% | **−56,506** | −26.3 | −0.22 | 16,243 |
| B Holy Grail | 0 | – | 0 | – | – | – |
| C Reversal | 0 | – | 0 | – | – | – |
| D NR7 | 22 | 45% | **+4,184** | +2.06 | +0.09 | 2,920 |

## Same rules over the full window (3 Aug – 1 Oct 2026, about 2 months)

| | A ORB-VWAP | B Holy Grail | C Reversal | D NR7 |
|---|---|---|---|---|
| Nifty, 1 lot | 13 trades, −₹18,473 (−3.4R) | 0 | 0 | 5 trades, +₹1,726 (+0.5R) |
| Bank Nifty, 1 lot | 20 trades, −₹31,011 (−4.2R) | 0 | 1 trade, −₹4,409 | 3 trades, +₹6,176 (+0.7R) |
| 10 stocks | 227 trades, −₹83,959 (−39.4R) | 1 trade, −₹2,302 | 0 | 45 trades, +₹18,845 (+8.8R) |

## What this says

1. **A: ORB-VWAP lost money everywhere, in both windows.** It won only 20–33% of trades, and the VWAP trail cut most trades early.
   * On Bank Nifty in September, **gross P&L was positive (+₹7,218), but costs of ₹10,051 turned it into a loss.** This is the post-April-2026 STT effect described in the research.
   * On stocks it lost even before costs.
   * In this sample the rules as written do not work. Do not trade A live in its current form.
2. **B: Holy Grail took zero index trades.** All 8–9 candidate days per index were rejected because the pullback candle's low/high gives a stop of roughly 20–40 Nifty points. That is far below the ~68-point minimum the cost filter requires. **On 5-minute index futures, B cannot be traded under 2026 costs.** It would need a 15-minute version with wider stops, which is untested.
3. **C: Reversal almost never qualified.** VWAP was usually less than 1.5R away (rejected on 5 of the September days for Nifty and 44 times across the stocks). In a one-directional falling month, failed breakouts were rare.
4. **D: NR7 was the only strategy positive in every test:** Nifty, Bank Nifty and stocks, in both the 1-month and 2-month windows. The stock sample over 2 months is +8.8R from 45 trades (51% win rate, +0.19R per trade).

## How much to trust this (honestly: very little yet)

* **The sample is tiny.** 1 month, 8 trades on Nifty and 12 on Bank Nifty. D's index results rest on 1–2 trades each. The stock D result (45 trades) is the only figure with any weight, and even 45 trades is well below the 100+ needed.
* **One market regime.** September was a strong sell-off. Results in a rising or sideways month could be completely different.
* **Spot, not futures.** The futures premium is ignored, and index VWAP is time-weighted because the index data has no volume. The stock VWAP is real.
* **The stocks were tested independently.** In live trading the account-level limits (2 open positions, 3 losses per day) would cut the stock trade count.
* **Source.** Yahoo data is unofficial. Use NSE-authorised data before making real decisions.

## Next steps

1. Get **2+ years** of 5-minute data. Run `run.sh backtest <file> --start <date>` per year and per regime, as in Section 6 of the playbook.
2. Prioritise **D (NR7)** for more testing on a larger stock universe.
3. Before deciding to drop A, test its variants: VWAP trail versus hold to 15:10, OR of 5 / 15 / 30 minutes.
4. For B, test a 15-minute-chart version. On 5-minute index charts the stop is always smaller than the cost filter allows.

## Reproduce

```bash
python3 tools/fetch_yahoo.py
python3 tools/intraday_lab.py trades data/NIFTY_5m.csv --instrument nifty --lots 1 --start 2026-09-01 --end 2026-10-01 --out nifty_trades.csv
python3 tools/intraday_lab.py trades data/BANKNIFTY_5m.csv --instrument banknifty --lots 1 --start 2026-09-01 --end 2026-10-01
python3 tools/intraday_lab.py backtest data/SBIN_5m.csv --instrument stock --capital 500000 --slippage 0.29 --start 2026-09-01
```

## Every index trade (September)

| Inst | Strategy | Day | Side | Entry time | Entry | Stop | Exit time | Exit | Reason | Net ₹ | R |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Nifty | A | 09-03 | Short | 11:05 | 23,928.5 | 24,011.5 | 15:10 | 23,888.5 | time | +1,673 | +0.31 |
| Nifty | A | 09-04 | Long | 10:55 | 23,968.1 | 23,900.2 | 12:10 | 23,951.9 | VWAP | −1,981 | −0.45 |
| Nifty | A | 09-07 | Short | 10:10 | 23,805.5 | 23,888.8 | 14:25 | 23,785.2 | VWAP | +397 | +0.07 |
| Nifty | A | 09-17 | Long | 09:30 | 23,285.5 | 23,195.1 | 09:40 | 23,251.3 | VWAP | −3,122 | −0.53 |
| Nifty | A | 09-23 | Long | 11:05 | 23,428.0 | 23,352.2 | 14:55 | 23,421.5 | VWAP | −1,334 | −0.27 |
| Nifty | A | 09-30 | Long | 10:10 | 22,756.2 | 22,660.6 | 10:30 | 22,712.8 | VWAP | −3,703 | −0.60 |
| Nifty | D | 09-07 | Short | 09:50 | 23,816.2 | 23,900.4 | 15:10 | 23,742.6 | time | +3,861 | +0.71 |
| Nifty | D | 09-21 | Long | 09:25 | 23,401.3 | 23,303.1 | 15:10 | 23,436.7 | time | +1,392 | +0.22 |
| BankNifty | A | 09-02 | Long | 10:00 | 57,092.4 | 56,848.3 | 11:00 | 57,048.5 | VWAP | −2,332 | −0.32 |
| BankNifty | A | 09-03 | Long | 09:40 | 57,715.4 | 57,472.1 | 10:10 | 57,652.8 | VWAP | −2,902 | −0.40 |
| BankNifty | A | 09-04 | Long | 11:15 | 57,579.1 | 57,325.5 | 14:15 | 57,513.1 | VWAP | −3,003 | −0.40 |
| BankNifty | A | 09-07 | Short | 10:20 | 57,087.3 | 57,340.0 | 13:50 | 57,115.8 | VWAP | −1,869 | −0.25 |
| BankNifty | A | 09-11 | Long | 10:10 | 56,024.3 | 55,765.2 | 15:10 | 56,581.8 | time | **+15,719** | **+2.02** |
| BankNifty | A | 09-17 | Long | 09:30 | 56,503.0 | 56,201.5 | 10:20 | 56,397.6 | VWAP | −4,166 | −0.46 |
| BankNifty | A | 09-18 | Short | 10:55 | 56,101.2 | 56,327.0 | 11:30 | 56,173.6 | VWAP | −3,171 | −0.47 |
| BankNifty | A | 09-23 | Long | 10:00 | 56,481.8 | 56,228.5 | 14:50 | 56,530.6 | VWAP | +456 | +0.06 |
| BankNifty | A | 09-30 | Long | 10:00 | 54,682.1 | 54,347.7 | 14:25 | 54,770.0 | VWAP | +1,662 | +0.17 |
| BankNifty | A | 10-01 | Long | 10:00 | 54,971.4 | 54,622.5 | 11:00 | 54,896.5 | VWAP | −3,226 | −0.31 |
| BankNifty | C | 09-11 | Short | 14:15 | 56,472.3 | 56,651.4 | 15:10 | 56,585.8 | time | −4,409 | −0.82 |
| BankNifty | D | 09-28 | Short | 09:20 | 54,922.5 | 55,219.8 | 15:10 | 54,480.9 | time | **+12,269** | **+1.38** |
