"""
fetch_yahoo.py -- download the last ~60 days of 5-minute NSE candles (and
daily candles, for the official previous close) from Yahoo Finance into
CSVs that intraday_lab.py / orb_first_candle.py can read. Standard library only.

    python3 tools/fetch_yahoo.py                    # Nifty, Bank Nifty + 10 liquid stocks
    python3 tools/fetch_yahoo.py ^NSEI RELIANCE.NS  # specific symbols
    python3 tools/fetch_yahoo.py --nifty500         # all Nifty 500 stocks (5m + daily)

Output: data/<NAME>_5m.csv and data/daily/<NAME>_1d.csv

Yahoo limits 5-minute history to about 60 days and index symbols have no
volume (VWAP then becomes a time-weighted average). Data is unofficial and
for personal research only -- use NSE-authorised data for serious testing.
"""

import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
DEFAULT = ["^NSEI", "^NSEBANK", "RELIANCE.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS", "TCS.NS",
           "SBIN.NS", "AXISBANK.NS", "KOTAKBANK.NS", "LT.NS", "BHARTIARTL.NS"]
NAMES = {"^NSEI": "NIFTY", "^NSEBANK": "BANKNIFTY"}
NIFTY500_URL = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
UA = {"User-Agent": "Mozilla/5.0"}


def _get(url: str, tries: int = 4):
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return r.read()
        except Exception:
            if k == tries - 1:
                raise
            time.sleep(2 ** k)


def fetch(symbol: str, out_dir: str = "data", interval: str = "5m", rng: str = "60d") -> str:
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(symbol)}?interval={interval}&range={rng}")
    res = json.loads(_get(url))["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    daily = interval == "1d"
    rows = []
    for i, ts in enumerate(res.get("timestamp") or []):
        o, h, l, c, v = (q[k][i] for k in ("open", "high", "low", "close", "volume"))
        if None in (o, h, l, c):
            continue
        dt = datetime.fromtimestamp(ts, IST)
        rows.append([dt.strftime("%Y-%m-%d" if daily else "%Y-%m-%d %H:%M:%S"),
                     round(o, 2), round(h, 2), round(l, 2), round(c, 2), v or 0])
    if not rows:
        raise ValueError("no data")
    name = NAMES.get(symbol, symbol.replace(".NS", ""))
    folder = os.path.join(out_dir, "daily") if daily else out_dir
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{name}_{interval}.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date" if daily else "datetime", "open", "high", "low", "close", "volume"])
        w.writerows(rows)
    return f"{symbol:14s} {interval:3s} {len(rows):5d} bars  {rows[0][0][:10]} to {rows[-1][0][:10]}"


def nifty500_symbols() -> list:
    text = _get(NIFTY500_URL).decode("utf-8-sig")
    syms = [r["Symbol"].strip() + ".NS" for r in csv.DictReader(text.splitlines()) if r.get("Symbol")]
    os.makedirs("data", exist_ok=True)
    with open("data/nifty500_list.csv", "w") as f:
        f.write(text)
    return syms


def _job(args):
    sym, interval, rng = args
    try:
        return fetch(sym, interval=interval, rng=rng)
    except Exception as e:
        return f"{sym:14s} {interval:3s} FAILED: {e}"


if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv == ["--nifty500"]:
        syms = nifty500_symbols()
        jobs = [(s, "5m", "60d") for s in syms] + [(s, "1d", "6mo") for s in syms]
        print(f"Downloading {len(syms)} Nifty 500 stocks (5-minute + daily)...")
        failed = 0
        with ThreadPoolExecutor(max_workers=8) as ex:
            for n, msg in enumerate(ex.map(_job, jobs), 1):
                failed += "FAILED" in msg
                if "FAILED" in msg or n % 100 == 0:
                    print(f"  [{n}/{len(jobs)}] {msg}")
        print(f"Done. {len(jobs) - failed} files ok, {failed} failed. Files are in data/ and data/daily/")
    else:
        for s in argv or DEFAULT:
            print(_job((s, "5m", "60d")))
