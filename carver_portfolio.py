"""
Capped-portfolio backtest of the Carver EWMAC "+20" strategy (Nifty 500).

Unlike carver_backtest.py (which holds EVERY stock that signals), this one
simulates a real account with a maximum number of positions:

    * capital is split into N slots (default 20)
    * a new position gets 1/N of the CURRENT portfolio value (cash permitting);
      positions are not rebalanced afterwards
    * ENTRY : forecast >= 20 at a bar's close and a slot is free -> buy next open
              (--fresh-only: only if it was below 20 on the bar before)
    * EXIT  : forecast <  19 at a bar's close                    -> sell next open
    * sells are done before buys, so a freed slot can be reused the same day
    * --once per-stock: never buy the same stock twice; --once first-ever: only its first-ever touch of 20
    * when more stocks qualify than there are free slots, they are ranked by:
        strength : strongest trend first (uncapped forecast)       [default]
        lowvol   : lowest annualised volatility first
        random   : random pick (use --seed / --runs to see the luck factor)
    * idle cash earns --cash-rate % a year (default 0)

Usage
    python carver_portfolio.py -t W                 # weekly, 20 slots
    python carver_portfolio.py -t M --rank lowvol
    python carver_portfolio.py -t W --rank random --runs 20
    python carver_portfolio.py -t W --max-pos 30 --cash-rate 6
"""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

from carver_backtest import BARS_PER_YEAR, TF_NAME, load_prices, perf, to_bars
from carver_scanner import Params, carver_forecast, load_nifty500


def build_panel(stocks: dict[str, pd.DataFrame], tf: str, p: Params) -> dict[str, pd.DataFrame]:
    """Aligned (dates x symbols) tables of open, close, forecast, strength and vol."""
    cols = {k: {} for k in ("open", "close", "fc", "strength", "vol")}
    for sym, daily in stocks.items():
        m = to_bars(daily, tf)
        if len(m) < 3:
            continue
        f = carver_forecast(m["Close"], tf, p)
        cols["open"][sym], cols["close"][sym] = m["Open"], m["Close"]
        cols["fc"][sym], cols["strength"][sym], cols["vol"][sym] = f["combined"], f["strength"], f["ann_vol_pct"]
    return {k: pd.DataFrame(v).sort_index() for k, v in cols.items()}


