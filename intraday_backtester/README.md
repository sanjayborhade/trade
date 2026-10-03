# Intraday Backtester: NSE / Nifty 500 one-minute data

A complete, tested Python system for answering one question honestly:

> **Does this strategy have a robust, repeatable intraday edge in Indian equities after realistic
> transaction costs and execution?**

It **never** tells you a strategy is profitable. It computes results from **your** data and gives a
verdict such as `NO EDGE AFTER COSTS`, `NOT ROBUST / INCONCLUSIVE` or `CANDIDATE EDGE (requires forward
testing)`, and explains why.

---

## 1. Windows quick start (exact commands)

**1. Install Python** (3.10–3.14) from <https://www.python.org/downloads/windows/>. During installation,
tick **"Add python.exe to PATH"**.

**2. Open a terminal in the project folder.** In File Explorer, open the `intraday_backtester` folder,
click the address bar, type `cmd` and press Enter.

**3. Create the virtual environment and install dependencies.** Either double-click `setup.bat`, or run:

```bat
py -3 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m pytest -q
```

The last command runs 26 self-tests (fills, costs, date parsing, look-ahead detection). They should all pass.

> PowerShell users: if `activate` is blocked, run
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, or use `cmd` instead, or call
> `.venv\Scripts\python.exe` directly. `run.bat <script> <args>` does this for you.

**4. Point the system at your data.** Either copy your CSVs into `data\raw\`, or edit `config.yaml`:

```yaml
data:
  DATA_FOLDER: "E:/STUDY/Market/Learning/algo/nifty500_1min"   # forward slashes work on Windows
```

Optional but recommended: also set `benchmarks.NIFTY50_FILE` (and/or `NIFTY500_FILE`) to 1-minute index CSVs.

**5. Configure.** Edit `config.yaml`: capital, risk, costs, slippage, limits. Every setting is commented.

**6. Inspect your data, then run a small test:**

```bat
python inspect_data.py --sample 10        :: quick look at 10 files (nothing cached)
python inspect_data.py                    :: full inspection + builds the fast Parquet cache
python check_lookahead.py                 :: look-ahead test on 3 of YOUR stocks
python backtest.py --strategy orb --limit 20
```

**7. Run one strategy:**

```bat
python backtest.py --strategy orb
python backtest.py --strategy vwap --universe liquid
```

**8. Run all strategies:**

```bat
python backtest.py --strategy all
```

**9. Run the optimiser** (development data only):

```bat
python optimize.py --strategy orb
python optimize.py --strategy orb --walk-forward
:: only ONCE, with parameters fixed in advance:
python optimize.py --strategy orb --final --params-file results\<run>\orb\recommended_params.yaml
```

**10. Find the reports.** Each run creates `results\<date_time>_<what>\`, and `results\LATEST.txt` holds
the newest path. Open `<strategy>\report.html` in your browser.

---

## 2. Project structure

```text
intraday_backtester/
├── config.yaml              all settings (paths, capital, risk, costs, limits, strategies, periods)
├── requirements.txt
├── setup.bat / run.bat      Windows helpers
├── inspect_data.py          data-quality inspection (step 1)
├── prepare_data.py          CSV -> cleaned Parquet cache (backtest.py also does this automatically)
├── check_lookahead.py       look-ahead bias test on your data
├── backtest.py              run strategies with a realistic portfolio
├── optimize.py              robust optimisation, validation, walk-forward, final test
├── ibt/
│   ├── config.py            defaults + validation of config.yaml
│   ├── data/loader.py       column detection, timestamp parsing, cleaning, quality stats
│   ├── data/store.py        Parquet cache, benchmarks (index files + equal-weight universe)
│   ├── features.py          causal indicators (VWAP, ATR, opening range, EMA/ADX on 5/15-min, RVOL...)
│   ├── strategies/          base.py + 10 strategies (one file each)
│   ├── engine/
│   │   ├── execution.py     fill rules (next-candle execution, gaps, slippage, spread)
│   │   ├── simulator.py     stage 1: signals -> candidate trades per stock (parallel)
│   │   ├── portfolio.py     stage 2: one account, all stocks, all limits
│   │   ├── costs.py         Indian intraday charges
│   │   ├── risk.py          position sizing
│   │   └── backtester.py    multiprocessing orchestration
│   ├── analysis/            performance, regimes, periods, charts, reports + verdict
│   ├── optimize.py          plateau ranking, validation, walk-forward
│   └── validation.py        future-perturbation look-ahead detector
├── tests/                   pytest suite + synthetic data generator
├── data/raw/                your CSVs (or set DATA_FOLDER)
├── data/cache/              created automatically
└── results/                 created automatically
```

---

## 3. Assumptions about your CSV files, and how they are handled

| Situation | Handling |
|---|---|
| Column names (`Date`, `datetime`, `timestamp`, `Open`, `close`, `Volume`, `vol`, `Symbol`, `ticker`...) | Matched case-insensitively against lists in `data.columns`. Add your own names there. |
| Separate date and time columns | Combined automatically |
| Date formats | ISO `2024-01-05 09:15`, `05-01-2024 09:15` (day first, Indian), `05/01/2024`, `05-Jan-2024`, `20240105 0915`, `+05:30` / `Z` time zones, Unix epoch seconds or milliseconds. Year-first dates are never treated as day-first. Set `datetime_format` if needed. |
| Candles stamped at the **end** of the minute (09:16 ... 15:30) | Auto-detected and shifted to start-of-minute (`timestamp_is_bar_end: auto`) |
| One file per stock / per stock per year / sub-folders / one file with all stocks | All supported. Symbol from a symbol column, else the file name (prefixes/suffixes such as `NSE_`, `_1min`, `-EQ` are stripped). |
| Duplicates | Removed (`duplicate_policy: last`) and counted |
| Zero / negative / missing prices | Rows dropped and counted |
| High below open/close etc. | Repaired (or dropped) and counted |
| Missing volume | Set to 0 and counted. If most volume is missing, VWAP becomes time-weighted and volume filters cannot pass. |
| Missing candles | Not invented. Orders fill on the next candle that exists. Days with fewer than `min_bars_per_day` (300) candles are excluded. |
| Candles outside 09:15–15:30 (pre-open etc.) | Dropped and counted |
| Big unadjusted overnight gaps (splits/bonuses) | Overnight moves > 25% are reported. That stock is not traded for 20 days after. **Use split-adjusted data if you can.** |
| Very large files | Read in chunks. Each worker holds one stock at a time. |

Every one of these is reported per stock in `data_quality_by_symbol.csv` and summarised by `inspect_data.py`.
**Read that report before trusting any backtest.** Example: a day/month swap shows up as "days falling on
weekends".

---

## 4. Execution model and look-ahead prevention

### Timing (explicit)

```text
candle i closes  -> strategy evaluates its signal using candles 0..i only
                 -> order is created
