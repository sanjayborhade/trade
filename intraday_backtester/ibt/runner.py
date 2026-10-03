"""Shared helpers for the command-line scripts."""

from __future__ import annotations

import os
import sys
from datetime import date, datetime
from typing import Dict, Optional

from .config import ConfigError, load_config
from .data.loader import DataFormatError
from .data.store import CacheError, cache_is_fresh, load_manifest, prepare_cache
from .logutil import get_logger, setup_logging


def new_run_dir(cfg, label: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    d = os.path.join(cfg["output"]["OUTPUT_FOLDER"], f"{stamp}_{label}")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(cfg["output"]["OUTPUT_FOLDER"], "LATEST.txt"), "w") as f:
        f.write(d + "\n")
    return d


def start(config_path: str, label: str, verbose: bool = False):
    """Load config, create the run folder and logging. Exits with a clear message on config errors."""
    try:
        cfg = load_config(config_path)
    except ConfigError as e:
        print(f"\nCONFIG ERROR\n{e}\n")
        sys.exit(2)
    run_dir = new_run_dir(cfg, label)
    log = setup_logging(run_dir, label, verbose)
    log.info(f"Config: {os.path.abspath(config_path)}")
    log.info(f"Output: {run_dir}")
    return cfg, run_dir, log


def ensure_cache(cfg, auto_prepare: bool = True) -> Dict:
    log = get_logger()
    try:
        if not cache_is_fresh(cfg):
            if not auto_prepare:
                raise CacheError("Data cache missing. Run: python prepare_data.py")
            log.info("No prepared data found - preparing it now (one-time step)...")
            return prepare_cache(cfg)
        return load_manifest(cfg)
    except (DataFormatError, CacheError) as e:
        log.error(f"DATA ERROR: {e}")
        sys.exit(3)


def parse_date(s: Optional[str]) -> Optional[date]:
    return date.fromisoformat(s) if s else None


def parse_params(s: Optional[str]) -> Dict:
    """'or_minutes=30,target_r=1.5,entry_type=close' -> dict with numbers converted."""
    out: Dict = {}
    if not s:
        return out
    for part in s.split(","):
        if not part.strip():
            continue
        if "=" not in part:
            raise ValueError(f"--params expects key=value pairs, got '{part}'")
        k, v = part.split("=", 1)
        out[k.strip()] = convert(v.strip())
    return out


def convert(v: str):
    lv = v.lower()
    if lv in ("true", "false"):
        return lv == "true"
    for t in (int, float):
        try:
            return t(v)
        except ValueError:
            pass
    return v
