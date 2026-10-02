# trade
## Monthly Ichimoku cloud-top crossover backtest (NIFTY 500)

- **Entry:** monthly close crosses above the Ichimoku cloud top (max of Span A/B, 9/26/52, displaced 26 bars)
- **Exit:** monthly close crosses below the cloud top
- **Fill:** next month's open (default) or the signal close (`--fill close`)

```bash
pip install -r requirements.txt
python ichimoku_monthly_backtest.py            # add --cost 0.5 for 0.5% round-trip, --refresh to re-download
```

Outputs go to `results/`: `trades.csv`, `per_symbol.csv`, `equity_curve.csv`, `portfolio_stats.csv`.

Caveat: the universe is today's NIFTY 500 list applied to all of history, so results carry survivorship bias.
