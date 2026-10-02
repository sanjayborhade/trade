"""Research: strategy variations for the fresh-cross 20-stock Carver portfolio.

Usage:  python carver_experiments.py M      # built-in list of variants, monthly
        python carver_experiments.py W      # weekly
        python carver_experiments.py M "{'mine': dict(regime='index_sma', regime_exit=True, reentry=True)}"

Columns: CAGR (full, 2005-2015, 2016-2026), max drawdowns, Sharpe, trades per year,
average positions. Costs 0.25%/side, idle cash earns 6%/yr.
"""
import sys
import numpy as np
import pandas as pd
from carver_backtest import load_prices, to_bars
from carver_scanner import Params, load_nifty500
from carver_portfolio import build_panel

TF = sys.argv[1] if len(sys.argv) > 1 else "M"
PER_YEAR = {"M": 12, "W": 52}[TF]

syms = [s for s in load_nifty500()["Symbol"] if s != "PATANJALI"]
stocks, bench = load_prices(syms, False)
panel = build_panel(stocks, TF, Params())
MINFRAC = 0.5
START = pd.Timestamp("2005-10-01")
dates = panel["close"].index[panel["close"].index >= START]
O = panel["open"].loc[dates].to_numpy()
Craw = panel["close"].loc[dates].to_numpy()
C = panel["close"].loc[dates].ffill().to_numpy()
F = panel["fc"].loc[dates].to_numpy()
S = panel["strength"].loc[dates].to_numpy()
V = panel["vol"].loc[dates].to_numpy()
HAS = ~np.isnan(Craw)
Fprev = panel["fc"].shift(1).loc[dates].to_numpy()
valid = ~np.isnan(F)
BREADTH = np.where(valid.sum(1) > 0, (F > 0).sum(1) / np.maximum(valid.sum(1), 1), np.nan)
bidx = to_bars(bench, TF)["Close"] if bench is not None else None
IDX = bidx.reindex(dates).ffill().to_numpy()
IDX_SMAS = {L: bidx.rolling(L).mean().reindex(dates).ffill().to_numpy() for L in (6, 8, 10, 12, 15, 30, 40, 50)}


def sim(entry=20, exit_=19, max_pos=20, rank="strength", regime=None, regime_th=0.5,
        regime_exit=False, sizing="equal", stop=None, trail=None, cost=0.0025, cash_rate=6.0,
        min_hold=0, sma=10, reentry=False):
    n = len(syms_arr)
    cash = equity = 1.0
    shares = np.zeros(n); epx = np.zeros(n); peak = np.zeros(n); ebar = np.zeros(n, int)
    to_buy, to_sell = [], []
    was_on = True
    eq = []
    ntr = 0
    g = (1 + cash_rate / 100) ** (1 / PER_YEAR) - 1
    for t in range(len(dates)):
        cash *= 1 + g
        for j in list(to_sell):
            if np.isnan(O[t, j]):
                continue
            cash += shares[j] * O[t, j] * (1 - cost); shares[j] = 0; to_sell.remove(j)
        slot = equity / max_pos
        for j in to_buy:
            if (shares > 0).sum() >= max_pos or np.isnan(O[t, j]):
                continue
            a = slot
            if sizing == "invvol":
                med = np.nanmedian(V[t - 1][valid[t - 1]]) if t else np.nan
                a = slot * np.clip(med / V[t - 1, j], 0.5, 1.5) if t and V[t - 1, j] > 0 else slot
            a = min(a, cash)
            if a < slot * MINFRAC:
                continue
            shares[j] = a / (O[t, j] * (1 + cost)); cash -= a
            epx[j] = O[t, j]; peak[j] = O[t, j]; ebar[j] = t; ntr += 1
        to_buy = []
        held = shares > 0
        equity = cash + np.nansum(shares[held] * C[t, held])
        eq.append((equity, held.sum()))
        peak[held] = np.maximum(peak[held], C[t, held])
        # regime
        if regime == "breadth":
            on = not (BREADTH[t] < regime_th)
        elif regime == "index_sma":
            on = not (IDX[t] < IDX_SMAS[sma][t])
        else:
            on = True
        sell = []
        for j in np.where(held)[0]:
            if not HAS[t, j]:
                continue
            if t - ebar[j] < min_hold:
                continue
            x = F[t, j] < exit_
            if stop is not None and C[t, j] < epx[j] * (1 - stop):
                x = True
            if trail is not None and C[t, j] < peak[j] * (1 - trail):
                x = True
            if regime_exit and not on:
                x = True
            if x:
                sell.append(j)
        to_sell = list(dict.fromkeys(to_sell + sell))
        prev_on, was_on = was_on, on
        free = max_pos - int(held.sum()) + len(to_sell)
        if free > 0 and on:
            fresh = ~(Fprev[t] >= entry - 1e-9)
            if reentry and not prev_on:  # market just turned back on: stocks still at 20 qualify too
                fresh = np.ones_like(fresh)
            ok = ~held & HAS[t] & (F[t] >= entry - 1e-9) & fresh
            cand = np.where(ok)[0]
            if len(cand):
                if rank == "strength":
                    cand = cand[np.argsort(-S[t, cand])]
                elif rank == "lowvol":
                    cand = cand[np.argsort(V[t, cand])]
                elif rank == "strength_per_vol":
                    cand = cand[np.argsort(-(S[t, cand] / V[t, cand]))]
                to_buy = list(cand[:free])
    e = pd.DataFrame(eq, index=dates, columns=["Equity", "Pos"])
    return e, ntr


