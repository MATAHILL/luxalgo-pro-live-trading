"""Deterministic TradingView/LuxAlgo alert identity (pure, no API writes)."""
from hashlib import sha256
from datetime import datetime, timezone
import re

BAR_RE=re.compile(r"[A-Za-z0-9_.:-]{8,120}\Z")

def validated_bar_id(value):
    if not isinstance(value,str): return None
    value=value.strip()
    return value if BAR_RE.fullmatch(value) else None

def luxalgo_bar_identity(timeframe, received_at, bartime):
    """Verify an exchange-time LuxAlgo `{time}` bar timestamp (Unix milliseconds).

    The result is a deterministic same-candle identifier, not a trade signal.
    Reject old/future/stale values; never grant order permission on its own.
    """
    if timeframe not in ("5m","15m"):
        return None
    if not isinstance(received_at,datetime) or received_at.tzinfo is None:
        return None
    if isinstance(bartime,bool) or not isinstance(bartime,(int,str)):
        return None
    raw=str(bartime).strip()
    if not re.fullmatch(r"[1-9][0-9]{12}",raw):
        return None
    ms=int(raw)
    now_ms=int(received_at.timestamp()*1000)
    period_ms=(300000 if timeframe=="5m" else 900000)
    # A completed bar may report its opening time; allow one extra interval
    # of delivery latency. A source timestamp in the future must fail closed.
    if not -120000<=now_ms-ms<=2*period_ms+120000:
        return None
    return f"TVBAR_{timeframe}_{ms//period_ms:011d}"


def normalize_luxalgo_symbol(value):
    """Allow listed TradingView exchange prefixes, never arbitrary ticker sources.

    Gate.io Futures and Binance perpetual symbols may appear as
    GATEIO:XRPUSDT.P or BINANCE:XRPUSDT.P. Only the normalized USDT
    contract name reaches the Gate.io contract metadata/readback path.
    """
    if not isinstance(value, str):
        return None
    raw=value.strip().upper()
    if ":" in raw:
        source, raw = raw.split(":", 1)
        if source not in ("GATEIO", "BINANCE"):
            return None
    if raw.endswith(".P"):
        raw=raw[:-2]
    return raw if re.fullmatch(r"[A-Z0-9]{3,25}USDT", raw) else None


def normalize_luxalgo_side(value):
    """Strict LuxAlgo confirmation labels only; never trade TP/SL/exit alerts."""
    if not isinstance(value,str):
        return None
    key=" ".join(value.strip().upper().replace("_"," ").split())
    return {
        "BUY":"LONG","LONG":"LONG","BULLISH CONFIRMATION":"LONG",
        "BULLISH CONFIRMATION SIGNAL":"LONG",
        "SELL":"SHORT","SHORT":"SHORT","BEARISH CONFIRMATION":"SHORT",
        "BEARISH CONFIRMATION SIGNAL":"SHORT",
    }.get(key)


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
