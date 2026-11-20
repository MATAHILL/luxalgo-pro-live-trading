"""User-selected Gate Futures USDT-M order-entry scope.

This deliberately limits NEW webhook entries only. Existing exchange positions,
protection recovery and reduce-only emergency closes MUST remain independent
of the list so historical exposure can still be protected and reconciled.
A selected coin still needs a live exchange contract/preflight readback.

XRP-ONLY POLICY
---------------
The live bot is scoped to XRPUSDT. BTC is refused unconditionally, before the
allowlist is consulted, so a later edit that re-widens SELECTED_SYMBOLS cannot
silently re-admit BTC. TradingView sends prefixed perpetual tickers such as
``GATEIO:XRPUSDT.P``; those are normalized to the bare USDT contract name
before the scope test, otherwise every real alert would be refused.
"""
import re

SELECTED_SYMBOLS = frozenset({
    "XRPUSDT",
})
SELECTED_CONTRACTS = frozenset(s[:-4] + "_USDT" for s in SELECTED_SYMBOLS)

# Hard exclusions, checked before the allowlist. Any BTC* ticker is refused.
BLOCKED_SYMBOLS = frozenset({
    "BTCUSDT", "BTCDOMUSDT", "BTCUSD", "BTCUSDT.P",
})
ALLOWED_PREFIXES = ("GATEIO", "BINANCE")

_USDT_RE = re.compile(r"[A-Z0-9]{3,25}USDT")


def _bare_symbol(symbol):
    """Strip a TradingView exchange prefix and the .P perpetual suffix."""
    if not isinstance(symbol, str):
        return ""
    raw = symbol.strip().upper()
    if ":" in raw:
        source, raw = raw.split(":", 1)
        if source not in ALLOWED_PREFIXES:
            return ""
    if raw.endswith(".P"):
        raw = raw[:-2]
    return raw


def selected_webhook_symbol(symbol):
    """True only for an allowlisted, non-BTC USDT contract."""
    bare = _bare_symbol(symbol)
    if not bare or not _USDT_RE.fullmatch(bare):
        return False
    if bare in BLOCKED_SYMBOLS or bare.startswith("BTC"):
        return False
    return bare in SELECTED_SYMBOLS


def selected_testnet_contract(contract):
    """True only for an allowlisted, non-BTC Gate.io contract name."""
    if not isinstance(contract, str):
        return False
    raw = contract.strip().upper()
    if raw in BLOCKED_SYMBOLS or raw.startswith("BTC"):
        return False
    return raw in SELECTED_CONTRACTS