def simulate(panel: dict[str, pd.DataFrame], start: pd.Timestamp, max_pos: int, entry: float,
             exit_: float, cost: float, rank: str, cash_rate: float, per_year: int,
             seed: int = 0, fresh_only: bool = False, once: str = "no") -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    dates = panel["close"].index[panel["close"].index >= start]
    # once="first-ever": the date each stock's forecast FIRST reached the entry level in its whole history
    full_f = panel["fc"]
    first_touch = (full_f >= entry - 1e-9).idxmax().where((full_f >= entry - 1e-9).any())
    first_touch = first_touch.reindex(panel["close"].columns)
    is_first = np.zeros((len(dates), len(panel["close"].columns)), dtype=bool)
    pos = {d: i for i, d in enumerate(dates)}
    for j, d in enumerate(first_touch.to_numpy()):
        if pd.notna(d) and pd.Timestamp(d) in pos:
            is_first[pos[pd.Timestamp(d)], j] = True
    traded = np.zeros(len(panel["close"].columns), dtype=bool)  # once="per-stock"
    syms = np.array(panel["close"].columns)
    O = panel["open"].loc[dates].to_numpy()
    C = panel["close"].loc[dates].ffill().to_numpy()   # last known close for marking
    F = panel["fc"].loc[dates].to_numpy()
    S = panel["strength"].loc[dates].to_numpy()
    V = panel["vol"].loc[dates].to_numpy()
    has_bar = ~np.isnan(panel["close"].loc[dates].to_numpy())

    cash, equity = 1.0, 1.0
    shares = np.zeros(len(syms))
    entry_info: dict[int, tuple] = {}
    to_buy: list[int] = []
    to_sell: list[int] = []
    trades, curve = [], []
    cash_growth = (1 + cash_rate / 100) ** (1 / per_year) - 1

    for t, d in enumerate(dates):
        cash *= 1 + cash_growth
        # 1) execute yesterday's decisions at today's open (sells first)
        for j in list(to_sell):
            if np.isnan(O[t, j]):
                continue  # no trade today, try next bar
            cash += shares[j] * O[t, j] * (1 - cost)
            e_d, e_px, e_fc = entry_info.pop(j)
            trades.append({"Symbol": syms[j], "EntryBar": e_d.date(), "EntryPrice": round(e_px, 2),
                           "ExitBar": d.date(), "ExitPrice": round(O[t, j], 2),
                           "Return%": round(((O[t, j] * (1 - cost)) / (e_px * (1 + cost)) - 1) * 100, 2),
                           "Status": "closed"})
            shares[j] = 0
            to_sell.remove(j)
        slot_value = equity / max_pos
        for j in to_buy:
            if (shares > 0).sum() >= max_pos or np.isnan(O[t, j]):
                continue
            alloc = min(slot_value, cash)
            if alloc < slot_value * 0.5:   # not enough cash for a meaningful position
                continue
            shares[j] = alloc / (O[t, j] * (1 + cost))
            traded[j] = True
            cash -= alloc
            entry_info[j] = (d, O[t, j], F[t - 1, j] if t else np.nan)
        to_buy = []

        # 2) mark to market at the close
        held = shares > 0
        equity = cash + np.nansum(shares[held] * C[t, held])
        curve.append({"Date": d, "Equity": equity, "Positions": int(held.sum()), "Cash%": cash / equity * 100})

        # 3) decide orders for the next open, using this bar's closed forecast
        new_sells = [j for j in np.where(held)[0] if has_bar[t, j] and F[t, j] < exit_]
        to_sell = list(dict.fromkeys(to_sell + new_sells))  # keep sells that could not fill yet
        free = max_pos - int(held.sum()) + len(to_sell)
        if free > 0:
            ok = ~held & has_bar[t] & (F[t] >= entry - 1e-9)
            if fresh_only:  # only stocks that crossed up to the entry level on THIS bar
                prev = F[t - 1] if t else np.full(len(syms), np.nan)
                ok &= ~(prev >= entry - 1e-9)
            if once == "per-stock":   # never buy a stock a second time
                ok &= ~traded
            elif once == "first-ever":  # only on the first-ever touch of 20
                ok &= is_first[t]
            cand = np.where(ok)[0]
            if len(cand):
                if rank == "strength":
                    cand = cand[np.argsort(-S[t, cand])]
                elif rank == "lowvol":
                    cand = cand[np.argsort(V[t, cand])]
                else:
                    cand = rng.permutation(cand)
                to_buy = list(cand[:free])

    for j in np.where(shares > 0)[0]:
        e_d, e_px, _ = entry_info[j]
        trades.append({"Symbol": syms[j], "EntryBar": e_d.date(), "EntryPrice": round(e_px, 2),
                       "ExitBar": dates[-1].date(), "ExitPrice": round(C[-1, j], 2),
                       "Return%": round((C[-1, j] / (e_px * (1 + cost)) - 1) * 100, 2),
                       "Status": "exit pending" if j in to_sell else "open"})
    return pd.DataFrame(curve).set_index("Date"), pd.DataFrame(trades)


