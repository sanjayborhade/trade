# trade

Research and tooling for systematic intraday trading on NSE (India).

* [`research/indian_intraday_playbook.md`](research/indian_intraday_playbook.md): an evidence-labelled study of five documented day traders (Raschke, Crabel, Fisher, Williams, Unger). It adapts them to NSE's 2026 market structure and costs, and sets out four objective strategies, a backtesting framework, risk maths, a regime matrix, a daily playbook and a decision tree.
* [`tools/intraday_lab.py`](tools/intraday_lab.py): a standard-library-only Python cost model (post-April-2026 STT), position sizing, losing-streak and drawdown Monte Carlo, and a 5-minute bar backtester.

```bash
python3 tools/intraday_lab.py costs      # round-trip cost tables
python3 tools/intraday_lab.py risk       # losing-streak / recovery tables
python3 tools/intraday_lab.py ruin       # drawdown probabilities
python3 tools/intraday_lab.py selftest   # random-walk sanity check (should lose ~costs)
python3 tools/intraday_lab.py backtest your_5min.csv orb
```

Nothing here is investment advice, and no strategy is claimed to be profitable.
