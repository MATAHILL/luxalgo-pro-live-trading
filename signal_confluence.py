"""LuxAlgo signal confluence gate (pure logic + read-only candle fetch).

A PASS is permission to CONTINUE through the existing safety-locked execution
path. It is never an order, never an authorization, and never a guarantee of
profit. Every failure mode returns BLOCK (fail-closed).

Design rules
------------
* No exchange write of any kind lives in this module.
* Missing, stale or malformed market data BLOCKS; it never defaults to PASS.
* BTC (and any symbol on the block list) is blocked before any scoring.
* The score is a *quality* measure; the hard gates are *veto* conditions.
  A high score cannot override a veto.
"""
from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

APPROVED_HOSTS = ("https://api.gateio.ws", "https://fx-api.gateio.ws")
CANDLE_PATH = "/api/v4/futures/usdt/candlesticks"
TF_SECONDS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}
SIGNAL_TIMEFRAMES = ("5m", "15m")
HTF_FOR = {"5m": "15m", "15m": "1h"}

# No allowlist. An empty value means "no restriction", never "nothing": every
# non-BTC symbol is admitted here, and the exchange contract preflight decides
# whether Gate.io actually lists it. The name is kept so any external reference
# keeps working.
DEFAULT_ALLOWLIST = ""
DEFAULT_BLOCKED = "BTCUSDT,BTCDOMUSDT,BTCUSDT.P,BTCUSD"

# Component ceilings. They sum to 100 so the score is directly readable.
W_TREND = 25
W_MOMENTUM = 15
W_VOLUME = 15
W_STRUCTURE = 15
W_VOLATILITY = 15
W_RR = 15


def _env_str(name, default):
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip()