def summarize(curve: pd.DataFrame, trades: pd.DataFrame, bench: pd.Series | None, per_year: int) -> dict:
    r = curve["Equity"].pct_change().fillna(0)
    out = perf(r, per_year)
    y = curve["Equity"].resample("YE").last()
    y = y.pct_change().fillna(y.iloc[0] - 1)
    out["Worst year"] = f"{y.min() * 100:.1f}%"
    out["Losing years"] = f"{(y < 0).sum()}/{len(y)}"
    if bench is not None:
        b = bench.reindex(curve.index).ffill().resample("YE").last()
        b = b.pct_change().fillna(b.iloc[0] / bench.reindex(curve.index).ffill().iloc[0] - 1)
        out["Years beating index"] = f"{(y > b).sum()}/{len(y)}"
    yrs = (curve.index[-1] - curve.index[0]).days / 365.25
    rt = trades["Return%"] / 100
    out["Trades / year"] = f"{len(trades) / yrs:.0f}"
    out["Win rate"] = f"{(rt > 0).mean() * 100:.1f}%"
    out["Avg positions"] = f"{curve['Positions'].mean():.1f}"
    out["Avg cash"] = f"{curve['Cash%'].mean():.0f}%"
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Capped-portfolio backtest of the Carver +20 strategy")
    ap.add_argument("--timeframe", "-t", choices=["D", "W", "M"], default="W")
    ap.add_argument("--max-pos", type=int, default=20, help="maximum open positions (default 20)")
    ap.add_argument("--fresh-only", action="store_true",
                    help="buy only stocks that crossed up to the entry level on the latest bar "
                         "(default: any stock currently at the entry level)")
    ap.add_argument("--once", choices=["no", "per-stock", "first-ever"], default="no",
                    help="per-stock: buy each stock at most once (no repeat trades); "
                         "first-ever: buy only on the first time its forecast EVER reached 20")
    ap.add_argument("--rank", choices=["strength", "lowvol", "random"], default="strength",
                    help="which stocks to buy when more qualify than free slots")
    ap.add_argument("--runs", type=int, default=1, help="with --rank random: number of random runs")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--entry", type=float, default=20.0)
    ap.add_argument("--exit", type=float, default=19.0)
    ap.add_argument("--cost", type=float, default=0.15, help="cost per side in %% (default 0.15)")
    ap.add_argument("--cash-rate", type=float, default=0.0, help="annual %% earned on idle cash (default 0)")
    ap.add_argument("--start", default="2005-10-01", help="simulation start date (default 2005-10-01)")
    ap.add_argument("--symbols", nargs="+")
    ap.add_argument("--exclude", nargs="+", default=["PATANJALI"],
                    help="symbols to leave out (default: PATANJALI, untradeable circuit run in 2020)")
    ap.add_argument("--list-file")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--out-dir", help="default: backtest_results/portfolio_<timeframe>")
    args = ap.parse_args()

    tf, per_year = args.timeframe, BARS_PER_YEAR[args.timeframe]
    symbols = [s.upper() for s in args.symbols] if args.symbols else list(load_nifty500(args.list_file)["Symbol"])
    symbols = [s for s in symbols if s not in {x.upper() for x in args.exclude}]
    stocks, bench = load_prices(symbols, args.refresh)
    panel = build_panel(stocks, tf, Params())
    bench_close = to_bars(bench, tf)["Close"] if bench is not None else None
    start = pd.Timestamp(args.start)

    runs = args.runs if args.rank == "random" else 1
    results, last = {}, None
    for k in range(runs):
        curve, trades = simulate(panel, start, args.max_pos, args.entry, args.exit, args.cost / 100,
                                 args.rank, args.cash_rate, per_year, seed=args.seed + k,
                                 fresh_only=args.fresh_only, once=args.once)
        results[f"run {k + 1}" if runs > 1 else "Strategy"] = summarize(curve, trades, bench_close, per_year)
        last = (curve, trades)
    curve, trades = last

    print(f"##### {TF_NAME[tf]}, max {args.max_pos} positions, rank={args.rank}, "
          f"entries={'fresh crosses only' if args.fresh_only else 'any stock at 20'}"
          f"{'' if args.once == 'no' else ', once=' + args.once}, "
          f"cost {args.cost}%/side, cash {args.cash_rate}%/yr #####")
    print(f"Period: {curve.index[0]:%Y-%m-%d} to {curve.index[-1]:%Y-%m-%d}\n")
    table = pd.DataFrame(results)
    if bench_close is not None:
        b = bench_close.reindex(curve.index).ffill().pct_change().fillna(0)
        table["Nifty 500 index"] = pd.Series(perf(b, per_year))
    if runs > 1:
        cagr = table.loc["CAGR", [c for c in table.columns if c.startswith("run")]].str.rstrip("%").astype(float)
        dd = table.loc["Max drawdown", [c for c in table.columns if c.startswith("run")]].str.rstrip("%").astype(float)
        print(f"Random ranking over {runs} runs: CAGR min {cagr.min():.1f}% / median {cagr.median():.1f}% / "
              f"max {cagr.max():.1f}%,  max drawdown worst {dd.min():.1f}% / median {dd.median():.1f}%\n")
        table = table[["run 1", "Nifty 500 index"]] if "Nifty 500 index" in table else table[["run 1"]]
    print(table.fillna("").to_string())

    yearly = curve["Equity"].resample("YE").last()
    yearly = (yearly.pct_change().fillna(yearly.iloc[0] - 1) * 100).round(1)
    yearly.index = yearly.index.year
    print("\n=== Return by year (%) ===")
    print(yearly.to_frame("Strategy").T.to_string())

    out_dir = args.out_dir or os.path.join("backtest_results", f"portfolio_{TF_NAME[tf].lower()}")
    os.makedirs(out_dir, exist_ok=True)
    curve.to_csv(os.path.join(out_dir, "equity.csv"))
    trades.sort_values("EntryBar").to_csv(os.path.join(out_dir, "trades.csv"), index=False)
    open_now = trades[trades["Status"] != "closed"].sort_values("EntryBar")
    print(f"\n=== Current holdings ({len(open_now)}) ===")
    print(open_now[["Symbol", "EntryBar", "EntryPrice", "ExitPrice", "Return%", "Status"]].to_string(index=False))
    print(f"\nSaved to {out_dir}/", file=sys.stderr)


if __name__ == "__main__":
    main()
