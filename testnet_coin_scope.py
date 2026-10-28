"""User-selected Gate Futures USDT-M Testnet order-entry scope.

This deliberately limits NEW webhook entries only. Existing exchange positions,
protection recovery and reduce-only emergency closes MUST remain independent
of the list so historical exposure can still be protected and reconciled.
A selected coin still needs a live exchange contract/preflight readback.
"""
SELECTED_SYMBOLS=frozenset({
    "BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT",
    "DOGEUSDT","BNBUSDT","AVAXUSDT","ADAUSDT",
})
SELECTED_CONTRACTS=frozenset(s[:-4]+"_USDT" for s in SELECTED_SYMBOLS)

def selected_webhook_symbol(symbol):
    return isinstance(symbol,str) and symbol in SELECTED_SYMBOLS

def selected_testnet_contract(contract):
    return isinstance(contract,str) and contract in SELECTED_CONTRACTS