syms_arr = np.array(panel["close"].columns)


def stats(e, ntr):
    eqs = e["Equity"]
    def cagr(x):
        y = (x.index[-1] - x.index[0]).days / 365.25
        return ((x.iloc[-1] / x.iloc[0]) ** (1 / y) - 1) * 100
    r = eqs.pct_change().dropna()
    dd = (eqs / eqs.cummax() - 1).min() * 100
    yrs = (eqs.index[-1] - eqs.index[0]).days / 365.25
    return {"CAGR": cagr(eqs), "IS 05-15": cagr(eqs.loc[:"2015"]), "OOS 16-26": cagr(eqs.loc["2015-12":]),
            "MaxDD": dd, "DD 16-26": ((eqs.loc["2016":] / eqs.loc["2016":].cummax() - 1).min() * 100),
            "Sharpe": r.mean() / r.std() * np.sqrt(PER_YEAR), "Tr/yr": ntr / yrs, "Pos": e["Pos"].mean()}


variants = {
    "BASE (20/19, 20 pos, strength)": {},
    # exit level
    "exit < 17": dict(exit_=17), "exit < 15": dict(exit_=15), "exit < 10": dict(exit_=10),
    "exit < 5": dict(exit_=5), "exit < 0": dict(exit_=0),
    # entry level
    "entry 18 (fresh cross of 18)": dict(entry=18), "entry 15": dict(entry=15),
    # positions
    "10 positions": dict(max_pos=10), "15 positions": dict(max_pos=15), "25 positions": dict(max_pos=25),
    "30 positions": dict(max_pos=30),
    # ranking / sizing
    "rank strength/vol": dict(rank="strength_per_vol"), "rank lowvol": dict(rank="lowvol"),
    "inverse-vol sizing": dict(sizing="invvol"),
    # regime filters
    "breadth>50% to buy": dict(regime="breadth", regime_th=0.5),
    "breadth>40% to buy": dict(regime="breadth", regime_th=0.4),
    "breadth>50% buy & sell-all": dict(regime="breadth", regime_th=0.5, regime_exit=True),
    "breadth>40% buy & sell-all": dict(regime="breadth", regime_th=0.4, regime_exit=True),
    "index>10m SMA to buy": dict(regime="index_sma"),
    "index>10m SMA buy & sell-all": dict(regime="index_sma", regime_exit=True),
    # stops
    "stop-loss 15%": dict(stop=0.15), "stop-loss 25%": dict(stop=0.25),
    "trailing stop 25%": dict(trail=0.25), "trailing stop 35%": dict(trail=0.35),
    "min hold 3 bars": dict(min_hold=3),
}
if __name__ == "__main__":
    extra = {}
    if len(sys.argv) > 2:
        extra = eval(sys.argv[2])
        variants = {k: {**v} for k, v in extra.items()}
    rows = {}
    for k, v in variants.items():
        rows[k] = stats(*sim(**v))
    df = pd.DataFrame(rows).T.round(2)
    with pd.option_context("display.width", 200):
        print(df.to_string())
