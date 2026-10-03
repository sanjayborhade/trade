"""Download NIFTY weekly option 1-min data for the given expiries (default: expiries covering May 2026)."""
import os
import sys
import urllib.request

BASE = "https://huggingface.co/datasets/thetrademarkk/india-index-options-1m/resolve/main/options/NIFTY/"
EXPIRIES = sys.argv[1:] or ["2026-05-05", "2026-05-12", "2026-05-19", "2026-05-26", "2026-06-02"]
out = os.path.join(os.path.dirname(__file__), "data", "NIFTY")
os.makedirs(out, exist_ok=True)
for e in EXPIRIES:
    dst = os.path.join(out, f"{e}.parquet")
    if not os.path.exists(dst):
        print("downloading", e)
        urllib.request.urlretrieve(BASE + f"{e}.parquet", dst)
