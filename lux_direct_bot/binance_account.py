"""Binance USD-M Futures USER_DATA snapshot: signed GET requests only.

No POST, PUT, DELETE, margin changes, new/cancel orders, or account writes exist
in this module. Credentials only from Render environment variables.
"""
import hashlib
import hmac
import os
import time
from urllib.parse import urlencode

import httpx

BASE = "https://fapi.binance.com"
READ_ONLY_PATHS = frozenset(("/fapi/v3/balance", "/fapi/v3/positionRisk"))


def signed_read(client, path, api_key, api_secret, now_ms=None):
    if path not in READ_ONLY_PATHS:
        raise ValueError("Unsigned or unsafe Binance endpoint denied")
    ts = int(time.time() * 1000) if now_ms is None else int(now_ms)
    query = urlencode({"timestamp": ts, "recvWindow": 5000})
    signature = hmac.new(api_secret.encode("utf-8"), query.encode("utf-8"),
                         hashlib.sha256).hexdigest()
    resp = client.get(BASE + path + "?" + query + "&signature=" + signature,
                      headers={"X-MBX-APIKEY": api_key})
    resp.raise_for_status()
    return resp.json()


def read_account_snapshot(client_factory=httpx.Client):
    api_key = os.environ.get("BINANCE_API_KEY", "")
    api_secret = os.environ.get("BINANCE_API_SECRET", "")
    if not api_key or not api_secret:
        return {"status": "NOT_CONFIGURED", "mode": "READ_ONLY",
                "orders_enabled": False, "positions": []}
    try:
        with client_factory(timeout=4.0, follow_redirects=False) as client:
            balances = signed_read(client, "/fapi/v3/balance", api_key, api_secret)
            positions = signed_read(client, "/fapi/v3/positionRisk", api_key, api_secret)
        if not isinstance(balances, list) or not isinstance(positions, list):
            raise ValueError("Unexpected Binance account response")
        usd = next((x for x in balances if x.get("asset") == "USDT"), None)
        compact_positions = []
        for x in positions:
            qty = str(x.get("positionAmt", "0"))
            if float(qty) == 0:
                continue
            compact_positions.append({
                "symbol": str(x.get("symbol", "")),
                "position_side": str(x.get("positionSide", "BOTH")),
                "amount": qty,
                "entry_price": str(x.get("entryPrice", "")),
                "mark_price": str(x.get("markPrice", "")),
                "unrealized_pnl": str(x.get("unRealizedProfit", "")),
                "leverage": str(x.get("leverage", "")),
            })
        if len(compact_positions) > 50:
            raise ValueError("Unexpected positions count")
        return {"status": "CONNECTED_READ_ONLY", "mode": "READ_ONLY",
                "orders_enabled": False,
                "usdt_balance": usd.get("balance") if usd else None,
                "usdt_available": usd.get("availableBalance") if usd else None,
                "positions": compact_positions}
    except httpx.HTTPStatusError as e:
        return {"status": "ERROR", "mode": "READ_ONLY",
                "http_status": e.response.status_code,
                "orders_enabled": False, "positions": []}
    except (httpx.HTTPError, ValueError, TypeError, KeyError, OverflowError) as e:
        return {"status": "ERROR", "mode": "READ_ONLY",
                "reason": type(e).__name__,
                "orders_enabled": False, "positions": []}