candle i+1 (the next candle that exists that day) -> order can execute
```

A breakout seen in the 10:15 candle can **never** be filled at the 10:15 open; the earliest fill is at
10:16 or later.

| Order / event | Fill price |
|---|---|
| Market entry | Open of the next candle + slippage + half the spread |
| Stop entry (breakout) | First later candle whose high trades **through** the level (by `trade_through_ticks`). Filled at `max(open, level)`, so a gap through the level fills at the worse open. Then slippage + half spread. |
| Limit entry | First later candle whose low trades through the level; filled at `min(open, level)`; no slippage |
| Stop-loss | Candle low ≤ stop. Filled at `min(open, stop)` (gaps fill worse) − slippage − half spread. |
| Target | Candle high trades through the target. Filled at the target. |
| Stop **and** target in one candle | The path inside a candle is unknown, so the stop is assumed first (`same_bar_stop_target`) |
| Entry candle of stop/limit orders | Only the stop is checked (assume the worst); targets from the next candle |
| Square-off / time exit / indicator exit | At the open of the next candle (e.g. 15:15), with slippage + spread |
| Position size | Capped at `MAX_PARTICIPATION` × the fill candle's volume (default 5%), i.e. a partial-fill model |

### How look-ahead is prevented

* Indicators use only candles up to the current one: running VWAP, `ewm`/`rolling` without centring.
* Daily values (previous high/low/close, ATR, NR7, average volume) come from **completed** days only
  (shifted by one day).
* The opening range is `NaN` until its last candle has closed.
* 5/15-minute candles are visible only from the 1-minute candle that **completes** them.
* "Normal volume for this minute" is the average of the **previous** 20 days.
* Universe/liquidity filters use data up to **yesterday**.
* Regime labels (bull/bear, volatility) use benchmark data up to **yesterday**.
* **It is tested, not just claimed.** `ibt/validation.py` scrambles all data after random cut-off
  times (stock and benchmark) and requires every signal, order level and trade before the cut to be
  identical. It catches planted leaks: using the day's final high, peeking one candle ahead, centred
  moving averages. All 10 strategies pass. Run it on your data with `python check_lookahead.py`.
* **Survivorship bias:** using today's Nifty 500 list for the past is optimistic. Provide
  `universe.MEMBERSHIP_FILE` (`symbol,start_date,end_date`) with historical constituents if you have it;
  the backtest warns when you don't.

---

## 5. Transaction costs (equity intraday on NSE)

All values are in `config.yaml → costs:`. Check them against your broker's contract note.

| Charge | Applies to | Default |
|---|---|---|
| Brokerage | each executed order | min(0.03% × value, ₹20) |
| STT | **sell** side only | 0.025% |
| Exchange transaction charge | both sides | 0.00307% |
| SEBI fee | both sides | ₹10 per crore |
| Stamp duty | **buy** side only | 0.003% |
| GST | on brokerage + exchange + SEBI | 18% |
| IPFT | both sides (optional) | 0 |
| Slippage | each market/stop fill | 0.05% (try 0.01 / 0.02 / 0.10) |
| Bid-ask spread | half paid on each market/stop fill | 0.04% full spread |

Reports separate **theoretical P&L** (signal prices), **gross P&L** (after slippage and spread, before
charges) and **net P&L**, so you can see exactly what kills an edge.

---

## 6. Portfolio simulation

Candidate trades from all stocks are replayed **in time order through one account**. If 20 stocks signal
at 10:15, they are ranked (strategy score by default) and taken until a limit blocks them:
`MAX_OPEN_POSITIONS`, one position per stock, `MAX_DAILY_TRADES`, `MAX_DAILY_LOSS` (realised loss + open
risk), `MAX_PORTFOLIO_EXPOSURE`, `MAX_POSITION_VALUE_PCT/ABS` and `MAX_PARTICIPATION`.

Quantity = equity × `RISK_PER_TRADE` ÷ (stop distance + estimated costs per share), rounded down. Equity is
the **realised** equity at that moment. Every refusal is counted in the report's "funnel".

Known simplification: stage 1 decides each stock's trade sequence assuming earlier trades were taken. If
the portfolio refuses an earlier trade, a later same-day signal on that stock is not re-generated.

---

## 7. Strategies

| Name (`--strategy`) | Idea | Research basis |
|---|---|---|
| `orb` | Opening range (5/15/30 min) breakout; stop orders or close confirmation; optional volume, ATR stops, R target, trails | Crabel; Fisher ACD; Zarattini & Aziz 2023 |
| `vwap_momentum` (`vwap`) | Above VWAP + 5-min EMA trend + N-candle breakout + volume; VWAP trail | Zarattini/Barbon/Aziz 2024 (VWAP trail) |
| `trend_pullback` (`pullback`) | 15-min ADX trend, first pullbacks to 5-min EMA20, stop above the pullback candle | Raschke "Holy Grail" |
| `relative_strength` (`rs`) | Stock vs Nifty 500 / Nifty 50 / equal-weight universe since the open; new day high above VWAP | Relative strength; Gao et al. 2018 |
| `failed_breakout` (`mean_reversion`) | Poke beyond yesterday's high/low that fails → fade to VWAP | Raschke Turtle Soup; Williams Oops; Fisher failed-A |
| `nr7_breakout` (`nr7`) | After NR7 days, break of open ± k × yesterday's range | Crabel; Williams |
| `first_candle_breakout` | First 5-min candle breakout of 3–10% gainers (your rules) | User rules |
| `pdh_breakout` (`pdh`) | Break of yesterday's high/low with volume above VWAP | Market structure |
| `gap_reversal` (`gap`) | Gap 1–4% fails at the opening range → target gap fill | Williams Oops |
| `vwap_reversion` | **Experimental**: stretched from VWAP + RSI extreme on low-ADX days | Weak; excluded from `all` unless `--include-experimental` |

`python backtest.py --list` shows every parameter. You asked for "top 20". Ten are implemented because
these are the ones the research supports with objective rules; others (gap-and-go, inside-bar, 52-week
high...) would mostly duplicate these. Add more as described in section 12.

**The defaults are research starting points, not optimised values.**

---

## 8. Development / validation / out-of-sample, walk-forward, and overfitting

* **Periods** are cut from the dates actually in your data: by default 60% development, 20% validation,
  20% test (`periods:`). You can set explicit dates instead.
* **In-sample (development):** where you are allowed to look and choose.
* **Out-of-sample (validation):** used once to check a choice made on development.
* **Test:** the final exam, used with `optimize.py --final`, for one parameter set chosen in advance.
  Looking at it repeatedly turns it into development data.
* **Walk-forward** (`--walk-forward`): choose on 24 months, test on the next 6, roll forward, then stitch
  the unseen results together. That stitched result is the closest thing to "how it would have done live".
* **Overfitting:** with enough parameter combinations something always looks great by chance. Defences
  built in:
  * ranking by a blend of metrics instead of profit;
  * a **plateau score** (a set's score averaged with its neighbours), so isolated spikes lose;
  * a requirement that the neighbours' development expectancy is also positive;
  * a minimum trade count;
  * a warning for grid parameters that change nothing;
  * reporting where the single highest-profit set ended up on validation (usually worse).
* **Parameter stability:** see the `heatmap_*.png` files. Look for broad green regions, not single cells.

---

## 9. Reading the output

Each strategy folder contains:

| File | Contents |
|---|---|
| `report.html` / `report.md` | Verdict, all statistics, robustness checks, signal funnel, tables, charts |
| `trades.csv` / `trades.parquet` | Every trade: ID, date, symbol, strategy, side, signal/entry/exit times and prices, qty, stop, target, theoretical/gross/net P&L, costs, R multiple, holding time, regimes, exit reason, MFE/MAE... |
| `daily_equity.csv` | Daily equity, P&L and trades |
| `tables/` | `by_period`, `by_year`, `by_symbol`, `monthly_pnl`, `by_time_of_day`, `by_regime_*`, `by_setup`, `by_exit_reason` |
| `charts/` | Equity (net vs gross, periods shaded), drawdown, monthly, yearly, P&L distribution, R distribution, win/loss, P&L by hour, by stock, by regime |
| `summary.json` | Machine-readable summary |

**The verdict checks:**
* net P&L after costs;
* whether costs or slippage removed a gross edge;
* trade count;
* the 95% bootstrap confidence interval of mean R;
* each period, especially the out-of-sample test;
* dependence on the top 5 stocks;
* the share of profitable years.

**Regimes per trade:**
* bull / bear / sideways and high / low volatility, from the benchmark as of yesterday;
* gap up / down;
* Nifty expiry day (weekday rules in `regimes.expiry_rules`: Thursday, then Tuesday from 2025-09-01);
* high / normal / low volume versus normal for that time of day.

---

## 10. Performance

Measured on one stock with 5 years of 1-minute data (469k candles): features 0.35 s, all 10 strategies
2.5 s, about 160 MB per worker. The portfolio stage handled 583,000 candidate trades in 5 s. Estimated
time for the full Nifty 500 × 5 years × all strategies: **about 5–10 minutes on 4 cores**, after the
one-time CSV→Parquet preparation.

**For very large datasets:**
* Run `prepare_data.py` once. Parquet loads 10–50× faster than CSV.
* Set `data.workers` to your core count minus 1.
* Use `--universe liquid` (fewer, more tradable stocks) and `--limit` while experimenting.
* Optimisation cost grows with grid size × stocks. Start small (`--grid`), keep `optimization.batch_size`
  around 8, and check memory in Task Manager.
* Store data on an SSD.

---

## 11. Errors and logs

Each run writes a `.log` file in its results folder. The program stops with a clear message for:
* config errors (with the exact field);
* a missing data folder or files;
* missing columns (with the columns it found);
* unparseable timestamps.

It reports and continues for:
* a bad file;
* a stock with no usable data;
* a strategy error on one stock (with traceback in the log);
* a benchmark that is missing for relative strength.

It also reports no-trade results (with the funnel showing why) and insufficient history.

---

## 12. Adding a strategy

Create `ibt/strategies/my_idea.py`:

```python
import numpy as np
from .base import BaseStrategy, Signals

