# trade
## Carver EWMAC scanner

`carver_scanner.py` is a Python port of the "Carver EWMAC Forecast" Pine indicator.
It scans the Nifty 500 and lists stocks whose combined forecast (the green line) is at +20.

```bash
pip install -r requirements.txt
python carver_scanner.py                       # monthly, forecast at +20 now
python carver_scanner.py --mode cross          # just crossed up to +20 this bar
python carver_scanner.py -t W --closed-only    # weekly, closed bars only
python carver_scanner.py --threshold 19.5      # near-touch tolerance
python carver_scanner.py --out hits.csv        # save results
```

## Backtest

`carver_backtest.py` backtests: enter when the forecast reaches 20, exit when it drops below 19,
orders filled at the next bar's open, Nifty 500 universe. Works on monthly, weekly and daily bars.

```bash
python carver_backtest.py -t M                 # results in backtest_results/monthly/
python carver_backtest.py -t W                 # results in backtest_results/weekly/
python carver_backtest.py -t D --exclude PATANJALI
python carver_backtest.py --start 2010-01-01 --cost 0.2
```

Yahoo's price data has bad ticks and unadjusted splits; one-day moves above +50% / below -33%
are treated as data errors and adjusted out before testing.
