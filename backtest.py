"""
Backtest: NIFTY weekly options "premium doubling" momentum buy.

Rules
-----
ENTRY  : a 1-min candle whose close is >= 2x the previous 1-min candle's close
         (premium doubles in one candle), any premium. Buy at that close.
         Every trading day is tested, using the current-week (nearest) expiry.
         Optional filters: --min-prem/--max-prem, --expiry-only.
EXIT 1 : TARGET - premium reaches 200 (candle high >= 200 -> filled at 200).
EXIT 2 : a 1-min candle closes below the 10 EMA of the option's close.
STOP   : low of the candle before the entry candle (hit if a candle's low <= SL).
EOD    : any open position is squared off at the 15:29 candle close.

Fill assumptions (conservative)
-------------------------------
* If SL and target are both touched inside the same candle, the SL is assumed first.
* If a candle opens beyond the SL/target, the fill is at the open (gap).
* One position at a time. When several contracts signal in the same minute, the
  one with the biggest % jump is taken. Re-entry is allowed after an exit.

Data: https://huggingface.co/datasets/thetrademarkk/india-index-options-1m
      (run download_data.py first).
"""
import argparse
import glob
import os

import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(__file__), "data", "NIFTY")


def ema(series, n):
    return series.ewm(span=n, adjust=False).mean()


def load_expiry(path, ema_len):
    d = pd.read_parquet(path)
    d = d.sort_values(["strike", "option_type", "timestamp"]).reset_index(drop=True)
    g = d.groupby(["strike", "option_type"], sort=False)
    # EMA runs over the contract's whole history (like a continuous chart).
    d["ema"] = g["close"].transform(lambda s: ema(s, ema_len))
    d["prev_close"] = g["close"].shift(1)
    d["prev_low"] = g["low"].shift(1)
    d["prev_day"] = g["trading_day"].shift(1)
    return d


