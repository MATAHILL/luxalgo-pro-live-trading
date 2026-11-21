"""Gate Futures USDT-M order-entry scope: no allowlist, blocked list only.

Every symbol is accepted. The only exclusions are the hard policy blocks in
BLOCKED_SYMBOLS (BTCUSDT, BTCDOMUSDT, BTCUSD) plus the BTC prefix veto, so an
accidental re-widening of anything cannot silently re-admit BTC.

This deliberately limits NEW webhook entries only. Existing exchange positions,
protection recovery and reduce-only emergency closes MUST remain independent
of the list so historical exposure can still be protected and reconciled.
A symbol still needs a live exchange contract/preflight readback: this module
answers "is it policy-permitted", not "does Gate.io list it". A symbol that
Gate.io does not list is rejected downstream by the contract preflight.
"""
import os

DEFAULT_BLOCKED = "BTCUSDT,BTCDOMUSDT,BTCUSD,BTCUSDT.P"


def _bare(symbol):
    if not isinstance(symbol, str):
        return ""
    raw = symbol.strip().upper()
    if ":" in raw:
        raw = raw.split(":", 1)[1]
    if raw.endswith(".P"):
        raw = raw[:-2]
    return raw


def _csv_symbols(raw, default):
    """Parse a comma-separated env value into bare symbols.

    Entries are normalized the same way incoming alerts are, so a value copied
    straight from TradingView ("GATEIO:XRPUSDT.P") still matches. An empty or
    missing value falls back to ``default`` so the block list can never be
    cleared by accident.
    """
    text = raw if raw is not None and str(raw).strip() else default
    out = set()
    for item in str(text).split(","):
        bare = _bare(item)
        if bare:
            out.add(bare)
    return frozenset(out)


def _contracts(symbols):
    """Map bare USDT symbols to Gate.io contract names (XRPUSDT -> XRP_USDT)."""
    out = set()
    for sym in symbols:
        if sym.endswith("USDT") and len(sym) > 4:
            out.add(sym[:-4] + "_USDT")
    return frozenset(out)


# No allowlist. These stay defined (empty) so any external reference to the
# names keeps working; an empty set means "no restriction", never "nothing".
SELECTED_SYMBOLS = frozenset()
SELECTED_CONTRACTS = frozenset()

# Hard exclusions. Checked before anything else so BTC can never pass.
BLOCKED_SYMBOLS = _csv_symbols(os.getenv("LUXALGO_BLOCKED_SYMBOLS"), DEFAULT_BLOCKED)
BLOCKED_CONTRACTS = frozenset({"BTC_USDT", "BTCDOM_USDT"})


def selected_webhook_symbol(symbol):
    bare = _bare(symbol)
    if not bare or bare in BLOCKED_SYMBOLS or bare.startswith("BTC"):
        return False
    return True


def selected_testnet_contract(contract):
    if not isinstance(contract, str):
        return False
    name = contract.strip().upper()
    if name in BLOCKED_CONTRACTS or name.startswith("BTC_"):
        return False
    return True
