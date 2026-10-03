# Backtest: First-Candle Breakout, Nifty 500 stocks

*Run on 3 Oct 2026 with `tools/orb_first_candle.py`. Not investment advice.*

## The strategy as tested

| Rule | How it was coded |
|---|---|
| Setup | The **first 5-minute candle (09:15–09:20)**. It stays valid while its high is still the day's high and its low is still the day's low. |
| Filter | **Gain vs previous close between 3% and 10%**, measured at the moment of entry. Previous close is the official daily close. |
| Entry | Buy when price **crosses above the first candle's high**, between 09:20 and 14:30. Fill at high + ₹0.05, or at the candle's open if it gapped above. |
| Stop | **First candle's low.** If that candle opens below the low, the fill is at that open. |
| Exit | Stop, or square-off at **15:10**. *No target was specified, so trades are held to the close.* |
| Same-candle ambiguity | If the entry candle also trades below the stop, it is counted as a loss (worst case) |
| Sizing | ₹5 lakh capital, **₹2,500 risk per trade (0.5%)**, max ₹5 lakh notional per trade |
| Costs | Full cash-intraday charges (brokerage, STT 0.025% sell, NSE, SEBI, stamp, GST) **+ 0.05% slippage per side** |
| Universe | Current Nifty 500 list from NSE (499 stocks with data); Yahoo 5-minute candles |

## Results

**14 Jul – 1 Oct 2026 (57 trading days)**

| View | Trades | Win % | Net ₹ | Total R | Avg R | Profit factor | Gross before costs |
|---|---|---|---|---|---|---|---|
| Every signal | 354 | 37% | **−64,781** | −26.1 | −0.07 | 0.82 | −35,903 |
| Account: first 3 signals per day, stop after 3 losses | 154 | 39% | **−1,371** | −0.5 | −0.00 | 0.99 | +10,916 |

**September only (1 Sep – 1 Oct)**

| View | Trades | Win % | Net ₹ | Total R | Avg R |
|---|---|---|---|---|---|
| Every signal | 125 | 37% | **−33,268** | −13.3 | −0.11 |
| Account: first 3 per day | 56 | 38% | **−10,744** | −4.4 | −0.08 |

**Exit variants** (because the rules give no target), every signal, full window:

| Exit | Net ₹ | Avg R |
|---|---|---|
| Your rules (stop or 15:10) | −64,781 | −0.07 |
| + target at 2R | −70,368 | −0.08 |
| + stop to breakeven at +1R | −44,646 | −0.05 |

**By month** (every signal): **July −₹10,474, August −₹21,039, September −₹31,592.** Negative in every month.

## Where it loses

| Split | Trades | Win % | Avg R | Net ₹ |
|---|---|---|---|---|
| Entry at 09:20 (breaks out right away) | 239 | 34% | **−0.14** | −83,821 |
| Entry 09:25–10:00 | 83 | 45% | **+0.15** | +29,615 |
| Entry after 10:00 | 32 | 34% | −0.14 | −10,575 |
| First candle > 3% wide | 135 | 33% | −0.11 | −35,538 |
| Gain 3–4% at entry | 204 | 37% | −0.10 | −48,277 |
| Nifty **closed** up that day *(hindsight)* | 169 | 42% | +0.06 | +26,221 |
| Nifty **above its open at the time of entry** *(known live)* | 137 | 36% | **−0.18** | −61,372 |

**What the splits mean:**
* The "Nifty up day" result is **hindsight**. When the filter uses only information available at entry, it does not help, and it was actually worse.
* "Skip 09:20 entries" looks positive overall (+0.07R, 115 trades). However, **the whole gain came from September** (+0.38R). July (−0.11R) and August (−0.04R) were negative. That is one good month, not a reliable edge.
* About 75% of trades exit at 15:10 rather than at the stop. The first-candle low is usually far away, so most trades drift sideways and give back their move, and costs plus slippage eat the rest.

## Conclusion

* **As specified, the strategy lost money in this sample:** negative in each of 3 months, and negative even before costs when every signal is taken.
* The account version (3 trades per day) is roughly breakeven. There is no evidence of an edge.
* Nothing found here is a proven fix. The 09:25–10:00 entry window is the only lead, and it depends on one month.

**Limits:** about 2.5 months of data (Yahoo's 5-minute limit); today's Nifty 500 list applied to past dates (mild survivorship bias); unofficial data. **Testing 2+ years of data is required before any decision.**

## Reproduce

```bash
python3 tools/fetch_yahoo.py --nifty500
python3 tools/orb_first_candle.py --data data --start 2026-09-01 --end 2026-10-01 --variants
python3 tools/orb_first_candle.py --data data --min-gain 3 --max-gain 10 --capital 500000 --risk 0.5
```