def run_day(day, trading_day, cfg):
    expiry = day["expiry"].iloc[0]
    day = day[day["trading_day"] == trading_day].copy()
    if day.empty:
        return []

    jump = day["close"] / day["prev_close"] if cfg.basis == "prev_close" else day["close"] / day["open"]
    day["jump"] = jump
    sig = day[
        (day["jump"] >= cfg.mult)
        & (day["prev_day"] == trading_day)  # previous candle must be same day (no overnight gap)
        & (day["close"] >= cfg.min_prem)
        & (day["close"] <= cfg.max_prem)
        & (day["timestamp"].dt.strftime("%H:%M") <= cfg.last_entry)
    ]

    bars = {k: v.set_index("timestamp") for k, v in day.groupby(["strike", "option_type"])}
    trades = []
    busy_until = None
    for ts, cands in sig.sort_values("timestamp").groupby("timestamp"):
        if busy_until is not None and ts <= busy_until:
            continue
        s = cands.sort_values("jump", ascending=False).iloc[0]
        key = (s["strike"], s["option_type"])
        entry, sl = s["close"], s["prev_low"]
        after = bars[key].loc[bars[key].index > ts]
        exit_px, reason, exit_ts = None, None, None
        for t, b in after.iterrows():
            if t.strftime("%H:%M") >= cfg.square_off:
                exit_px, reason, exit_ts = b["close"], "EOD", t
                break
            if b["low"] <= sl:
                exit_px, reason, exit_ts = min(sl, b["open"]), "STOPLOSS", t
                break
            if b["high"] >= cfg.target:
                exit_px, reason, exit_ts = max(cfg.target, b["open"]), "TARGET", t
                break
            if b["close"] < b["ema"]:
                exit_px, reason, exit_ts = b["close"], "EMA10", t
                break
        if exit_px is None:  # contract stopped trading; use last bar
            last = after.iloc[-1] if len(after) else bars[key].loc[ts]
            exit_px, reason, exit_ts = last["close"], "EOD", last.name
        pts = exit_px - entry
        lot_size = cfg.lot_size or (75 if expiry < "2026-01-01" else 65)  # NSE revised 75 -> 65 for 2026 contracts
        gross = pts * lot_size * cfg.lots
        trades.append(
            dict(
                date=trading_day,
                expiry=expiry,
                contract=f"NIFTY {int(s['strike'])} {s['option_type']}",
                entry_time=ts.strftime("%H:%M"),
                prev_close=round(s["prev_close"], 2),
                entry=round(entry, 2),
                stoploss=round(sl, 2),
                exit_time=exit_ts.strftime("%H:%M"),
                exit=round(exit_px, 2),
                reason=reason,
                points=round(pts, 2),
                gross_pnl=round(gross, 2),
                net_pnl=round(gross - cfg.charges, 2),
            )
        )
        busy_until = exit_ts
    return trades


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2026-05-01")
    p.add_argument("--end", default="2026-05-31")
    p.add_argument("--mult", type=float, default=2.0, help="candle close / reference >= mult")
    p.add_argument("--basis", choices=["prev_close", "open"], default="prev_close",
                   help="'doubles' measured vs previous candle close or same candle open")
    p.add_argument("--min-prem", type=float, default=0)
    p.add_argument("--max-prem", type=float, default=float("inf"))
    p.add_argument("--expiry-only", action="store_true", help="trade only on expiry days")
    p.add_argument("--target", type=float, default=200)
    p.add_argument("--ema", type=int, default=10)
    p.add_argument("--lot-size", type=int, default=0, help="0 = auto (75 before 2026, 65 from 2026)")
    p.add_argument("--lots", type=int, default=1)
    p.add_argument("--charges", type=float, default=60, help="approx brokerage+taxes per round trip (Rs)")
    p.add_argument("--last-entry", default="15:15")
    p.add_argument("--square-off", default="15:29")
    p.add_argument("--out", default="results/trades.csv")
    cfg = p.parse_args()

    # Expiry files needed: any expiry from the start date up to the first one after the end date.
    files = sorted(glob.glob(os.path.join(DATA_DIR, "*.parquet")))
    expiries = [os.path.basename(f)[:10] for f in files]
    files = [f for f, e in zip(files, expiries)
             if e >= cfg.start and (e <= cfg.end or e == min([x for x in expiries if x > cfg.end], default=None))]
    if not files:
        raise SystemExit("No data found - run download_data.py first.")

    data = {os.path.basename(f)[:10]: load_expiry(f, cfg.ema) for f in files}
    days = sorted({t for d in data.values() for t in d["trading_day"].unique() if cfg.start <= t <= cfg.end})
    trades, tested = [], []
    for day in days:
        expiry = min((e for e in data if e >= day), default=None)  # current-week contract
        if expiry is None or (cfg.expiry_only and day != expiry):
            continue
        tested.append(day)
        trades += run_day(data[expiry], day, cfg)

    t = pd.DataFrame(trades)
    pd.set_option("display.width", 250, "display.max_columns", 50)
    print(f"Days tested ({len(tested)}): {', '.join(tested)}\n")
    if t.empty:
        print("No trades.")
        return
    print(t.to_string(index=False))
    os.makedirs(os.path.dirname(cfg.out) or ".", exist_ok=True)
    t.to_csv(cfg.out, index=False)

    wins = t[t.net_pnl > 0]
    print("\n--- Summary ---")
    print(f"Trades        : {len(t)}")
    print(f"Winners       : {len(wins)}  ({len(wins) / len(t):.0%})")
    print(f"Exit reasons  : {t.reason.value_counts().to_dict()}")
    print(f"Total points  : {t.points.sum():.2f}")
    print(f"Gross P&L     : Rs {t.gross_pnl.sum():,.0f}  ({cfg.lots} lot/trade)")
    print(f"Net P&L       : Rs {t.net_pnl.sum():,.0f}  (after ~Rs {cfg.charges:.0f}/trade charges)")
    print(f"Avg win / loss: Rs {wins.net_pnl.mean() if len(wins) else 0:,.0f} / "
          f"Rs {t[t.net_pnl <= 0].net_pnl.mean() if (t.net_pnl <= 0).any() else 0:,.0f}")
    print("\nPer day:")
    print(t.groupby("date").agg(trades=("net_pnl", "size"), net_pnl=("net_pnl", "sum")).to_string())


if __name__ == "__main__":
    main()
