"""
Friday-evening order generator for the Carver EWMAC "+20" strategy (20-stock portfolio).

Run it after the NSE close on Friday (or any time over the weekend / before
Monday's open). It reads YOUR holdings, checks every stock on the closed
weekly bar, and prints exactly what to SELL and BUY at Monday's open.

Rules (same as the backtest, carver_portfolio.py):
    SELL : a holding whose weekly forecast is below 19
    BUY  : stocks with forecast >= 20 that you don't hold, strongest trend
           first, until you have 20 positions; each gets 1/20 of the
           portfolio value (cash + holdings at Friday's close)

Your portfolio file (default my_portfolio.csv), one row per stock plus a CASH row:
    Symbol,Qty,BuyPrice,BuyDate
    CASH,250000,,
    TCS,10,3950,2026-08-03
    BEL,600,385.5,2026-07-20

After Monday's orders are filled, update this file with the real quantities,
prices and remaining cash (or run with --apply to write the planned changes
to a new file my_portfolio_next.csv that you can check and rename).

Usage
    python weekly_orders.py                      # weekly, 20 positions
    python weekly_orders.py --max-pos 20 --portfolio my_portfolio.csv
    python weekly_orders.py --apply              # also write my_portfolio_next.csv
    python weekly_orders.py -t M                 # monthly version (run after month end)
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import os
import sys
import time

import numpy as np
import pandas as pd
import yfinance as yf

from carver_backtest import CACHE_DIR, TF_NAME, clean_prices, download_ohlc, to_bars
from carver_scanner import Params, carver_forecast, load_nifty500

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
MARKET_DONE = dt.time(15, 45)   # NSE closes 15:30; give Yahoo a few minutes


# ---------------------------------------------------------------------------
#  Data: cached full history + fresh last few weeks
# ---------------------------------------------------------------------------
def update_prices(tickers: list[str], refresh: bool) -> dict[str, pd.DataFrame]:
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, "daily_ohlc.pkl")
    cache: dict = {} if refresh or not os.path.exists(path) else pd.read_pickle(path)

    missing = [t for t in tickers if cache.get(t) is None]
    if missing:
        print(f"Downloading full history for {len(missing)} tickers (first run takes a few minutes)...",
              file=sys.stderr)
        cache.update(download_ohlc(missing))

    have = [t for t in tickers if cache.get(t) is not None and t not in missing]
    if have:
        print(f"Updating last month of prices for {len(have)} tickers...", file=sys.stderr)
        for start in range(0, len(have), 100):
            batch = have[start:start + 100]
            for attempt in range(3):
                try:
                    data = yf.download(batch, period="1mo", interval="1d", auto_adjust=False,
                                       group_by="ticker", threads=True, progress=False)
                    break
                except Exception as exc:
                    print(f"  retrying ({exc})", file=sys.stderr)
                    time.sleep(5 * (attempt + 1))
            else:
                continue
            for t in batch:
                try:
                    new = (data[t] if isinstance(data.columns, pd.MultiIndex) else data)[["Open", "Close"]].dropna()
                except KeyError:
                    continue
                if new.empty:
                    continue
                new.index = pd.to_datetime(new.index).tz_localize(None)
                old = cache[t]
                cache[t] = pd.concat([old[old.index < new.index[0]], new])
    pd.to_pickle(cache, path)
    return {t: cache[t] for t in tickers if cache.get(t) is not None}


def closed_bars(daily: pd.DataFrame, tf: str, now: dt.datetime) -> pd.DataFrame:
    """Bars whose period has ended (a Friday-labelled week counts as closed after 15:45 IST Friday)."""
    bars = to_bars(daily, tf, drop_current=False)
    if bars.empty:
        return bars
    end = bars.index[-1].date()
    if tf == "D":
        end_ok = end < now.date() or now.time() >= MARKET_DONE
    else:
        end_ok = end < now.date() or (end == now.date() and now.time() >= MARKET_DONE)
    return bars if end_ok else bars.iloc[:-1]


# ---------------------------------------------------------------------------
#  Portfolio file
# ---------------------------------------------------------------------------
def read_portfolio(path: str) -> tuple[pd.DataFrame, float]:
    if not os.path.exists(path):
        pd.DataFrame({"Symbol": ["CASH"], "Qty": [500000], "BuyPrice": [None], "BuyDate": [None]}) \
            .to_csv(path, index=False)
        print(f"Created {path} with CASH = 500000. Edit it with your real cash and holdings, then run again.")
        sys.exit(0)
    df = pd.read_csv(path, dtype={"Symbol": str})
    df["Symbol"] = df["Symbol"].str.strip().str.upper().str.replace(".NS", "", regex=False)
    cash = float(df.loc[df["Symbol"] == "CASH", "Qty"].sum())
    hold = df[df["Symbol"] != "CASH"].copy()
    hold["Qty"] = hold["Qty"].astype(float)
    hold = hold[hold["Qty"] > 0]
    dup = hold["Symbol"][hold["Symbol"].duplicated()]
    if len(dup):
        sys.exit(f"Duplicate symbols in {path}: {', '.join(dup)} -- keep one row per stock.")
    return hold.reset_index(drop=True), cash


def main() -> None:
    ap = argparse.ArgumentParser(description="Print Monday's BUY/SELL orders for the 20-stock Carver portfolio")
    ap.add_argument("--timeframe", "-t", choices=["W", "M"], default="W", help="W=weekly (default), M=monthly")
    ap.add_argument("--portfolio", default="my_portfolio.csv")
    ap.add_argument("--max-pos", type=int, default=20)
    ap.add_argument("--entry", type=float, default=20.0)
    ap.add_argument("--exit", type=float, default=19.0)
    ap.add_argument("--cost", type=float, default=0.15, help="cost per side in %% used for cash estimates")
    ap.add_argument("--buffer", type=float, default=2.0,
                    help="%% of each buy kept back for a gap-up at the open (default 2)")
    ap.add_argument("--exclude", nargs="+", default=[], help="never buy these symbols")
    ap.add_argument("--list-file", help="local copy of ind_nifty500list.csv")
    ap.add_argument("--refresh", action="store_true", help="re-download full history for every stock")
    ap.add_argument("--apply", action="store_true", help="write the planned portfolio to <portfolio>_next.csv")
    args = ap.parse_args()

    tf = args.timeframe
    now = dt.datetime.now(IST)
    hold, cash = read_portfolio(args.portfolio)
    universe = load_nifty500(args.list_file)
    names = dict(zip(universe["Symbol"], universe["Company Name"]))
    symbols = list(dict.fromkeys(list(universe["Symbol"]) + list(hold["Symbol"])))
    prices = update_prices([f"{s}.NS" for s in symbols], args.refresh)

    p = Params()
    rows = []
    for s in symbols:
        daily = prices.get(f"{s}.NS")
        if daily is None:
            continue
        bars = closed_bars(clean_prices(daily), tf, now)
        if len(bars) < 3:
            continue
        f = carver_forecast(bars["Close"], tf, p)
        last = f.iloc[-1]
        if pd.isna(last["combined"]):
            continue
        rows.append({"Symbol": s, "BarDate": bars.index[-1].date(), "Close": float(bars["Close"].iloc[-1]),
                     "Forecast": float(last["combined"]), "Strength": float(last["strength"]),
                     "PrevForecast": float(f["combined"].iloc[-2]) if len(f) > 1 else np.nan,
                     "AnnVol%": float(last["ann_vol_pct"]), "InNifty500": s in names})
    sig = pd.DataFrame(rows).set_index("Symbol")
    bar_date = sig["BarDate"].mode().iloc[0]
    stale = sig[sig["BarDate"] != bar_date]

    # ---- current holdings -------------------------------------------------
    hold["Close"] = hold["Symbol"].map(sig["Close"])
    hold["Forecast"] = hold["Symbol"].map(sig["Forecast"])
    unknown = hold[hold["Close"].isna()]
    hold_value = float((hold["Qty"] * hold["Close"]).sum())
    total = cash + hold_value
    slot = total / args.max_pos

    sells = hold[hold["Forecast"] < args.exit]
    keep = hold[~hold.index.isin(sells.index)]
    cash_after_sells = cash + float((sells["Qty"] * sells["Close"]).sum()) * (1 - args.cost / 100)

    # ---- new buys -----------------------------------------------------------
    free = args.max_pos - len(keep)
    excluded = {x.upper() for x in args.exclude} | set(hold["Symbol"])
    cand = sig[(sig["Forecast"] >= args.entry - 1e-9) & (sig["BarDate"] == bar_date)
               & sig["InNifty500"] & ~sig.index.isin(excluded)].sort_values("Strength", ascending=False)

    buys, reserve, budget = [], [], cash_after_sells
    for s, r in cand.iterrows():
        if len(buys) >= free:
            reserve.append(s)
            continue
        alloc = min(slot, budget) * (1 - args.buffer / 100)
        qty = math.floor(alloc / (r["Close"] * (1 + args.cost / 100)))
        if qty < 1 or alloc < slot * 0.5 * (1 - args.buffer / 100):
            reserve.append(s)   # not enough cash, or one share costs more than a slot
            continue
        cost_est = qty * r["Close"] * (1 + args.cost / 100)
        budget -= cost_est
        fresh = "new" if not (r["PrevForecast"] >= args.entry - 1e-9) else "already at 20"
        buys.append({"Symbol": s, "Company": names.get(s, ""), "Qty": qty, "RefPrice": round(r["Close"], 2),
                     "Approx ₹": round(cost_est), "Strength": round(r["Strength"], 1),
                     "AnnVol%": round(r["AnnVol%"], 1), "Signal": fresh})

    # ---- report -----------------------------------------------------------
    label = TF_NAME[tf]
    when = "Monday's" if tf == "W" else "next session's"
    print("=" * 78)
    print(f" CARVER {label.upper()} ORDERS  --  signals from the bar closing {bar_date}, execute at {when} open")
    print(f" Generated {now:%Y-%m-%d %H:%M} IST   |   max {args.max_pos} positions")
    print("=" * 78)
    print(f" Portfolio value : ₹{total:,.0f}   (cash ₹{cash:,.0f} + holdings ₹{hold_value:,.0f})")
    print(f" Slot size       : ₹{slot:,.0f}  (1/{args.max_pos} of portfolio)")
    print(f" Positions       : {len(hold)} now -> {len(keep) + len(buys)} after orders\n")

    print(f"--- SELL at open ({len(sells)}) ---")
    if len(sells):
        t = sells.assign(**{"P&L%": ((sells["Close"] / sells["BuyPrice"] - 1) * 100).round(1)})
        print(t[["Symbol", "Qty", "BuyPrice", "Close", "P&L%", "Forecast"]].round(2).to_string(index=False))
    else:
        print("  nothing to sell")

    print(f"\n--- BUY at open ({len(buys)}) ---")
    if buys:
        print(pd.DataFrame(buys).to_string(index=False))
        print(f"  Estimated cash left after buys: ₹{budget:,.0f}")
        if len(buys) < free and len(cand) > len(buys):
            print(f"  {free - len(buys)} slot(s) left empty: not enough cash for a full slot "
                  f"(they fill automatically as positions are sold or cash is added)")
    elif free <= 0:
        print("  portfolio is full")
    else:
        print(f"  no new stock at +{args.entry:g} -- {free} slot(s) stay in cash (park it in a liquid fund)")
    if reserve:
        print(f"  Reserve list (if a buy fails / circuit-locked): {', '.join(reserve[:8])}")

    print(f"\n--- HOLD ({len(keep)}) ---")
    if len(keep):
        t = keep.assign(**{"P&L%": ((keep["Close"] / keep["BuyPrice"] - 1) * 100).round(1),
                           "Note": np.where(keep["Forecast"] < args.exit + 1.0, "close to exit", "")})
        print(t[["Symbol", "Qty", "BuyPrice", "Close", "P&L%", "Forecast", "Note"]].round(2)
              .sort_values("Forecast").to_string(index=False))

    if len(unknown):
        print(f"\n!! No price data for: {', '.join(unknown['Symbol'])} -- check the symbols in {args.portfolio}")
    if len(stale):
        print(f"\n!! {len(stale)} stocks have no bar for {bar_date} (suspended / no data), skipped: "
              f"{', '.join(stale.index[:10])}")
    if bar_date < (now.date() - dt.timedelta(days=10 if tf == 'W' else 40)):
        print("\n!! Latest closed bar looks old -- Yahoo data may be delayed; re-run later.")

    print("\nNotes: quantities use Friday's close; place LIMIT or market orders at the open and")
    print("update your portfolio file with the real fills. Signals are valid until the next close.")

    stamp = f"{bar_date:%Y-%m-%d}"
    orders = [{"Action": "SELL", "Symbol": r.Symbol, "Qty": int(r.Qty), "RefPrice": r.Close} for r in sells.itertuples()]
    orders += [{"Action": "BUY", "Symbol": b["Symbol"], "Qty": b["Qty"], "RefPrice": b["RefPrice"]} for b in buys]
    os.makedirs("orders", exist_ok=True)
    pd.DataFrame(orders, columns=["Action", "Symbol", "Qty", "RefPrice"]).to_csv(f"orders/orders_{stamp}.csv", index=False)
    print(f"Orders saved to orders/orders_{stamp}.csv")

    if args.apply:
        nxt = keep[["Symbol", "Qty", "BuyPrice", "BuyDate"]].copy()
        add = pd.DataFrame([{"Symbol": b["Symbol"], "Qty": b["Qty"], "BuyPrice": b["RefPrice"],
                             "BuyDate": "(fill date)"} for b in buys])
        cash_row = pd.DataFrame([{"Symbol": "CASH", "Qty": round(budget, 2)}])
        out = pd.concat([cash_row, nxt, add], ignore_index=True)
        path = os.path.splitext(args.portfolio)[0] + "_next.csv"
        out.to_csv(path, index=False)
        print(f"Planned portfolio written to {path} (prices are estimates -- fix them after the fills)")


if __name__ == "__main__":
    main()
