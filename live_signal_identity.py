"""Deterministic TradingView/LuxAlgo alert identity (pure, no API writes)."""
from hashlib import sha256
from datetime import datetime, timezone
import re

BAR_RE=re.compile(r"[A-Za-z0-9_.:-]{8,120}\Z")

def validated_bar_id(value):
    if not isinstance(value,str): return None
    value=value.strip()
    return value if BAR_RE.fullmatch(value) else None

def stable_signal_identity(symbol,side,timeframe,received_at,bar_id=None):
    """Same bar+direction+market => same event ID across retries/deploys.

    Legacy webhook without bar_id is still accepted as SIGNAL_ONLY. Its
    receive-time bucket is intentionally never allowed to authorize orders.
    """
    if not isinstance(received_at,datetime) or received_at.tzinfo is None:
        raise ValueError("Timezone-aware receive timestamp required")
    if not re.fullmatch(r"[A-Z0-9]{3,25}USDT",symbol or ""):
        raise ValueError("Invalid USDT symbol")
    if side not in ("LONG","SHORT") or timeframe not in ("5m","15m"):
        raise ValueError("Invalid side or timeframe")
    stable=validated_bar_id(bar_id)
    if stable:
        fingerprint=f"{symbol}|{side}|{timeframe}|TV_BAR:{stable}"
    else:
        bucket=int(received_at.timestamp())//(300 if timeframe=="5m" else 900)
        fingerprint=f"{symbol}|{side}|{timeframe}|LEGACY_RECEIVED:{bucket}"
    return sha256(fingerprint.encode("utf8")).hexdigest()[:20], stable
