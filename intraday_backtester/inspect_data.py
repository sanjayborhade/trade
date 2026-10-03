"""Inspect your 1-minute data BEFORE trusting any backtest.

    python inspect_data.py               # full inspection (also builds the cache used by backtest.py)
    python inspect_data.py --sample 20   # quick look at the first 20 files only (nothing cached)

Reports: files, stocks, date range, candles, detected columns and how timestamps were
parsed, duplicates, missing candles, invalid OHLC, missing volume, candles/day,
stocks with insufficient or suspicious data. Writes inspect_report.txt + CSV.
"""

import argparse
import copy
import os
import sys

import pandas as pd

from ibt.data.loader import (DataFormatError, detect_columns, find_files, parse_timestamps, read_header)
from ibt.data.store import prepare_cache
from ibt.runner import start


def header_signatures(files, dcfg, log, max_examples=3):
    sigs = {}
    for p in files:
        try:
            header, delim, enc = read_header(p)
            key = (tuple(header), delim)
            sigs.setdefault(key, []).append(p)
        except DataFormatError as e:
            log.error(f"  {e}")
    lines = []
    for (header, delim), paths in sorted(sigs.items(), key=lambda x: -len(x[1])):
        lines.append(f"\nFile layout used by {len(paths)} file(s), e.g. {os.path.basename(paths[0])}")
        lines.append(f"  delimiter '{delim}'  columns: {list(header)}")
        try:
            cols = detect_columns(list(header), dcfg["columns"], paths[0])
            lines.append(f"  detected: {cols}")
            raw = pd.read_csv(paths[0], sep=delim, nrows=400, usecols=sorted(set(cols.values())))
            ts = parse_timestamps(raw, cols, dcfg)
            src = cols.get("datetime") or (f"{cols['date']} + {cols['time']}" if "date" in cols and "time" in cols
                                           else cols.get("date") or cols.get("time"))
            lines.append(f"  timestamp source: {src}  (dayfirst={dcfg['dayfirst']}, format={dcfg['datetime_format']})")
            for k in range(min(max_examples, len(raw))):
                rv = (raw[cols['datetime']].iloc[k] if "datetime" in cols else
                      " ".join(str(raw[c].iloc[k]) for c in (cols.get('date'), cols.get('time')) if c))
                lines.append(f"    raw '{rv}'  ->  parsed {ts.iloc[k]}")
            if ts.isna().mean() > 0.01:
                lines.append(f"  WARNING: {ts.isna().mean():.0%} of the first 400 timestamps could not be parsed. "
                             f"Set data.datetime_format in config.yaml.")
            first_min = ts.dropna().dt.strftime("%H:%M").min() if ts.notna().any() else "-"
            lines.append(f"  earliest time in sample: {first_min}  (09:16 + 15:30 candles => end-of-minute stamps; "
                         f"handled by timestamp_is_bar_end)")
        except DataFormatError as e:
            lines.append(f"  PROBLEM: {e}")
        except Exception as e:  # noqa: BLE001
            lines.append(f"  PROBLEM reading sample: {type(e).__name__}: {e}")
    return lines


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--sample", type=int, help="inspect only the first N files (quick, nothing cached)")
    ap.add_argument("--force", action="store_true", help="re-read all files even if the cache is up to date")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    cfg, run_dir, log = start(a.config, "inspect", a.verbose)
    dcfg = cfg["data"]
    try:
        files = find_files(dcfg["DATA_FOLDER"], dcfg["file_pattern"])
    except DataFormatError as e:
        log.error(str(e))
        sys.exit(3)
    if not files:
        log.error(f"No files matching '{dcfg['file_pattern']}' in {dcfg['DATA_FOLDER']}")
        sys.exit(3)
    log.info(f"Found {len(files)} file(s) in {dcfg['DATA_FOLDER']} "
             f"({sum(os.path.getsize(p) for p in files) / 1e6:,.0f} MB)")
    rep = ["DATA INSPECTION REPORT", "=" * 70, f"Folder: {dcfg['DATA_FOLDER']}", f"Files: {len(files)}"]
    rep += header_signatures(files, dcfg, log)

    if a.sample:
        cfg = copy.deepcopy(cfg)
        sample_dir = os.path.join(run_dir, "sample_raw")
        os.makedirs(sample_dir)
        for p in files[: a.sample]:
            dst = os.path.join(sample_dir, os.path.relpath(p, dcfg["DATA_FOLDER"]).replace(os.sep, "__"))
            with open(p, "rb") as fi, open(dst, "wb") as fo:
                fo.write(fi.read())
        cfg["data"]["DATA_FOLDER"] = sample_dir
        cfg["data"]["CACHE_FOLDER"] = os.path.join(run_dir, "sample_cache")
        log.info(f"Sample mode: inspecting the first {a.sample} file(s) only.")
    try:
        man = prepare_cache(cfg, force=a.force)
    except DataFormatError as e:
        log.error(f"DATA ERROR: {e}")
        sys.exit(3)
    q = pd.read_csv(os.path.join(cfg["data"]["CACHE_FOLDER"], "quality_report.csv"))
    q.to_csv(os.path.join(run_dir, "data_quality_by_symbol.csv"), index=False)
    good = q[q["rows_clean"] > 0]
    minimum = int(cfg["universe"]["min_trading_days"])
    rep += ["", "SUMMARY", "-" * 70,
            f"Stocks with usable data      : {len(good)} of {len(q)}",
            f"Date range                   : {good['first_date'].min()} to {good['last_date'].max()}",
            f"Trading days (calendar)      : {len(man['calendar'])}",
            f"Raw rows / clean candles     : {int(q['rows_raw'].sum()):,} / {int(q['rows_clean'].sum()):,}",
            f"Average candles per day      : {good['avg_bars_per_day'].mean():.1f}  (375 = complete NSE day)",
            f"Missing minutes (average)    : {good['missing_minutes_pct'].mean():.2f}%",
            f"Duplicate timestamps removed : {int(q['duplicates'].sum()):,}",
            f"Unparseable timestamps       : {int(q['invalid_timestamps'].sum()):,}",
            f"Rows with bad/zero prices    : {int(q['nonpositive_or_missing_price'].sum()):,}",
            f"Invalid OHLC candles         : {int(q['ohlc_invalid'].sum()):,} ({dcfg['ohlc_policy']})",
            f"Candles outside 09:15-15:30  : {int(q['outside_session'].sum()):,}",
            f"Missing volume values        : {int(q['missing_volume'].sum()):,}",
            f"Short days excluded          : {int(q['short_days_excluded'].sum()):,} "
            f"(< {dcfg['min_bars_per_day']} candles)",
            f"End-of-minute stamps shifted : {int(q['bar_end_shift_applied'].sum())} stock(s)",
            f"Days falling on weekends     : {int(q.get('weekend_days', pd.Series([0])).sum()):,}  (should be ~0; if not, the "
            f"day/month order is probably wrong)"]
    insuff = good[good["days"] < minimum]
    rep += ["", f"STOCKS WITH INSUFFICIENT HISTORY (< {minimum} days): {len(insuff)}"]
    rep += [f"  {r.symbol}: {r.days} days" for r in insuff.itertuples()][:50]
    sus = q[(q["problems"].fillna("") != "")]
    rep += ["", f"STOCKS WITH DATA PROBLEMS: {len(sus)} (full list: data_quality_by_symbol.csv)"]
    for r in sus.head(60).itertuples():
        rep.append(f"  {r.symbol}: {r.problems}")
    ca = q[q["ca_gap_dates"].fillna("") != ""]
    if len(ca):
        rep += ["", f"POSSIBLE SPLITS/BONUSES (unadjusted prices) in {len(ca)} stock(s) - trading is paused for "
                    f"{dcfg['ca_exclusion_days']} days after each:"]
        rep += [f"  {r.symbol}: {r.ca_gap_dates}" for r in ca.head(40).itertuples()]
    if man.get("file_errors"):
        rep += ["", "FILES THAT COULD NOT BE READ:"] + [f"  {e}" for e in man["file_errors"][:50]]
    rep += ["", "If timestamps, columns or the date range look wrong, fix data: settings in config.yaml and run",
            "  python prepare_data.py --force", "before backtesting."]
    txt = "\n".join(str(x) for x in rep)
    with open(os.path.join(run_dir, "inspect_report.txt"), "w", encoding="utf-8") as f:
        f.write(txt)
    print("\n" + txt)
    log.info(f"\nSaved: {run_dir}/inspect_report.txt and data_quality_by_symbol.csv")


if __name__ == "__main__":
    main()
