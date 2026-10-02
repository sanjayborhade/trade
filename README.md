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

## Capped portfolio (max 20 stocks)

`carver_portfolio.py` simulates a real account: capital split into 20 slots, each new position gets
1/20 of current portfolio value, extra signals ranked by trend strength (or `--rank lowvol|random`).

```bash
python carver_portfolio.py -t W                # weekly, 20 slots -> backtest_results/portfolio_weekly/
python carver_portfolio.py -t M                # monthly
python carver_portfolio.py -t W --rank random --runs 20   # how much the ranking choice matters
python carver_portfolio.py -t W --max-pos 30 --cash-rate 6
```

## Live orders (monthly by default)

`carver_orders.py` reads your holdings from `my_portfolio.csv` and prints exactly what to SELL and BUY
at the next session's open for the 20-stock strategy (same rules as `carver_portfolio.py --fresh-only`).
Monthly: run after the close on the last trading day of the month. Weekly: `-t W`, Friday evening.

```
Symbol,Qty,BuyPrice,BuyDate
CASH,300000,,
TCS,4,3950,2026-08-03
```

```bash
python carver_orders.py                          # monthly, capital 300000, 20 slots of 15000
python carver_orders.py --monthly-limit 75000    # cap new buys per calendar month
python carver_orders.py --apply                  # also writes my_portfolio_next.csv with the planned changes
python carver_orders.py --as-of 2026-07-31       # replay a past month-end
python carver_orders.py -t W                     # weekly version
```
Defaults (edit the SETTINGS block at the top of the script): monthly, capital ₹3,00,000,
20 positions (slot ₹15,000), only stocks that crossed up to 20 on the latest bar are bought.
