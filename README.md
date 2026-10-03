# trade

Backtest of a NIFTY weekly options buying strategy on **expiry days**, run on 1‑minute data.

## Strategy (as tested)

| Rule | Implementation |
|---|---|
| Entry | A 1‑min candle **closes ≥ 2× the previous candle's close** (premium doubles in one candle). The entry premium (that close) must be **between 20 and 50**. Buy at that close. Entries only up to 15:15. |
| Exit 1: target | Premium reaches **200**. Filled at 200, or at the open if the candle gaps above 200. |
| Exit 2: EMA | A 1‑min candle **closes below the 10 EMA** of that option's close. Exit at that close. |
| Stop loss | **Low of the candle before the entry candle**. Filled at the SL, or at the open if the candle gaps below it. |
| End of day | Any open position is squared off at the 15:29 close. |
| Sizing | 1 lot (75 for 2025 contracts, 65 for 2026), with about ₹60 per round trip for charges. |
| Conflicts | One position at a time. If several strikes signal in the same minute, the one with the biggest % jump is taken. If SL and target are both hit in one candle, the SL is assumed to come first. |

## Results

### 1 month: May 2026 expiries (5, 12, 19, 26 May)

**0 trades.** On these four expiry days no option priced 20–50 doubled inside a single 1‑minute candle. Every 1‑min doubling that day was in options priced from 0.05 to about 10. Even with the trigger relaxed to +50%, only 1–5 candles per day fell in the 20–50 band.

### Extended check: 47 expiry days, Aug 2025 to Jul 2026

| Metric | Value |
|---|---|
| Trades | 17 (on 12 of 47 expiry days) |
| Win rate | 41% (7 W / 10 L) |
| Exits | EMA10: 16, EOD: 1, **Target 200: 0**, SL: 0 |
| Total points | +16.75 |
| Net P&L (1 lot) | **≈ +₹634** after charges |
| Avg win / avg loss | ₹1,378 / −₹901 |

Full trade list: `results/trades_12m_strict.csv` and `results/12m_strict.txt`.

**Observations**
- The signal is rare, about once every 3 expiries, and nearly all signals come after 14:00 (the expiry‑day gamma window).
- The 200 target was never reached. The best trade went from 29.4 to 70.8. In practice the 10‑EMA close exit decides every trade.
- The stop loss (previous candle's low) is about 50–70% below entry, so the EMA exit always fires before it does.
- After slippage, the result is roughly breakeven. Fills on a candle that just doubled are optimistic, because spreads widen sharply in these moves.

## Run it

```bash
pip install pandas pyarrow
python download_data.py                       # May 2026 expiries (default)
python backtest.py                            # backtest May 2026
python download_data.py 2025-09-02 2025-09-09 # any other expiries
python backtest.py --start 2025-08-01 --end 2026-09-30
```

Useful flags: `--basis open` (measure "doubling" against the candle's own open), `--mult 1.5`, `--min-prem/--max-prem`, `--target`, `--ema`, `--lots`, `--charges`.

Data: [thetrademarkk/india-index-options-1m](https://huggingface.co/datasets/thetrademarkk/india-index-options-1m) (CC‑BY‑NC‑4.0, educational use; not redistributed here). Some expiries are missing from the dataset, for example 2025‑10‑20, 2026‑06‑09, 2026‑06‑16 to 30, and Aug 2026 onwards.
