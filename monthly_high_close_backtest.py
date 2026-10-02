"""
Backtest: MONTHLY HIGH == MONTHLY CLOSE  (NIFTY 500, monthly timeframe)

ENTRY
    Signal month: close >= high * (1 - tol), at least 10 trading sessions,
    high > low, and close above the previous month's close.
    Buy at the open of the first trading day of the next month. If that
    open is already at or below the stop-loss, the trade is skipped.

STOP-LOSS (both exits)
    Low of the signal (entry) candle. Checked on daily bars: a gap below the
    stop fills at that day's open, otherwise at the stop price.

EXIT (two separate backtests)
    1. TARGET : target = entry * (1 + signal-month growth), where growth is
                the signal month's close vs the previous month's close.
                Checked on daily bars (gap above target fills at the open).
                If target and stop are both inside one day's range, the stop
                is assumed to have hit first (conservative).
    2. EMA10  : a monthly close below the 10-period EMA of monthly closes;
                exit at the next month's first open.

PORTFOLIO VIEWS
    A. Fixed amount per trade (--per-trade, default Rs 1,00,000), unlimited
       capital: total invested vs P&L, peak capital needed, CAGR on that
       peak capital, and concurrent positions per year.
    B. Compounding portfolio (--capital, default Rs 10,00,000) with at most
       --max-pos positions (default 20), each sized at equity / max-pos at
       entry. When there are more signals than free slots, the strongest
       signal-month growth goes first. Monthly mark-to-market equity gives
       CAGR, max drawdown and positions held per year.

Costs: --cost % per round trip (default 0.3).

Usage
    pip install pandas numpy yfinance requests
    python monthly_high_close_backtest.py --tol 0.5
"""

import argparse
import os
import time
from datetime import date

import numpy as np
import pandas as pd
import requests
import yfinance as yf

NIFTY500_URL = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
CACHE_DIR = "data_cache"
RESULTS_DIR = "results"


# --------------------------------------------------------------------------- data
def get_nifty500_symbols():
    path = os.path.join(CACHE_DIR, "nifty500.csv")
    if not os.path.exists(path):
        resp = requests.get(NIFTY500_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        resp.raise_for_status()
        with open(path, "w") as f:
            f.write(resp.text)
    df = pd.read_csv(path)
    return [s.strip() + ".NS" for s in df["Symbol"]]


def download_daily(tickers, start):
    path = os.path.join(CACHE_DIR, f"daily_{start}.pkl")
    if os.path.exists(path):
        return pd.read_pickle(path)
    frames = {}
    for i in range(0, len(tickers), 50):
        chunk = tickers[i:i + 50]
        for attempt in range(4):
            try:
                raw = yf.download(chunk, start=start, interval="1d", auto_adjust=True,
                                  group_by="ticker", threads=True, progress=False)
                break
            except Exception as e:  # rate limit / network
                print(f"  retry {attempt + 1} for chunk {i}: {e}")
                time.sleep(2 ** (attempt + 1))
        else:
            continue
        for t in chunk:
            if t in raw.columns.get_level_values(0):
                d = raw[t][["Open", "High", "Low", "Close"]].dropna()
                if len(d):
                    frames[t] = d
        print(f"  downloaded {min(i + 50, len(tickers))}/{len(tickers)}")
    pd.to_pickle(frames, path)
    return frames


def clean_daily(d):
    """Remove Yahoo data errors. NSE stocks move at most ~20% a day, so:
    1. drop one-off spike bars that sit >50% away from the 7-day median;
    2. if a jump of >+100% / <-60% remains (unadjusted split, demerger, bad
       series), keep only the history after the last such jump."""
    px = d[["Open", "High", "Low", "Close"]]
    ratio = px.div(d["Close"].rolling(7, center=True, min_periods=1).median(), axis=0)
    d = d[~((ratio > 1.5) | (ratio < 0.67)).any(axis=1)]
    prev = d["Close"].shift()
    jump = pd.concat([d["Close"] / prev - 1, d["Open"] / prev - 1], axis=1)
    breaks = d.index[(jump.max(axis=1) > 1.0) | (jump.min(axis=1) < -0.6)]
    if len(breaks):
        d = d[d.index >= breaks[-1]]
    return d


def prepare(daily):
    """Daily bars trimmed to completed months, plus monthly bars built from them."""
    d = daily.copy()
    d.index = pd.to_datetime(d.index).tz_localize(None)
    month_start = pd.Timestamp(date.today()).replace(day=1)
    d = d[d.index < month_start]  # drop the current, still-incomplete month
    d = clean_daily(d)
    d["Days"] = 1
    m = d.resample("ME").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last",
                              "Days": "sum"}).dropna()
    m = m[m["Days"] > 0]
    return d.drop(columns="Days"), m