def _env_float(name, default):
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return float(default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(default)
    return value if math.isfinite(value) else float(default)


def _env_int(name, default):
    return int(_env_float(name, default))


def _env_bool(name, default):
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return bool(default)
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _csv_set(raw):
    """Parse a comma-separated env value into bare, normalized symbols.

    Entries are normalized the same way incoming alerts are, so a value copied
    straight from TradingView ("GATEIO:ETHUSDT.P") still matches the bare
    symbol the webhook delivers. An empty value yields an empty set.
    """
    out = set()
    for item in str(raw).split(","):
        bare = _bare_symbol(item)
        if bare:
            out.add(bare)
    return frozenset(out)


class ConfluenceConfig:
    """Tunable thresholds. Every value is overridable by environment variable."""

    __slots__ = (
        "allowlist", "blocked", "min_score", "min_rr", "min_stop_atr",
        "max_stop_atr", "min_atr_pct", "max_atr_pct", "cooldown_minutes",
        "require_htf", "max_chase_atr", "min_volume_ratio", "max_candle_age",
    )

    def __init__(self, **kw):
        self.allowlist = kw.get("allowlist", frozenset())
        self.blocked = kw.get("blocked", _csv_set(DEFAULT_BLOCKED))
        self.min_score = kw.get("min_score", 60)
        self.min_rr = kw.get("min_rr", 1.5)
        self.min_stop_atr = kw.get("min_stop_atr", 0.6)
        self.max_stop_atr = kw.get("max_stop_atr", 3.0)
        self.min_atr_pct = kw.get("min_atr_pct", 0.15)
        self.max_atr_pct = kw.get("max_atr_pct", 3.0)
        self.cooldown_minutes = kw.get("cooldown_minutes", 30)
        self.require_htf = kw.get("require_htf", True)
        self.max_chase_atr = kw.get("max_chase_atr", 1.5)
        self.min_volume_ratio = kw.get("min_volume_ratio", 1.0)
        self.max_candle_age = kw.get("max_candle_age", 2)

    @classmethod
    def from_env(cls):
        return cls(
            allowlist=_csv_set(_env_str("LUXALGO_SYMBOL_ALLOWLIST", DEFAULT_ALLOWLIST)),
            blocked=_csv_set(_env_str("LUXALGO_BLOCKED_SYMBOLS", DEFAULT_BLOCKED)),
            min_score=_env_int("LUXALGO_MIN_CONFLUENCE_SCORE", 60),
            min_rr=_env_float("LUXALGO_MIN_RR", 1.5),
            min_stop_atr=_env_float("LUXALGO_MIN_STOP_ATR", 0.6),
            max_stop_atr=_env_float("LUXALGO_MAX_STOP_ATR", 3.0),
            min_atr_pct=_env_float("LUXALGO_MIN_ATR_PCT", 0.15),
            max_atr_pct=_env_float("LUXALGO_MAX_ATR_PCT", 3.0),
            cooldown_minutes=_env_int("LUXALGO_COOLDOWN_MINUTES", 30),
            require_htf=_env_bool("LUXALGO_REQUIRE_HTF_ALIGN", True),
            max_chase_atr=_env_float("LUXALGO_MAX_CHASE_ATR", 1.5),
            min_volume_ratio=_env_float("LUXALGO_MIN_VOLUME_RATIO", 1.0),
            max_candle_age=_env_int("LUXALGO_MAX_CANDLE_AGE", 2),
        )

    def as_dict(self):
        out = {}
        for key in self.__slots__:
            value = getattr(self, key)
            out[key] = sorted(value) if isinstance(value, frozenset) else value
        return out


# --------------------------------------------------------------------------
# Symbol scope
# --------------------------------------------------------------------------

def _bare_symbol(symbol):
    """Strip a TradingView exchange prefix and the .P perpetual suffix."""
    if not isinstance(symbol, str):
        return ""
    raw = symbol.strip().upper()
    if ":" in raw:
        raw = raw.split(":", 1)[1]
    if raw.endswith(".P"):
        raw = raw[:-2]
    return raw


def symbol_permitted(symbol, config=None):
    """Return (allowed: bool, reason: str). BTC is blocked unconditionally.

    There is no allowlist. Every non-BTC symbol is permitted here; the block
    list (LUXALGO_BLOCKED_SYMBOLS) and the BTC prefix veto are the only symbol
    restrictions. A symbol still has to pass the exchange contract preflight
    downstream, so this answers "is it policy-permitted", not "does Gate.io
    list it".
    """
    cfg = config or ConfluenceConfig.from_env()
    bare = _bare_symbol(symbol)
    if not bare:
        return False, "SYMBOL_MISSING"
    if bare in cfg.blocked or bare.startswith("BTC"):
        return False, "BTC_BLOCKED"
    return True, "OK"


# --------------------------------------------------------------------------
# Indicators (pure, completed candles only)
# --------------------------------------------------------------------------

def _ema(values, n):
    if len(values) < n or n <= 0:
        return None
    acc = sum(values[:n]) / n
    for v in values[n:]:
        acc += (v - acc) * (2.0 / (n + 1))
    return acc


def _rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    diffs = [b - a for a, b in zip(closes[-(n + 1):-1], closes[-n:])]
    up = sum(max(x, 0.0) for x in diffs) / n
    down = sum(max(-x, 0.0) for x in diffs) / n
    if up == 0 and down == 0:
        return 50.0
    if down == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + up / down)


def _atr(bars, n=14):
    """Wilder ATR over completed bars."""
    if len(bars) < n + 1:
        return None
    trs = []
    for prev, cur in zip(bars[:-1], bars[1:]):
        trs.append(max(cur["h"] - cur["l"],
                       abs(cur["h"] - prev["c"]),
                       abs(cur["l"] - prev["c"])))
    if len(trs) < n:
        return None
    atr = sum(trs[:n]) / n
    for tr in trs[n:]:
        atr = (atr * (n - 1) + tr) / n
    return atr if math.isfinite(atr) and atr > 0 else None


