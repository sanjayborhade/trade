# trade: Indian Intraday Strategy Kit

Research, four objective intraday strategies for NSE, and a backtest/risk toolkit.
**Needs only Python 3.9+.** No pip installs. Not investment advice; no strategy is claimed to be profitable.

## What's inside

> **New: [`intraday_backtester/`](intraday_backtester/README.md)** is a production backtesting system for your own Nifty 500 **1-minute** data. It includes a realistic portfolio, Indian costs, look-ahead tests, development/validation/test periods, walk-forward and robust optimisation. Start with its README.

| File | What it is |
|---|---|
| **`STRATEGIES.md`** | **Start here.** One-page cards for the 4 strategies (A–D): exact entry, stop, exit, sizing and when not to trade |
| `research/indian_intraday_playbook.md` | Full research: 5 documented traders, Indian market adaptation (2026 costs and rules), backtest framework, risk maths, regime matrix, playbook, decision tree, sources |
| `tools/intraday_lab.py` | Cost calculator, position sizing, loss-streak/drawdown maths, backtester for the 4 strategies |
| `tools/fetch_yahoo.py` | Downloads the last ~60 days of 5-minute candles into `data/` (`--nifty500` for all Nifty 500 stocks) |
| `tools/orb_first_candle.py` | Backtester for the **first 5-minute candle breakout** (3–10% gainers) on a stock universe |
| `research/backtest_first_candle_orb.md` | **Results: first-candle breakout on Nifty 500, Jul–Sep 2026** |
| `research/backtest_results_2026-09.md` | **Results: 1-month (Sep 2026) backtest of all 4 strategies** |
| `templates/daily_checklist.md` | Printable pre-market → post-market checklist |
| `templates/trade_journal.csv` | Journal template (open in Excel / Google Sheets) |
| `sample_data/synthetic_5min.csv` | **Synthetic** random data, only to show the CSV format |
| `run.sh` / `run.bat` | One-click launchers (Mac/Linux / Windows) |

## Quick start

```bash
# Mac / Linux                          # Windows (Command Prompt)
./run.sh costs                         run.bat costs
./run.sh risk                          run.bat risk
./run.sh selftest                      run.bat selftest
```

(On Windows, if `python` is not found, install Python 3 from python.org and tick "Add to PATH".)

## Backtest on your own data

1. Export **5-minute or 1-minute** candles (1-minute is auto-converted to 5-minute) from your broker, TradingView or a data vendor. Column names are detected automatically: `Date`/`Time`/`Timestamp`/`datetime`, `Open`/`High`/`Low`/`Close`/`Volume` in any case, separate date and time columns, ISO, dd-mm-yyyy or epoch times. Extra indicator columns are ignored. The standard header is:
   `datetime,open,high,low,close,volume` (e.g. `2026-01-05 09:15:00,26150,26180,26120,26170,123456`).
   Use at least 2–5 years, IST times, sorted oldest first.
2. Run:

```bash
./run.sh backtest my_nifty_5min.csv --instrument nifty --capital 1000000 --risk 0.5
./run.sh backtest my_stock_5min.csv --instrument stock --capital 500000 --strategy nr7
./run.sh backtest sample_data/synthetic_5min.csv --capital 2500000            # demo on the sample file
./run.sh trades   my_nifty_5min.csv --strategy orb --out orb_trades.csv   # every trade, for Excel
```

| Option | Values |
|---|---|
| `--strategy` | `all`, `orb` (A), `holygrail` (B), `fade` (C), `nr7` (D) |
| `--instrument` | `nifty` (lot 65), `banknifty` (lot 30), `stock` (cash intraday) |
| `--capital` | Capital in ₹ |
| `--risk` | Risk per trade in % (default 0.5) |
| `--slippage` | Per-side slippage in points/₹ |
| `--start` / `--end` | Only trade between these dates (YYYY-MM-DD); earlier data warms up ATR/ADX |
| `--lots` | Always trade exactly N lots (ignores capital/risk sizing) |

Costs use the post-1-April-2026 STT schedule. Edit `RATES` at the top of `tools/intraday_lab.py` if your broker's charges differ.

## First-candle breakout on Nifty 500

```bash
python3 tools/fetch_yahoo.py --nifty500                      # ~1 minute, about 115 MB
python3 tools/orb_first_candle.py --data data --variants      # all Nifty 500 stocks
python3 tools/orb_first_candle.py --data data --start 2026-09-01 --min-gain 3 --max-gain 10
```
Options: `--capital`, `--risk`, `--slippage` (% per side), `--last-entry 14:30`, `--max-per-day 3`, `--out trades.csv`.
