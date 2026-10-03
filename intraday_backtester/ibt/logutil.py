"""Logging to console + file."""

from __future__ import annotations

import logging
import os
import sys
import warnings


def setup_logging(log_dir: str, name: str, verbose: bool = False) -> logging.Logger:
    os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger("ibt")
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    fh = logging.FileHandler(os.path.join(log_dir, f"{name}.log"), mode="w", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"))
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(fh)
    logger.addHandler(ch)
    logging.captureWarnings(True)
    warnings.simplefilter("default")
    wl = logging.getLogger("py.warnings")
    wl.handlers.clear()
    wl.addHandler(fh)
    wl.propagate = False
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger("ibt")
