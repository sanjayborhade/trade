"""Clean the raw 1-minute CSVs once and store them as fast Parquet files.

    python prepare_data.py            # builds data/cache (skipped if already up to date)
    python prepare_data.py --force    # rebuild, e.g. after changing data settings in config.yaml
"""

import argparse
import sys

from ibt.data.loader import DataFormatError
from ibt.data.store import prepare_cache
from ibt.runner import start


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--force", action="store_true", help="rebuild even if the cache looks up to date")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    cfg, run_dir, log = start(a.config, "prepare", a.verbose)
    try:
        man = prepare_cache(cfg, force=a.force)
    except DataFormatError as e:
        log.error(f"DATA ERROR: {e}")
        sys.exit(3)
    if man.get("file_errors"):
        log.warning(f"{len(man['file_errors'])} file(s) could not be read - see above / the log.")
    log.info("Next: python inspect_data.py   (review data quality)   then   python backtest.py --strategy orb")


if __name__ == "__main__":
    main()