# ----------------------------------------------------------------------- backtest
def backtest_symbol(sym, d, m, tol, exit_mode, cost):
    """Returns (trades, live_signals, skipped_gap_count) for one stock."""
    mo, mh, ml, mc = (m[k].values for k in ("Open", "High", "Low", "Close"))
    mdays = m["Days"].values
    ema10 = m["Close"].ewm(span=10, adjust=False).mean().values
    months = m.index.to_period("M")
    month_pos = {p: k for k, p in enumerate(months)}

    O, H, L, C = (d[k].values for k in ("Open", "High", "Low", "Close"))
    dates = d.index
    dmonth = np.array([month_pos[p] for p in dates.to_period("M")])
    first_day = {}
    for k, mi in enumerate(dmonth):
        first_day.setdefault(mi, k)
    month_end_day = np.r_[dmonth[1:] != dmonth[:-1], True]

    trades, live, skipped = [], [], 0
    n = len(m)
    i = 1
    while i < n:
        growth = mc[i] / mc[i - 1] - 1
        signal = (mc[i] >= mh[i] * (1 - tol) and mdays[i] >= 10
                  and mh[i] > ml[i] and growth > 0)
        if not signal:
            i += 1
            continue
        sl = ml[i]
        if i + 1 >= n:  # signal on the last completed month -> buy next month
            live.append({"symbol": sym.replace(".NS", ""), "signal_month": str(months[i]),
                         "close": round(mc[i], 2), "stop_loss": round(sl, 2),
                         "growth_%": round(growth * 100, 2),
                         "target": round(mc[i] * (1 + growth), 2),
                         "risk_%": round((1 - sl / mc[i]) * 100, 2)})
            break

        e = first_day[i + 1]
        e_px = O[e]
        if e_px <= sl:  # opened below the stop: no trade
            skipped += 1
            i += 1
            continue
        tgt = e_px * (1 + growth)

        x = x_px = None
        reason = "OPEN"
        for k in range(e, len(O)):
            if k > e and O[k] <= sl:
                x, x_px, reason = k, O[k], "SL"
            elif exit_mode == "target" and k > e and O[k] >= tgt:
                x, x_px, reason = k, O[k], "TARGET"
            elif L[k] <= sl:
                x, x_px, reason = k, sl, "SL"
            elif exit_mode == "target" and H[k] >= tgt:
                x, x_px, reason = k, tgt, "TARGET"
            elif exit_mode == "ema" and month_end_day[k] and C[k] < ema10[dmonth[k]]:
                if k + 1 < len(O):
                    x, x_px, reason = k + 1, O[k + 1], "EMA"
                else:
                    reason = "OPEN (EMA exit due)"
            if x is not None or reason != "OPEN":
                break

        if x is None:
            x, x_px = len(O) - 1, C[-1]
        trades.append({
            "symbol": sym.replace(".NS", ""),
            "signal_month": str(months[i]),
            "signal_growth_%": round(growth * 100, 2),
            "entry_date": dates[e].date(),
            "entry_price": round(e_px, 2),
            "stop_loss": round(sl, 2),
            "target_price": round(tgt, 2) if exit_mode == "target" else np.nan,
            "exit_date": dates[x].date(),
            "exit_price": round(x_px, 2),
            "exit_reason": reason,
            "days_held": (dates[x] - dates[e]).days,
            "return_%": round((x_px / e_px - 1 - cost) * 100, 2),
            "status": "OPEN" if reason.startswith("OPEN") else "CLOSED",
        })
        if reason.startswith("OPEN"):
            break
        # a new signal can come from the exit month onwards
        i = max(i + 1, dmonth[x])
    return trades, live, skipped


