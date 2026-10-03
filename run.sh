#!/usr/bin/env sh
# Usage: ./run.sh <command> [csv] [options]   e.g. ./run.sh backtest data.csv --instrument nifty
cd "$(dirname "$0")" && exec python3 tools/intraday_lab.py "$@"
