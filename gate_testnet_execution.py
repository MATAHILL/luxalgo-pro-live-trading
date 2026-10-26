"""Isolated Gate.io Futures TESTNET transport. No production order endpoint.

Not connected to LuxAlgo webhooks or the bot. Writing remains disabled unless a
separate verified protected-execution workflow explicitly enables it.
"""
import hashlib
import hmac
import json
import os
import time
from urllib.request import Request, urlopen

TESTNET_HOST = "https://api-testnet.gateapi.io"
ORDER_PATH = "/api/v4/futures/usdt/orders"

class TestnetExecutionDisabled(RuntimeError):
    pass

def signing_message(method: str, path: str, query: str, body: bytes, timestamp: str) -> str:
    if not path.startswith("/api/v4/futures/usdt/"):
        raise ValueError("Only USDT futures API paths are allowed")
    if method not in ("GET", "POST"):
        raise ValueError("Unsupported method")
    return "\n".join((method, path, query, hashlib.sha512(body).hexdigest(), timestamp))

def signed_headers(method: str, path: str, query: str, body: bytes, timestamp: str, key: str, secret: str) -> dict:
    message = signing_message(method, path, query, body, timestamp)
    return {"KEY": key, "Timestamp": timestamp,
            "SIGN": hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha512).hexdigest(),
            "Accept": "application/json", "Content-Type": "application/json"}

def order_payload(contract: str, signed_contracts: int, order_type: str, limit_price: str | None, client_id: str) -> dict:
    """Validate an order envelope; does not verify risk or native stop coverage."""
    import re
    from decimal import Decimal, InvalidOperation
    if not re.fullmatch(r"[A-Z0-9]{2,28}_USDT", contract):
        raise ValueError("Invalid futures contract")
    if type(signed_contracts) is not int or signed_contracts == 0:
        raise ValueError("Nonzero integer contract size required")
    if not re.fullmatch(r"t-[A-Za-z0-9_.-]{1,28}", client_id):
        raise ValueError("Invalid client order ID")
    if order_type == "MARKET":
        if limit_price is not None:
            raise ValueError("Market order cannot have a limit price")
        return dict(contract=contract, size=signed_contracts, price="0", tif="ioc", text=client_id)
    if order_type == "LIMIT":
        try:
            price = Decimal(str(limit_price))
        except (InvalidOperation, ValueError, TypeError):
            raise ValueError("Invalid limit price") from None
        if not price.is_finite() or price <= 0:
            raise ValueError("Limit price must be positive")
        return dict(contract=contract, size=signed_contracts, price=str(price), tif="gtc", text=client_id)
    raise ValueError("Order type must be MARKET or LIMIT")

def prepare_order_for_review(payload: dict) -> dict:
    """Only prepare validated data; deliberately never submit an order."""
    checked = order_payload(payload["contract"], payload["size"], payload["order_type"],
                            payload.get("limit_price"), payload["client_id"])
    return dict(environment="GATE_FUTURES_TESTNET", validated_payload=checked,
                submitted=False, protective_orders_verified=False,
                blocker="ENTRY_AND_EXCHANGE_NATIVE_SL_TP_REQUIRE_ATOMIC_RECOVERY_AND_RECONCILIATION")

def send_protected_order(*_args, **_kwargs):
    """Fail closed until order, protective stops, reconciliation and recovery are integrated."""
    raise TestnetExecutionDisabled("Protected Testnet execution is not yet implemented; no order sent")
