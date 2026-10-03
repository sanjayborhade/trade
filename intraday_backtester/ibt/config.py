"""Configuration loading, defaults and validation."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from datetime import date, time
from typing import Any, Dict, List

import yaml


class ConfigError(Exception):
    """Raised when config.yaml is missing, malformed or contains invalid values."""


DEFAULTS: Dict[str, Any] = {
    "data": {
        "DATA_FOLDER": "data/raw", "CACHE_FOLDER": "data/cache", "file_pattern": "*.csv",
        "symbol_from": "auto", "strip_prefixes": ["NSE_", "NSE:", "NSE-"],
        "strip_suffixes": ["_1MIN", "_1M", "-1MIN", "_MINUTE", "-EQ", "_EQ", ".NS", "_NSE", "-NSE"],
        "columns": {
            "datetime": ["datetime", "date_time", "timestamp", "time_stamp", "datetime_ist", "dt"],
            "date": ["date", "trade_date", "tradedate", "day"],
            "time": ["time", "trade_time", "tradetime", "minute"],
            "open": ["open", "o", "open_price", "openprice"],
            "high": ["high", "h", "high_price", "highprice"],
            "low": ["low", "l", "low_price", "lowprice"],
            "close": ["close", "c", "close_price", "closeprice", "ltp", "last"],
            "volume": ["volume", "vol", "v", "qty", "quantity", "traded_qty", "tradedqty", "volume_traded"],
            "symbol": ["symbol", "ticker", "tradingsymbol", "trading_symbol", "scrip", "stock", "name",
                       "instrument"],
        },
        "datetime_format": None, "dayfirst": True, "timezone": "Asia/Kolkata",
        "timestamp_is_bar_end": "auto", "session_start": "09:15", "session_end": "15:30",
        "min_bars_per_day": 300, "exclude_short_days": True, "duplicate_policy": "last",
        "ohlc_policy": "repair", "max_minute_jump_pct": 15, "corporate_action_gap_pct": 25,
        "ca_exclusion_days": 20, "big_file_mb": 500, "chunksize": 2_000_000, "workers": 0,
    },
    "benchmarks": {"NIFTY50_FILE": None, "NIFTY500_FILE": None, "build_equal_weight": True,
                   "regime_benchmark": "auto"},
    "universe": {"MEMBERSHIP_FILE": None, "mode": "all", "min_price": 50, "max_price": 100000,
                 "min_avg_daily_volume": 0, "min_avg_traded_value": 50_000_000,
                 "liquidity_lookback_days": 20, "min_trading_days": 60},
    "portfolio": {"INITIAL_CAPITAL": 1_000_000, "RISK_PER_TRADE": 0.005, "MAX_OPEN_POSITIONS": 5,
                  "MAX_DAILY_LOSS": 0.015, "daily_loss_mode": "realized_plus_open_risk",
                  "MAX_DAILY_TRADES": 10, "MAX_PORTFOLIO_EXPOSURE": 1.0, "MAX_POSITION_VALUE_PCT": 0.25,
                  "MAX_POSITION_VALUE_ABS": None, "MAX_PARTICIPATION": 0.05, "sizing_equity": "current",
                  "allow_short": True, "ranking": "score", "random_seed": 42},
    "costs": {"BROKERAGE_PCT": 0.0003, "BROKERAGE_MAX_PER_ORDER": 20, "STT": 0.00025,
              "EXCHANGE_CHARGES": 0.0000307, "SEBI_CHARGES": 0.000001, "STAMP_DUTY": 0.00003, "GST": 0.18,
              "IPFT": 0.0, "SLIPPAGE": 0.0005, "SPREAD": 0.0004},
    "execution": {"same_bar_stop_target": "stop_first", "trade_through_ticks": 1, "tick_size": 0.05,
                  "square_off_time": "15:15", "order_validity_minutes": 30},
    "regimes": {"trend_fast_days": 20, "trend_slow_days": 50, "vol_lookback_days": 20, "vol_rank_days": 250,
                "gap_threshold_pct": 1.0, "high_volume_ratio": 1.5, "low_volume_ratio": 0.7,
                "expiry_rules": [{"from": "2000-01-01", "weekday": 3}, {"from": "2025-09-01", "weekday": 1}]},
    "periods": {"mode": "fractions", "fractions": [0.6, 0.2, 0.2],
                "dates": {"development": ["2019-01-01", "2023-12-31"], "validation": ["2024-01-01", "2025-06-30"],
                          "test": ["2025-07-01", "2026-12-31"]}},
    "walk_forward": {"train_months": 24, "test_months": 6, "step_months": 6},
    "optimization": {"min_trades": 100, "batch_size": 8, "top_n_validate": 5},
    "analysis": {"bootstrap_samples": 2000, "top_symbols_concentration": 5},
    "output": {"OUTPUT_FOLDER": "results", "save_parquet": True, "charts": True},
    "strategies": {},
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "columns":
            out[k] = _deep_merge(out[k], v)
        elif k == "columns" and isinstance(v, dict):
            cols = copy.deepcopy(out.get(k, {}))
            cols.update(v)
            out[k] = cols
        else:
            out[k] = v
    return out


def parse_hhmm(value: Any, field: str = "time") -> int:
    """'09:15' -> minutes since midnight (555)."""
    try:
        t = time.fromisoformat(str(value).strip())
    except ValueError as e:
        raise ConfigError(f"{field}: '{value}' is not a valid HH:MM time") from e
    return t.hour * 60 + t.minute


class _UniqueKeyLoader(yaml.SafeLoader):
    """Rejects duplicate keys: plain YAML would silently keep only the LAST 'data:' section."""

    def construct_mapping(self, node, deep=False):
        seen = {}
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise ConfigError(f"config.yaml defines '{key}' twice (lines {seen[key] + 1} and "
                                  f"{key_node.start_mark.line + 1}). Only the second one would be used - "
                                  f"delete or merge one of them.")
            seen[key] = key_node.start_mark.line
        return super().construct_mapping(node, deep)


def load_config(path: str = "config.yaml") -> Dict[str, Any]:
    if not os.path.exists(path):
        raise ConfigError(f"Config file not found: {os.path.abspath(path)}")
    try:
        with open(path, encoding="utf-8") as f:
            user = yaml.load(f, Loader=_UniqueKeyLoader) or {}
    except yaml.YAMLError as e:
        hint = ""
        if "escape" in str(e):
            hint = ("\nHINT: Windows paths inside double quotes need forward slashes "
                    "(\"E:/STUDY/data\") or single quotes ('E:\\STUDY\\data').")
        raise ConfigError(f"config.yaml is not valid YAML: {e}{hint}") from e
    if not isinstance(user, dict):
        raise ConfigError("config.yaml must contain sections such as data:, portfolio:, costs:")
    cfg = _deep_merge(DEFAULTS, user)
    base = os.path.dirname(os.path.abspath(path))
    cfg["_base_dir"] = base

    def resolve(p):
        if p in (None, "", "null"):
            return None
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p))

    d = cfg["data"]
    d["DATA_FOLDER"] = resolve(d["DATA_FOLDER"])
    d["CACHE_FOLDER"] = resolve(d["CACHE_FOLDER"])
    for k in ("NIFTY50_FILE", "NIFTY500_FILE"):
        cfg["benchmarks"][k] = resolve(cfg["benchmarks"][k])
    cfg["universe"]["MEMBERSHIP_FILE"] = resolve(cfg["universe"]["MEMBERSHIP_FILE"])
    cfg["output"]["OUTPUT_FOLDER"] = resolve(cfg["output"]["OUTPUT_FOLDER"])
    validate(cfg)
    return cfg


def validate(cfg: Dict[str, Any]) -> None:
    problems: List[str] = []

    def num(section, key, lo=None, hi=None, allow_none=False):
        v = cfg[section].get(key)
        if v is None and allow_none:
            return
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            problems.append(f"{section}.{key} must be a number (got {v!r})")
            return
        if lo is not None and v < lo:
            problems.append(f"{section}.{key} must be >= {lo} (got {v})")
        if hi is not None and v > hi:
            problems.append(f"{section}.{key} must be <= {hi} (got {v})")

    num("portfolio", "INITIAL_CAPITAL", 1)
    num("portfolio", "RISK_PER_TRADE", 0.00001, 0.1)
    num("portfolio", "MAX_OPEN_POSITIONS", 1)
    num("portfolio", "MAX_DAILY_LOSS", 0.0001, 1)
    num("portfolio", "MAX_DAILY_TRADES", 1)
    num("portfolio", "MAX_PORTFOLIO_EXPOSURE", 0.01, 10)
    num("portfolio", "MAX_POSITION_VALUE_PCT", 0.001, 10)
    num("portfolio", "MAX_POSITION_VALUE_ABS", 1, allow_none=True)
    num("portfolio", "MAX_PARTICIPATION", 0, 1)
    for k in ("BROKERAGE_PCT", "STT", "EXCHANGE_CHARGES", "SEBI_CHARGES", "STAMP_DUTY", "GST", "IPFT",
              "SLIPPAGE", "SPREAD"):
        num("costs", k, 0, 1)
    num("costs", "BROKERAGE_MAX_PER_ORDER", 0)
    num("execution", "tick_size", 0.0001)
    num("execution", "trade_through_ticks", 0)
    num("data", "min_bars_per_day", 1, 375)
    if cfg["portfolio"]["sizing_equity"] not in ("current", "initial"):
        problems.append("portfolio.sizing_equity must be 'current' or 'initial'")
    if cfg["portfolio"]["ranking"] not in ("score", "random", "symbol"):
        problems.append("portfolio.ranking must be score | random | symbol")
    if cfg["portfolio"]["daily_loss_mode"] not in ("realized", "realized_plus_open_risk"):
        problems.append("portfolio.daily_loss_mode must be realized | realized_plus_open_risk")
    if cfg["execution"]["same_bar_stop_target"] not in ("stop_first", "target_first"):
        problems.append("execution.same_bar_stop_target must be stop_first | target_first")
    if cfg["data"]["duplicate_policy"] not in ("last", "first"):
        problems.append("data.duplicate_policy must be last | first")
    if cfg["data"]["ohlc_policy"] not in ("repair", "drop"):
        problems.append("data.ohlc_policy must be repair | drop")
    if str(cfg["data"]["timestamp_is_bar_end"]).lower() not in ("auto", "true", "false"):
        problems.append("data.timestamp_is_bar_end must be auto | true | false")
    if cfg["data"]["symbol_from"] not in ("auto", "column", "filename"):
        problems.append("data.symbol_from must be auto | column | filename")
    if cfg["universe"]["mode"] not in ("all", "liquid"):
        problems.append("universe.mode must be all | liquid")
    for sec, key in (("data", "session_start"), ("data", "session_end"), ("execution", "square_off_time")):
        try:
            parse_hhmm(cfg[sec][key], f"{sec}.{key}")
        except ConfigError as e:
            problems.append(str(e))
    p = cfg["periods"]
    if p["mode"] == "fractions":
        fr = p["fractions"]
        if not (isinstance(fr, list) and len(fr) == 3 and all(isinstance(x, (int, float)) and x > 0 for x in fr)
                and abs(sum(fr) - 1) < 1e-6):
            problems.append("periods.fractions must be three positive numbers that sum to 1, e.g. [0.6, 0.2, 0.2]")
    elif p["mode"] == "dates":
        for name in ("development", "validation", "test"):
            try:
                a, b = p["dates"][name]
                if date.fromisoformat(str(a)) > date.fromisoformat(str(b)):
                    problems.append(f"periods.dates.{name}: start is after end")
            except (KeyError, ValueError, TypeError):
                problems.append(f"periods.dates.{name} must be [\"YYYY-MM-DD\", \"YYYY-MM-DD\"]")
    else:
        problems.append("periods.mode must be fractions | dates")
    if not isinstance(cfg.get("strategies"), dict):
        problems.append("strategies: must be a mapping of strategy name -> settings")
    if problems:
        raise ConfigError("Invalid config.yaml:\n  - " + "\n  - ".join(problems))


def data_config_hash(cfg: Dict[str, Any]) -> str:
    """Hash of the settings that change how raw CSVs are cleaned (cache invalidation)."""
    keys = ["DATA_FOLDER", "file_pattern", "symbol_from", "strip_prefixes", "strip_suffixes", "columns",
            "datetime_format", "dayfirst", "timezone", "timestamp_is_bar_end", "session_start", "session_end",
            "min_bars_per_day", "exclude_short_days", "duplicate_policy", "ohlc_policy",
            "corporate_action_gap_pct"]
    sub = {k: cfg["data"].get(k) for k in keys}
    sub["bench"] = {k: cfg["benchmarks"].get(k) for k in ("NIFTY50_FILE", "NIFTY500_FILE", "build_equal_weight")}
    return hashlib.sha1(json.dumps(sub, sort_keys=True, default=str).encode()).hexdigest()[:12]


def n_workers(cfg: Dict[str, Any]) -> int:
    w = int(cfg["data"].get("workers") or 0)
    if w <= 0:
        w = max(1, (os.cpu_count() or 2) - 1)
    return w