# --------------------------------------------------------------------- portfolios
def month_ends(trades, last_month):
    first = pd.Period(trades.entry_date.min(), "M")
    return pd.period_range(first, last_month, freq="M")


def fixed_amount_view(t, per_trade, last_month):
    """Every trade gets the same rupee amount; capital is unlimited."""
    t = t.copy()
    t["entry_p"] = pd.PeriodIndex(pd.to_datetime(t.entry_date), freq="M")
    t["exit_p"] = pd.PeriodIndex(pd.to_datetime(t.exit_date), freq="M")
    t["pnl"] = per_trade * t["return_%"] / 100
    cal = month_ends(t, last_month)
    held = np.array([((t.entry_p <= p) & ((t.exit_p > p) | (t.status == "OPEN"))).sum()
                     for p in cal])
    conc = pd.Series(held, index=cal)

    years = len(cal) / 12
    peak_cap = conc.max() * per_trade
    total_pnl = t.pnl.sum()
    cagr = ((peak_cap + total_pnl) / peak_cap) ** (1 / years) - 1 if peak_cap else np.nan

    t["entry_y"] = t.entry_p.dt.year
    t["exit_y"] = t.exit_p.dt.year
    closed = t[t.status == "CLOSED"]
    by_year = pd.DataFrame({
        "trades_entered": t.groupby("entry_y").size(),
        "invested_Rs": t.groupby("entry_y").size() * per_trade,
        "booked_PnL_Rs": closed.groupby("exit_y").pnl.sum(),
        "avg_held": conc.groupby(conc.index.year).mean(),
        "max_held": conc.groupby(conc.index.year).max(),
    }).fillna(0)
    by_year.index.name = "year"
    summary = {
        "total_invested": len(t) * per_trade,
        "total_pnl": total_pnl,
        "booked_pnl": closed.pnl.sum(),
        "open_mtm_pnl": t[t.status == "OPEN"].pnl.sum(),
        "peak_capital": peak_cap,
        "years": years,
        "cagr": cagr,
    }
    return summary, by_year


