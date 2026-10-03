# trade

Backtest of a NIFTY weekly options buying strategy on 1‑minute data.

## Strategy (as tested)

| Rule | Implementation |
|---|---|
| Universe | **Every trading day**, using the **current‑week (nearest) expiry** NIFTY options, all strikes. |
| Entry | A 1‑min candle **closes ≥ 2× the previous candle's close** (premium doubles in one candle), at **any premium**. Buy at that close. Entries only up to 15:15. |
| Exit 1: target | Premium reaches **200**. Filled at 200, or at the open if the candle gaps above 200. |
| Exit 2: EMA | A 1‑min candle **closes below the 10 EMA** of that option's close. Exit at that close. |
| Stop loss | **Low of the candle before the entry candle**. Filled at the SL, or at the open if the candle gaps below it. |
| End of day | Any open position is squared off at the 15:29 close. |
| Sizing | 1 lot (75 for 2025 contracts, 65 for 2026), with about ₹60 per round trip for charges. |
| Conflicts | One position at a time. If several strikes signal in the same minute, the one with the biggest % jump is taken. If SL and target are both hit in one candle, the SL is assumed to come first. |

## Results

### 1 month: May 2026 (19 trading days)

| Metric | Value |
|---|---|
| Trades | 162 |
| Winners | **0** |
| Exits | Stop loss: 136, EMA10: 26, Target: 0 |
| Net P&L (1 lot) | **−₹10,750** (gross −₹1,030; the rest is charges) |

- 156 of the 162 trades were bought at **0.50 or less**, typically a far out‑of‑the‑money option ticking 0.05 → 0.10. Those are tick noise, not real moves. They usually fall back one tick into the stop loss, and charges make every one a loss.
- 158 of the 162 trades came on expiry days. Away from expiry, current‑week premiums almost never double in a single minute.
- The largest entry all month was 4.65 (23350 PE on 12 May), and it lost too.

Trades: `results/trades_may2026.csv`, `results/may2026.txt`.

### Extended check: Aug 2025 to Jul 2026, same rules

1,285 trades, 8 winners (1%), net **−₹85,068**. 1,214 of the trades were entered at 0.50 or less, and 1,273 were on expiry days. The few real winners are the same 20–50‑premium trades from the earlier version. Because only one position is held at a time, the junk trades also sometimes block those good signals. Trades: `results/trades_12m.csv`, `results/12m.txt`.

### Earlier version (expiry days only, premium 20–50)

May 2026: 0 trades. Aug 2025 to Jul 2026: 17 trades, 41% win rate, net ≈ +₹634. See `results/*_strict.*`. To re‑run it:
`python backtest.py --expiry-only --min-prem 20 --max-prem 50`.

## Run it

```bash
pip install pandas pyarrow
python download_data.py 2026-05-05 2026-05-12 2026-05-19 2026-05-26 2026-06-02
python backtest.py --start 2026-05-01 --end 2026-05-31
python download_data.py 2025-09-02 2025-09-09 # any other expiries
python backtest.py --start 2025-08-01 --end 2026-09-30
```

Useful flags: `--expiry-only`, `--basis open` (measure "doubling" against the candle's own open), `--mult 1.5`, `--min-prem/--max-prem`, `--target`, `--ema`, `--lots`, `--charges`.

Data: [thetrademarkk/india-index-options-1m](https://huggingface.co/datasets/thetrademarkk/india-index-options-1m) (CC‑BY‑NC‑4.0, educational use; not redistributed here). Some expiries are missing from the dataset, for example 2025‑10‑20, 2026‑06‑09, 2026‑06‑16 to 30, and Aug 2026 onwards.
