# The Strategies to Trade: One-Page Cards

There are 4 strategies, and **"No Trade"** is a fifth, valid outcome. Full research and reasoning: [`research/indian_intraday_playbook.md`](research/indian_intraday_playbook.md).

> **None of these is proven on NSE data yet.**
> 1. Backtest them on your own 5-minute data (`run.sh backtest your.csv`).
> 2. Paper-trade for at least 50 trades.
> 3. Then go live at **0.25% risk per trade**.
>
> Not investment advice.

## Which strategy, for which capital

| Your capital | Trade these | Instrument |
|---|---|---|
| ₹1 lakh | A, B, C, D | **Liquid F&O stocks, cash intraday (MIS)**. One position at a time. Nifty futures are too big for this account. |
| ₹5 lakh | A, B, C, D | Liquid stocks, cash intraday. Max 2 positions. |
| ₹10 lakh | A, B, C on Nifty futures (1 lot) + D on stocks | Nifty futures only when the stop is 65–140 pts |
| ₹25 lakh+ | All four | Nifty / Bank Nifty futures + stocks. Total open risk ≤ 1%. |

## Rules for every strategy (non-negotiable)

| Rule | Value |
|---|---|
| Chart | 5-minute candles. Act only on a **closed** candle. |
| Risk per trade | **0.5% of capital** (0.25% for your first 100 live trades) |
| Size | Qty = (capital × 0.5%) ÷ (stop distance + costs per unit), **rounded down**. If it rounds to 0 → **no trade**. |
| Cost filter | Skip if round-trip costs + slippage > **25% of your risk**. For Nifty futures this means the stop must be **≥ 70 pts**. |
| Daily stop | **−1.5% of capital OR 3 losing trades** → done for the day |
| Weekly stop | **−3%** → done for the week |
| Monthly | **−6%** → half size next month |
| Drawdown | **−10% from peak** → stop live trading, go back to paper |
| Flat by | **15:10** |
| Never | Move a stop further away; add to a loser; trade 09:15–09:30 (except Strategy C on gap days); trade RBI policy before 10:30, Budget day or election-result day |

---

## A: ORB-VWAP Trend (opening-range breakout)

*Based on: Crabel, Fisher, Unger, plus the Zarattini/Aziz VWAP research.*

| | |
|---|---|
| **Use when** | Normal open (gap < 1× daily ATR). Opening range is 0.25–0.8× daily ATR wide. Not a Nifty expiry Tuesday (for Nifty). Yesterday was not a huge-range day (range < 1.5× ATR). |
| **Setup** | Opening range (OR) = high and low of 09:15–09:30 |
| **BUY when** | A 5-minute candle **closes above OR high + 0.05× ATR**, **and** above VWAP, between 09:30 and 11:30 |
| **SELL (short) when** | A candle **closes below OR low − 0.05× ATR**, **and** below VWAP, between 09:30 and 11:30 |
| **Stop** | The tighter of: the other side of the OR, or entry ∓ 0.5× ATR |
| **Target** | None. Let it run. |
| **Trail** | Exit on the first candle that **closes back across VWAP**. At +1R, move the stop to breakeven. |
| **Time exit** | 15:10 |
| **Max** | 1 long + 1 short per day |

## B: Holy Grail Pullback (trend continuation)

*Based on: Linda Raschke.*

| | |
|---|---|
| **Use when** | **ADX(14) on 15-minute chart ≥ 30** and the 20 EMA (5-minute) is sloping in the trade direction |
| **Setup** | In an uptrend, a 5-minute candle's low touches the **20 EMA** (mirror for a downtrend) |
| **BUY when** | The next candle **closes above the high of the touch candle**. Only the first or second pullback after ADX crosses 30. |
| **Stop** | Low of the touch candle |
| **Target** | Book 50% at **2R** or the previous swing high, whichever comes first |
| **Trail** | Trail the rest below the 20 EMA (on candle close) |
| **No entries** | After 14:30, or if ADX has been falling for 3 candles |
| **Max** | 2 trades per day |

## C: Failed-Breakout / Gap Reversal ("Oops" / "Turtle Soup")

*Based on: Raschke, Larry Williams, Fisher.*

| | |
|---|---|
| **Use when** | Range day or gap day. **Not** when 15-minute ADX ≥ 30 (strong trend). |
| **Setup** | Price trades **above yesterday's high by ≥ 0.1× ATR** (often on a gap-up) … |
| **SELL when** | … and within 30 minutes a 5-minute candle **closes back below yesterday's high**. Mirror for longs at yesterday's low. |
| **Stop** | Day's extreme + 0.05× ATR |
| **Target** | **VWAP**. If VWAP is less than **1.5R** away → **skip the trade.** |
| **Time stop** | Target not hit within 1 hour → exit |
| **Max** | 1 per instrument per day |

## D: NR7 Volatility Breakout (quiet day → big day)

*Based on: Toby Crabel, Larry Williams.*

| | |
|---|---|
| **Use when** | **Yesterday was NR7**, the smallest daily range of the last 7 days. Best on liquid F&O stocks. |
| **Setup** | Levels = today's open ± **0.5 × yesterday's range** |
| **BUY / SELL when** | The first 5-minute candle **closes beyond a level**, between 09:20 and 12:00. If the gap opened beyond the level → skip. |
| **Stop** | 0.5× ATR from entry, or the opposite level, whichever is nearer |
| **Exit** | 15:10. At +1R, move the stop to breakeven. |
| **Max** | 3 stocks per day, max 2 from the same sector. Skip on that stock's results day. |

---

## Daily decision (30 seconds)

```
Event day / loss limit hit?                         -> NO TRADE
Gap > 1x ATR?                                       -> only Strategy C (if the gap fails), else NO TRADE
09:30: OR 0.25-0.8x ATR, breaks with VWAP?          -> Strategy A
15-min ADX >= 30 and pullback to 20 EMA?            -> Strategy B
Stock was NR7 yesterday and breaks open +/- 0.5R?   -> Strategy D
Poke beyond yesterday's H/L that fails, VWAP >= 1.5R away? -> Strategy C
Nothing qualifies?                                  -> NO TRADE (a correct decision)
```

## Regime cheat sheet

| Strategy | Trend day | Sideways | High volatility | Low volatility | Gap day | Expiry (Tue) |
|---|---|---|---|---|---|---|
| A ORB-VWAP | Works well | Poor | Potentially suitable | Poor | Poor if gap > 1 ATR | Poor (skip for Nifty) |
| B Holy Grail | Works well | Poor | Potentially suitable | Poor | Potentially suitable after 10:00 | Morning only |
| C Reversal | Poor | Works well | Potentially suitable | Poor | **Works well** | Potentially suitable |
| D NR7 | Works well | Poor | Potentially suitable | Works well (as setup) | Poor | Poor |
