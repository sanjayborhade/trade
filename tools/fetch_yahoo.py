"""
fetch_yahoo.py -- download the last ~60 days of 5-minute NSE candles from
Yahoo Finance into CSVs that intraday_lab.py can read. Standard library only.

    python3 tools/fetch_yahoo.py                    # Nifty, Bank Nifty + 10 liquid stocks
    python3 tools/fetch_yahoo.py ^NSEI RELIANCE.NS  # specific symbols

Yahoo limits 5-minute history to about 60 days and index symbols have no
volume (VWAP then becomes a time-weighted average). Data is unofficial and
for personal research only -- use NSE-authorised data for serious testing.
"""

import csv
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
DEFAULT = ["^NSEI", "^NSEBANK", "RELIANCE.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS", "TCS.NS",
           "SBIN.NS", "AXISBANK.NS", "KOTAKBANK.NS", "LT.NS", "BHARTIARTL.NS"]
NAMES = {"^NSEI": "NIFTY", "^NSEBANK": "BANKNIFTY"}


def fetch(symbol: str, out_dir: str = "data") -> str:
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(symbol)}?interval=5m&range=60d")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        res = json.load(r)["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    rows = []
    for i, ts in enumerate(res["timestamp"]):
        o, h, l, c, v = (q[k][i] for k in ("open", "high", "low", "close", "volume"))
        if None in (o, h, l, c):
            continue
        rows.append([datetime.fromtimestamp(ts, IST).strftime("%Y-%m-%d %H:%M:%S"),
                     round(o, 2), round(h, 2), round(l, 2), round(c, 2), v or 0])
    os.makedirs(out_dir, exist_ok=True)
    name = NAMES.get(symbol, symbol.replace(".NS", ""))
    path = os.path.join(out_dir, f"{name}_5m.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["datetime", "open", "high", "low", "close", "volume"])
        w.writerows(rows)
    print(f"{symbol:14s} {len(rows):5d} bars  {rows[0][0][:10]} to {rows[-1][0][:10]}  -> {path}")
    return path


if __name__ == "__main__":
    for s in sys.argv[1:] or DEFAULT:
        try:
            fetch(s)
        except Exception as e:  # keep going on one bad symbol
            print(f"{s:14s} FAILED: {e}")