class MyIdea(BaseStrategy):
    name = "my_idea"
    title = "My idea"
    research_basis = "why this should work"
    default_params = {"lookback": 20, "stop_atr": 0.5, "target_r": 2.0,
                      "entry_start": "09:30", "entry_cutoff": "14:00"}

    def generate_signal(self, f):                 # f = ibt.features.Frame (causal arrays)
        hh = f.rolling_high_before(int(self.p["lookback"]))
        long = (f.close > hh) & (f.close > f.vwap)
        return Signals(long=long, short=np.zeros(f.n, bool), order_type="market",
                       score=np.nan_to_num(f.rvol_cum))

    def calculate_stop(self, f, i, side, fill):
        return fill - side * self.p["stop_atr"] * f.atr[i]
```

Register it in `ibt/strategies/__init__.py`, then run `python -m pytest -q`. The look-ahead test covers
every registered strategy automatically.

---

## 13. Limitations (read these)

* Candle data cannot show the order of prices inside a minute, so fills are modelled conservatively, not
  exactly.
* Slippage and spread are percentages. Real costs are higher for small caps, at the open and on news. Test
  0.10% slippage before believing anything.
* No market-impact model beyond the volume-participation cap.
* Shorting assumes intraday (MIS) shorts are always available.
* Unadjusted corporate actions distort ATR and gaps. Excluded windows reduce, but do not remove, the problem.
* Without a historical membership file the stock list has survivorship bias.
* A backtest that passes every check is still only a hypothesis. Paper-trade it next.
