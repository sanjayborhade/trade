# trade: Indian Intraday Strategy Kit

Research, four objective intraday strategies for NSE, and a backtest/risk toolkit.
**Needs only Python 3.9+.** No pip installs. Not investment advice; no strategy is claimed to be profitable.

## What's inside

| File | What it is |
|---|---|
| **`STRATEGIES.md`** | **Start here.** One-page cards for the 4 strategies (A–D): exact entry, stop, exit, sizing and when not to trade |
| `research/indian_intraday_playbook.md` | Full research: 5 documented traders, Indian market adaptation (2026 costs and rules), backtest framework, risk maths, regime matrix, playbook, decision tree, sources |
| `tools/intraday_lab.py` | Cost calculator, position sizing, loss-streak/drawdown maths, backtester for the 4 strategies |
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

1. Export **5-minute** candles from your broker or data vendor to CSV with this header:
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

Costs use the post-1-April-2026 STT schedule. Edit `RATES` at the top of `tools/intraday_lab.py` if your broker's charges differ.