def compounding_view(t, monthly, capital, max_pos, cost, last_month):
    """Monthly-stepped portfolio with a slot limit and equal-weight sizing."""
    t = t.copy()
    t["entry_p"] = pd.PeriodIndex(pd.to_datetime(t.entry_date), freq="M")
    t["exit_p"] = pd.PeriodIndex(pd.to_datetime(t.exit_date), freq="M")
    closes = {s.replace(".NS", ""): m["Close"].set_axis(m.index.to_period("M"))
              for s, m in monthly.items()}
    cal = month_ends(t, last_month)
    by_entry = {p: g.sort_values("signal_growth_%", ascending=False)
                for p, g in t.groupby("entry_p")}

    cash, equity_prev = capital, capital
    open_pos = []  # dicts: symbol, shares, exit_p, exit_price, status
    curve, held, taken, skipped = [], [], [], 0
    for p in cal:
        # entries at this month's first open
        for _, tr in by_entry.get(p, pd.DataFrame()).iterrows():
            if len(open_pos) >= max_pos:
                skipped += 1
                continue
            alloc = min(equity_prev / max_pos, cash)
            if alloc <= 0:
                skipped += 1
                continue
            shares = alloc / tr.entry_price
            cash -= alloc
            open_pos.append({"symbol": tr.symbol, "shares": shares, "exit_p": tr.exit_p,
                             "exit_price": tr.exit_price, "status": tr.status,
                             "alloc": alloc})
            taken.append(tr)
        # exits during this month -> cash
        still = []
        for pos in open_pos:
            if pos["status"] == "CLOSED" and pos["exit_p"] == p:
                cash += pos["shares"] * pos["exit_price"] * (1 - cost)
            else:
                still.append(pos)
        open_pos = still
        # month-end mark to market
        mtm = 0.0
        for pos in open_pos:
            s = closes[pos["symbol"]]
            px = s.get(p, s[:p].iloc[-1] if len(s[:p]) else pos["alloc"] / pos["shares"])
            mtm += pos["shares"] * px
        equity_prev = cash + mtm
        curve.append(equity_prev)
        held.append(len(open_pos))

    eq = pd.Series(curve, index=cal)
    hs = pd.Series(held, index=cal)
    years = len(cal) / 12
    dd = (eq / eq.cummax() - 1).min()
    yearly = pd.DataFrame({
        "end_equity_Rs": eq.groupby(eq.index.year).last(),
        "avg_held": hs.groupby(hs.index.year).mean(),
        "max_held": hs.groupby(hs.index.year).max(),
    })
    start_eq = yearly["end_equity_Rs"].shift(1).fillna(capital)
    yearly.insert(0, "start_equity_Rs", start_eq)
    yearly["return_%"] = (yearly.end_equity_Rs / yearly.start_equity_Rs - 1) * 100
    yearly.index.name = "year"
    summary = {
        "start": capital, "final": eq.iloc[-1], "years": years,
        "cagr": (eq.iloc[-1] / capital) ** (1 / years) - 1,
        "max_dd": dd, "trades_taken": len(taken), "signals_skipped_full": skipped,
    }
    return summary, yearly, eq


# ------------------------------------------------------------------------- report
def rs(x):
    return f"Rs {x:,.0f}"