def _num(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def normalize_candles(raw, timeframe, now=None):
    """Coerce a Gate.io candlestick payload into completed bars, oldest first.

    In-progress bars are dropped: a signal must never be scored against a
    candle that can still change.
    """
    if not isinstance(raw, list):
        return []
    secs = TF_SECONDS.get(timeframe)
    if not secs:
        return []
    now_ts = (now or datetime.now(timezone.utc)).timestamp()
    out = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        t = _num(item.get("t"))
        o = _num(item.get("o"))
        h = _num(item.get("h"))
        l = _num(item.get("l"))
        c = _num(item.get("c"))
        v = _num(item.get("v")) or 0.0
        if None in (t, o, h, l, c):
            continue
        if c <= 0 or h < c or l > c or h < l or v < 0:
            continue
        if t + secs > now_ts:
            continue  # still forming
        out[int(t)] = {"t": int(t), "o": o, "h": h, "l": l, "c": c, "v": v}
    return [out[k] for k in sorted(out)]


def fetch_candles(contract, timeframe, limit=120, timeout=6.0):
    """Read-only public Gate.io futures candles. Returns [] on any failure."""
    if timeframe not in TF_SECONDS:
        return []
    query = urlencode({"contract": contract, "interval": timeframe,
                       "limit": int(limit)})
    for host in APPROVED_HOSTS:
        req = Request(host + CANDLE_PATH + "?" + query, headers={
            "Accept": "application/json",
            "User-Agent": "LuxAlgo-PRO-Confluence/1.0",
        }, method="GET")
        try:
            with urlopen(req, timeout=timeout) as res:
                data = json.loads(res.read(2000000))
        except Exception:  # noqa: BLE001 - any transport failure means no data
            continue
        if isinstance(data, list) and data:
            return data
    return []


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

def _score_trend(side, closes, ema9, ema21, htf_closes):
    pts = 0
    if ema9 is not None and ema21 is not None:
        if (side == "LONG" and ema9 > ema21) or (side == "SHORT" and ema9 < ema21):
            pts += 12
    price = closes[-1]
    if ema21 is not None:
        if (side == "LONG" and price > ema21) or (side == "SHORT" and price < ema21):
            pts += 6
    htf_ema = _ema(htf_closes, 50) if htf_closes and len(htf_closes) >= 50 else None
    if htf_ema is not None and len(htf_closes) >= 55:
        slope = htf_ema - _ema(htf_closes[:-5], 50)
        if (side == "LONG" and slope > 0) or (side == "SHORT" and slope < 0):
            pts += 7
    return min(pts, W_TREND)


def _score_momentum(side, rsi):
    if rsi is None:
        return 0
    if side == "LONG":
        if 52.0 <= rsi <= 68.0:
            return W_MOMENTUM
        if 50.0 <= rsi < 52.0 or 68.0 < rsi <= 72.0:
            return 8
        return 0
    if 32.0 <= rsi <= 48.0:
        return W_MOMENTUM
    if 28.0 <= rsi < 32.0 or 48.0 < rsi <= 50.0:
        return 8
    return 0


def _score_volume(volume_ratio, config):
    if volume_ratio is None:
        return 0
    if volume_ratio >= 1.3:
        return W_VOLUME
    if volume_ratio >= max(1.1, config.min_volume_ratio):
        return 11
    if volume_ratio >= 0.9:
        return 5
    return 0


def _score_structure(side, bars, ema9, ema21, atr):
    if len(bars) < 22 or atr is None:
        return 0
    prior = bars[-21:-1]
    price = bars[-1]["c"]
    hi = max(b["h"] for b in prior)
    lo = min(b["l"] for b in prior)
    if side == "LONG" and price > hi:
        return W_STRUCTURE
    if side == "SHORT" and price < lo:
        return W_STRUCTURE
    if ema21 is not None and ema9 is not None:
        near = abs(price - ema21) <= 0.5 * atr
        aligned = (ema9 > ema21) if side == "LONG" else (ema9 < ema21)
        if near and aligned:
            return 10
    return 4


def _score_volatility(atr_pct, stop_atr, config):
    atr_ok = (atr_pct is not None and
              config.min_atr_pct <= atr_pct <= config.max_atr_pct)
    stop_ok = (stop_atr is not None and
               config.min_stop_atr <= stop_atr <= config.max_stop_atr)
    if atr_ok and stop_ok:
        return W_VOLATILITY
    if atr_ok or stop_ok:
        return 7
    return 0


def _score_rr(rr):
    if rr is None:
        return 0
    if rr >= 2.5:
        return W_RR
    if rr >= 2.0:
        return 12
    if rr >= 1.5:
        return 8
    return 0


def evaluate(*, symbol, side, timeframe, entry, stop, take_profit,
             candles, htf_candles=None, config=None, now=None,
             last_entry_epoch=None):
    """Score one LuxAlgo signal. Returns a decision dict; never raises.

    ``candles`` / ``htf_candles`` are raw Gate.io payloads (or already
    normalized bar lists). ``last_entry_epoch`` is the epoch seconds of the
    most recent accepted entry for this symbol, used for the cooldown veto.
    """
    cfg = config or ConfluenceConfig.from_env()
    now = now or datetime.now(timezone.utc)
    blockers = []
    components = {"trend": 0, "momentum": 0, "volume": 0,
                  "structure": 0, "volatility": 0, "rr": 0}
    metrics = {}

    def result(decision, score=0, summary=""):
        return {
            "decision": decision,
            "score": int(score),
            "min_score": int(cfg.min_score),
            "components": components,
            "metrics": metrics,
            "blockers": blockers,
            "summary": summary or ("PASS" if decision == "PASS" else ",".join(blockers)),
            "config": cfg.as_dict(),
        }

    # --- veto 0: symbol scope (BTC can never pass) -------------------------
    ok, reason = symbol_permitted(symbol, cfg)
    if not ok:
        blockers.append(reason)
        return result("BLOCK", 0, reason)

    if side not in ("LONG", "SHORT"):
        blockers.append("INVALID_SIDE")
        return result("BLOCK", 0, "INVALID_SIDE")
    # Only the two timeframes the bot trades may carry a signal. 1h/4h are
    # known here solely as higher-timeframe context feeds.
    if timeframe not in SIGNAL_TIMEFRAMES:
        blockers.append("INVALID_TIMEFRAME")
        return result("BLOCK", 0, "INVALID_TIMEFRAME")

    # --- veto 1: price geometry -------------------------------------------
    e, s, t = _num(entry), _num(stop), _num(take_profit)
    if None in (e, s, t) or min(e, s, t) <= 0:
        blockers.append("LEVELS_UNUSABLE")
        return result("BLOCK", 0, "LEVELS_UNUSABLE")
    if side == "LONG" and not (s < e < t):
        blockers.append("GEOMETRY_INVALID")
        return result("BLOCK", 0, "GEOMETRY_INVALID")
    if side == "SHORT" and not (t < e < s):
        blockers.append("GEOMETRY_INVALID")
        return result("BLOCK", 0, "GEOMETRY_INVALID")

    risk = abs(e - s)
    reward = abs(t - e)
    rr = reward / risk if risk > 0 else None
    metrics["rr"] = round(rr, 3) if rr else None
    if rr is None or rr < cfg.min_rr:
        blockers.append("RR_BELOW_MIN")

    # --- veto 2: market data ----------------------------------------------
    # Always normalize: Gate.io raw candles also carry an "h" key, so a
    # presence check is not enough to detect already-normalized bars.
    bars = normalize_candles(candles, timeframe, now)
    if len(bars) < 30:
        blockers.append("INSUFFICIENT_CANDLES")
        return result("BLOCK", 0, "INSUFFICIENT_CANDLES")

    secs = TF_SECONDS[timeframe]
    age_bars = (now.timestamp() - (bars[-1]["t"] + secs)) / secs
    metrics["candle_age_bars"] = round(age_bars, 2)
    if age_bars > cfg.max_candle_age:
        blockers.append("STALE_CANDLES")

    closes = [b["c"] for b in bars]
    ema9 = _ema(closes, 9)
    ema21 = _ema(closes, 21)
    rsi = _rsi(closes, 14)
    atr = _atr(bars, 14)
    if atr is None:
        blockers.append("ATR_UNAVAILABLE")
        return result("BLOCK", 0, "ATR_UNAVAILABLE")

    price = closes[-1]
    atr_pct = (atr / price) * 100.0 if price > 0 else None
    stop_atr = risk / atr if atr > 0 else None
    metrics.update({
        "price": round(price, 8),
        "ema9": round(ema9, 8) if ema9 else None,
        "ema21": round(ema21, 8) if ema21 else None,
        "rsi14": round(rsi, 2) if rsi is not None else None,
        "atr14": round(atr, 8),
        "atr_pct": round(atr_pct, 3) if atr_pct else None,
        "stop_atr": round(stop_atr, 3) if stop_atr else None,
    })

    # --- veto 3: volatility regime ----------------------------------------
    if atr_pct is None or not (cfg.min_atr_pct <= atr_pct <= cfg.max_atr_pct):
        blockers.append("ATR_REGIME_OUT_OF_BAND")
    if stop_atr is None or stop_atr < cfg.min_stop_atr:
        blockers.append("STOP_TOO_TIGHT")
    elif stop_atr > cfg.max_stop_atr:
        blockers.append("STOP_TOO_WIDE")

    # --- veto 4: chasing an extended move ---------------------------------
    if ema21 is not None and atr > 0:
        extension = (price - ema21) / atr
        metrics["extension_atr"] = round(extension, 3)
        if side == "LONG" and extension > cfg.max_chase_atr:
            blockers.append("CHASE_EXTENSION")
        if side == "SHORT" and extension < -cfg.max_chase_atr:
            blockers.append("CHASE_EXTENSION")

    # --- veto 5: higher-timeframe conflict --------------------------------
    htf_bars = []
    if htf_candles:
        htf_tf = HTF_FOR[timeframe]
        htf_bars = normalize_candles(htf_candles, htf_tf, now)
    htf_closes = [b["c"] for b in htf_bars] if htf_bars else []
    if cfg.require_htf:
        if len(htf_closes) < 55:
            blockers.append("HTF_DATA_UNAVAILABLE")
        else:
            htf_ema = _ema(htf_closes, 50)
            htf_slope = htf_ema - _ema(htf_closes[:-5], 50)
            metrics["htf_slope"] = round(htf_slope, 8)
            if side == "LONG" and htf_slope <= 0:
                blockers.append("HTF_CONFLICT")
            if side == "SHORT" and htf_slope >= 0:
                blockers.append("HTF_CONFLICT")

    # --- veto 6: cooldown after a recent accepted entry -------------------
    if last_entry_epoch:
        elapsed_min = (now.timestamp() - float(last_entry_epoch)) / 60.0
        metrics["minutes_since_last_entry"] = round(elapsed_min, 1)
        if elapsed_min < cfg.cooldown_minutes:
            blockers.append("COOLDOWN_ACTIVE")

    # --- scoring ----------------------------------------------------------
    vols = [b["v"] for b in bars]
    avg_vol = sum(vols[-21:-1]) / 20.0 if len(vols) >= 21 else 0.0
    volume_ratio = (vols[-1] / avg_vol) if avg_vol > 0 else None
    metrics["volume_ratio"] = round(volume_ratio, 3) if volume_ratio else None

    components["trend"] = _score_trend(side, closes, ema9, ema21, htf_closes)
    components["momentum"] = _score_momentum(side, rsi)
    components["volume"] = _score_volume(volume_ratio, cfg)
    components["structure"] = _score_structure(side, bars, ema9, ema21, atr)
    components["volatility"] = _score_volatility(atr_pct, stop_atr, cfg)
    components["rr"] = _score_rr(rr)
    score = sum(components.values())

    if score < cfg.min_score:
        blockers.append("SCORE_BELOW_MIN")

    if blockers:
        return result("BLOCK", score)
    return result("PASS", score, "PASS")


def describe(decision):
    """One-line dashboard string for a decision dict."""
    if not isinstance(decision, dict):
        return "confluence unavailable"
    if decision.get("decision") == "PASS":
        return "confluence PASS %d/%d" % (decision.get("score", 0),
                                          decision.get("min_score", 0))
    return "confluence BLOCK %d/%d: %s" % (
        decision.get("score", 0), decision.get("min_score", 0),
        ",".join(decision.get("blockers") or ["UNKNOWN"]))
