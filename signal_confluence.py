"""Fail-closed, read-only market-data quality gate for LuxAlgo webhooks.

This module does NOT create, reverse, or modify LuxAlgo signals and never sends
exchange orders. It only checks supplied LuxAlgo levels and public candle-data
quality before the existing safety-locked pipeline can continue.

Public data source: Gate.io USDT futures candlesticks (HTTP GET only).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE_URL = "https://api.gateio.ws/api/v4"
HTF_FOR = {"5m": "15m", "15m": "1h"}
INTERVAL_SECONDS = {"5m": 300, "15m": 900, "1h": 3600}


@dataclass(frozen=True)
class ConfluenceConfig:
    min_score: int = 5
    min_ltf_candles: int = 40
    min_htf_candles: int = 30
    max_entry_deviation_pct: Decimal = Decimal("1.5")
    max_range_pct: Decimal = Decimal("3.0")
    cooldown_seconds: int = 900

    @classmethod
    def from_env(cls) -> "ConfluenceConfig":
        """Read bounded, optional tuning values; invalid values fail startup."""
        def integer(name: str, default: int, low: int, high: int) -> int:
            raw = os.getenv(name)
            if raw is None or not raw.strip():
                return default
            value = int(raw)
            if not low <= value <= high:
                raise ValueError(f"{name} must be between {low} and {high}")
            return value

        def decimal(name: str, default: str, low: str, high: str) -> Decimal:
            raw = os.getenv(name, default)
            value = Decimal(raw)
            if not value.is_finite() or not Decimal(low) <= value <= Decimal(high):
                raise ValueError(f"{name} is outside its permitted range")
            return value

        return cls(
            min_score=integer("CONFLUENCE_MIN_SCORE", 5, 1, 6),
            min_ltf_candles=integer("CONFLUENCE_MIN_LTF_CANDLES", 40, 20, 120),
            min_htf_candles=integer("CONFLUENCE_MIN_HTF_CANDLES", 30, 20, 120),
            max_entry_deviation_pct=decimal(
                "CONFLUENCE_MAX_ENTRY_DEVIATION_PCT", "1.5", "0.1", "5.0"),
            max_range_pct=decimal("CONFLUENCE_MAX_RANGE_PCT", "3.0", "0.1", "10.0"),
            cooldown_seconds=integer("CONFLUENCE_COOLDOWN_SECONDS", 900, 0, 86400),
        )


def _decimal(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _candle(row):
    """Normalize Gate candle objects; reject malformed OHLC rows."""
    if not isinstance(row, dict):
        return None
    timestamp = row.get("t", row.get("timestamp"))
    try:
        ts = int(timestamp)
    except (TypeError, ValueError, OverflowError):
        return None
    o, h, low, close = (_decimal(row.get(k)) for k in ("o", "h", "l", "c"))
    if None in (o, h, low, close) or min(o, h, low, close) <= 0:
        return None
    if high_invalid(h, o, low, close):
        return None
    return {"t": ts, "o": o, "h": h, "l": low, "c": close}


def high_invalid(high, open_price, low, close):
    return high < max(open_price, low, close) or low > min(open_price, close)


def fetch_candles(contract: str, timeframe: str, limit: int = 120,
                  timeout: float = 3.5):
    """Fetch public Gate.io candles using GET only; no credentials or writes."""
    if timeframe not in INTERVAL_SECONDS or not isinstance(contract, str):
        raise ValueError("Unsupported contract or timeframe")
    if not contract.endswith("_USDT") or not contract.replace("_", "").isalnum():
        raise ValueError("Invalid Gate.io USDT contract")
    if isinstance(limit, bool) or not 20 <= int(limit) <= 200:
        raise ValueError("Candle limit must be between 20 and 200")
    query = urlencode({"contract": contract, "interval": timeframe, "limit": int(limit)})
    request = Request(
        f"{BASE_URL}/futures/usdt/candlesticks?{query}",
        headers={"Accept": "application/json", "User-Agent": "LuxAlgo-Confluence/1.0"},
        method="GET",
    )
    with urlopen(request, timeout=float(timeout)) as response:
        payload = json.load(response)
    if not isinstance(payload, list):
        raise RuntimeError("Gate.io returned an unexpected candle response")
    candles = [_candle(item) for item in payload]
    candles = [item for item in candles if item is not None]
    candles.sort(key=lambda item: item["t"])
    if len(candles) < 2:
        raise RuntimeError("Gate.io returned insufficient valid candles")
    return candles


def _normalize_candles(rows):
    if not isinstance(rows, (list, tuple)):
        return []
    result = [_candle(row) for row in rows]
    result = [row for row in result if row is not None]
    result.sort(key=lambda item: item["t"])
    return result


def evaluate(*, symbol: str, side: str, timeframe: str, entry, stop,
             take_profit, candles, htf_candles, config: ConfluenceConfig,
             now: datetime, last_entry_epoch=None):
    """Return PASS only when all configured data/level safety checks pass.

    Direction comes exclusively from the authenticated LuxAlgo webhook.
    This function never emits a BUY/SELL signal or changes the supplied levels.
    """
    if timeframe not in HTF_FOR or side not in ("LONG", "SHORT"):
        return _blocked("INVALID_INPUT", 0)
    if not isinstance(now, datetime) or now.tzinfo is None:
        return _blocked("INVALID_EVALUATION_TIME", 0)
    if not isinstance(config, ConfluenceConfig):
        return _blocked("INVALID_CONFIGURATION", 0)

    entry_d, stop_d, target_d = map(_decimal, (entry, stop, take_profit))
    level_ok = all(v is not None and v > 0 for v in (entry_d, stop_d, target_d))
    if level_ok:
        level_ok = ((stop_d < entry_d < target_d) if side == "LONG"
                    else (target_d < entry_d < stop_d))

    ltf = _normalize_candles(candles)
    htf = _normalize_candles(htf_candles)
    now_epoch = now.timestamp()
    ltf_seconds = INTERVAL_SECONDS[timeframe]
    htf_tf = HTF_FOR[timeframe]
    htf_seconds = INTERVAL_SECONDS[htf_tf]

    ltf_count_ok = len(ltf) >= config.min_ltf_candles
    htf_count_ok = len(htf) >= config.min_htf_candles
    ltf_fresh = bool(ltf) and 0 <= now_epoch - ltf[-1]["t"] <= 2 * ltf_seconds + 120
    htf_fresh = bool(htf) and 0 <= now_epoch - htf[-1]["t"] <= 2 * htf_seconds + 120

    entry_near_market = False
    range_ok = False
    deviation_pct = None
    range_pct = None
    if ltf and entry_d is not None and entry_d > 0:
        latest = ltf[-1]["c"]
        deviation_pct = abs(latest - entry_d) / entry_d * Decimal("100")
        entry_near_market = deviation_pct <= config.max_entry_deviation_pct
        recent = ltf[-20:]
        avg_range = sum((c["h"] - c["l"] for c in recent), Decimal("0")) / Decimal(len(recent))
        range_pct = avg_range / latest * Decimal("100") if latest > 0 else None
        range_ok = range_pct is not None and range_pct <= config.max_range_pct

    cooldown_ok = True
    if last_entry_epoch is not None and config.cooldown_seconds:
        try:
            last = float(last_entry_epoch)
            cooldown_ok = (now_epoch - last) >= config.cooldown_seconds
            if now_epoch < last:
                cooldown_ok = False
        except (TypeError, ValueError, OverflowError):
            cooldown_ok = False

    checks = {
        "LUXALGO_LEVEL_GEOMETRY": level_ok,
        "LTF_CANDLE_COUNT": ltf_count_ok,
        "HTF_CANDLE_COUNT": htf_count_ok,
        "LTF_DATA_FRESHNESS": ltf_fresh,
        "HTF_DATA_FRESHNESS": htf_fresh,
        "ENTRY_PRICE_PROXIMITY_AND_RANGE": entry_near_market and range_ok,
        "ENTRY_COOLDOWN": cooldown_ok,
    }
    score = sum(1 for passed in checks.values() if passed)
    blockers = [name for name, passed in checks.items() if not passed]
    # min_score is retained as a configurable compatibility field, but every
    # mandatory check must pass; score alone can never override a failed check.
    passed = not blockers and score >= config.min_score
    return {
        "decision": "PASS" if passed else "BLOCK",
        "score": score,
        "min_score": config.min_score,
        "blockers": blockers,
        "components": {name: ("PASS" if ok else "BLOCK") for name, ok in checks.items()},
        "metrics": {
            "symbol": symbol,
            "timeframe": timeframe,
            "higher_timeframe": htf_tf,
            "ltf_candles": len(ltf),
            "htf_candles": len(htf),
            "entry_deviation_pct": str(deviation_pct) if deviation_pct is not None else None,
            "average_recent_range_pct": str(range_pct) if range_pct is not None else None,
        },
        "summary": "READ_ONLY_MARKET_QUALITY_PASS" if passed else "READ_ONLY_MARKET_QUALITY_BLOCK",
    }


def _blocked(reason, min_score):
    return {"decision": "BLOCK", "score": 0, "min_score": min_score,
            "blockers": [reason], "components": {}, "metrics": {}, "summary": reason}