def report(t, skipped_gap, fa, fa_year, cp, cp_year, label, args):
    print("\n" + "=" * 78)
    print(f"  {label}")
    print("=" * 78)
    closed = t[t.status == "CLOSED"]
    r = closed["return_%"]
    wins, losses = r[r > 0], r[r <= 0]
    pf = wins.sum() / abs(losses.sum()) if losses.sum() else np.inf
    print(f"  Trades                  : {len(t)}  (closed {len(closed)}, open {len(t) - len(closed)})"
          f"; skipped, opened below SL: {skipped_gap}")
    print(f"  Exit reasons            : " + ", ".join(f"{k} {v}" for k, v in
                                                   t.exit_reason.value_counts().items()))
    print(f"  Win rate (closed)       : {len(wins) / max(len(closed), 1) * 100:.1f}%")
    print(f"  Avg / median return     : {r.mean():.2f}% / {r.median():.2f}%")
    print(f"  Avg win / avg loss      : {wins.mean():.2f}% / {losses.mean():.2f}%")
    print(f"  Profit factor           : {pf:.2f}")
    print(f"  Avg / median days held  : {closed.days_held.mean():.0f} / {closed.days_held.median():.0f}")

    print(f"\n  A) Fixed {rs(args.per_trade)} per trade, unlimited capital")
    print(f"     Total invested       : {rs(fa['total_invested'])}  ({len(t)} trades)")
    print(f"     Total P&L            : {rs(fa['total_pnl'])}  (booked {rs(fa['booked_pnl'])}, "
          f"open MTM {rs(fa['open_mtm_pnl'])})")
    print(f"     Return on invested   : {fa['total_pnl'] / fa['total_invested'] * 100:.1f}%")
    print(f"     Peak capital needed  : {rs(fa['peak_capital'])}")
    print(f"     CAGR on peak capital : {fa['cagr'] * 100:.1f}%  over {fa['years']:.1f} yrs")
    print("\n" + fa_year.astype({"trades_entered": int, "max_held": int}).round(1)
          .to_string(float_format=lambda v: f"{v:,.1f}").replace("\n", "\n     ").join(["     ", ""]))

    print(f"\n  B) Compounding portfolio: {rs(cp['start'])} start, max {args.max_pos} positions")
    print(f"     Final equity         : {rs(cp['final'])}")
    print(f"     CAGR                 : {cp['cagr'] * 100:.1f}%  over {cp['years']:.1f} yrs")
    print(f"     Max drawdown         : {cp['max_dd'] * 100:.1f}%")
    print(f"     Trades taken/skipped : {cp['trades_taken']} / {cp['signals_skipped_full']} (no free slot)")
    print("\n" + cp_year.round(1).to_string(float_format=lambda v: f"{v:,.1f}")
          .replace("\n", "\n     ").join(["     ", ""]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2005-01-01")
    ap.add_argument("--tol", type=float, default=0.0,
                    help="%% tolerance: close >= high*(1-tol). 0 = exact (default)")
    ap.add_argument("--cost", type=float, default=0.3, help="round-trip cost in %%")
    ap.add_argument("--per-trade", type=float, default=100_000)
    ap.add_argument("--capital", type=float, default=1_000_000)
    ap.add_argument("--max-pos", type=int, default=20)
    args = ap.parse_args()

    os.makedirs(CACHE_DIR, exist_ok=True)
    os.makedirs(RESULTS_DIR, exist_ok=True)
    tol = args.tol / 100 if args.tol > 0 else 1e-9  # tiny epsilon for float rounding
    cost = args.cost / 100

    syms = get_nifty500_symbols()
    print(f"NIFTY 500 symbols: {len(syms)}; loading daily data from {args.start} ...")
    data = {s: prepare(d) for s, d in download_daily(syms, args.start).items()}
    data = {s: dm for s, dm in data.items() if len(dm[1]) >= 12}
    monthly = {s: dm[1] for s, dm in data.items()}
    last_month = max(m.index[-1] for m in monthly.values()).to_period("M")
    print(f"Stocks with >=12 months of data: {len(data)}; last completed month {last_month}")

    tag = f"tol{args.tol:g}"
    for exit_mode, label in [("target", "EXIT 1: TARGET = SIGNAL-MONTH GROWTH, SL = SIGNAL CANDLE LOW"),
                             ("ema", "EXIT 2: MONTHLY CLOSE BELOW 10 EMA, SL = SIGNAL CANDLE LOW")]:
        trades, live, skipped = [], [], 0
        for s, (d, m) in data.items():
            tr, lv, sk = backtest_symbol(s, d, m, tol, exit_mode, cost)
            trades += tr
            live += lv
            skipped += sk
        t = pd.DataFrame(trades)
        fa, fa_year = fixed_amount_view(t, args.per_trade, last_month)
        cp, cp_year, eq = compounding_view(t, monthly, args.capital, args.max_pos, cost, last_month)
        report(t, skipped, fa, fa_year, cp, cp_year,
               f"{label}  (tol={args.tol}%, cost={args.cost}%)", args)
        t.to_csv(os.path.join(RESULTS_DIR, f"trades_{exit_mode}_{tag}.csv"), index=False)
        fa_year.to_csv(os.path.join(RESULTS_DIR, f"yearly_fixed_{exit_mode}_{tag}.csv"))
        cp_year.to_csv(os.path.join(RESULTS_DIR, f"yearly_portfolio_{exit_mode}_{tag}.csv"))
        eq.rename("equity").to_csv(os.path.join(RESULTS_DIR, f"equity_{exit_mode}_{tag}.csv"))
        if live:  # stocks not already in a trade that signalled on the last month
            pd.DataFrame(live).sort_values("growth_%", ascending=False).to_csv(
                os.path.join(RESULTS_DIR, f"live_signals_{exit_mode}_{tag}.csv"), index=False)
            print(f"\n  New signals on {last_month} close (buy next month open): {len(live)}"
                  f" -> results/live_signals_{exit_mode}_{tag}.csv")



if __name__ == "__main__":
    main()
