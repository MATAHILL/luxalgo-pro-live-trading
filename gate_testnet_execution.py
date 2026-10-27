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

def protective_trigger_payload(contract: str, side: str, trigger_kind: str,
                               trigger_price: str, mark_reference_price: str,
                               position_mode: str = "dual") -> dict:
    """Construct documented Gate Futures position-close trigger; never submit.

    Require an actual fresh mark before choosing above/below trigger rule.
    This avoids creating an already-triggered protective order.
    """
    from decimal import Decimal, InvalidOperation
    import re
    if not re.fullmatch(r"[A-Z0-9]{2,28}_USDT", contract):
        raise ValueError("Invalid contract")
    if side not in ("LONG", "SHORT") or trigger_kind not in ("STOP_LOSS", "TAKE_PROFIT"):
        raise ValueError("Invalid position side or trigger purpose")
    if position_mode != "dual":
        raise ValueError("Only explicitly verified dual-mode plans supported")
    try:
        trigger=Decimal(str(trigger_price))
        mark=Decimal(str(mark_reference_price))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Invalid trigger or mark") from None
    if not all(p.is_finite() and p>0 for p in (trigger,mark)) or trigger==mark:
        raise ValueError("Invalid or already reached trigger")
    above=(side=="LONG" and trigger_kind=="TAKE_PROFIT") or (side=="SHORT" and trigger_kind=="STOP_LOSS")
    if (trigger>mark) != above:
        raise ValueError("Trigger is on incorrect side of fresh mark")
    close_side="long" if side=="LONG" else "short"
    return {
        "initial": {"contract":contract, "size":0, "price":"0", "tif":"ioc",
                    "auto_size":"close_"+close_side},
        "trigger": {"strategy_type":0, "price_type":1,
                    "price":str(trigger), "rule":1 if above else 2,
                    "expiration":86400},
        "order_type":"close-"+close_side+"-position"
    }


def signed_testnet_request(method: str, path: str, body: dict | None = None,
                           query: str = "", *, timeout: int = 8) -> dict | list:
    """Strict Testnet-only REST transport. No retries of order-writing requests.

    This helper does not authorize trading and is never invoked by webhook.
    Calling code must validate risk, recovery, native protection and idempotency.
    """
    from urllib.error import HTTPError, URLError
    if path not in (ORDER_PATH, "/api/v4/futures/usdt/price_orders",
                    "/api/v4/futures/usdt/positions"):
        raise ValueError("API path not allowlisted")
    if method == "POST" and path == "/api/v4/futures/usdt/positions":
        raise ValueError("Position write prohibited")
    if method not in ("GET", "POST"):
        raise ValueError("Unsupported HTTP method")
    if method == "POST":
        if path != ORDER_PATH:
            raise TestnetExecutionDisabled("Native protection writer is not independently verified")
        # A single environment toggle is NOT authorization to create an unprotected position.
        # Fail closed until the protected execution coordinator has been implemented,
        # tested against the exchange and given its own guarded submission path.
        raise TestnetExecutionDisabled("Protected Testnet order coordinator not verified; no entry sent")
        if not isinstance(body, dict) or body.get("contract") is None:
            raise ValueError("Explicit validated Testnet entry payload required")
    if method == "GET" and body is not None:
        raise ValueError("GET body forbidden")
    if method == "POST" and not isinstance(body,dict):
        raise ValueError("POST requires JSON object")
    key=os.getenv("GATE_TESTNET_API_KEY","").strip()
    secret=os.getenv("GATE_TESTNET_API_SECRET","").strip()
    if not key or not secret:
        raise TestnetExecutionDisabled("Gate Testnet credentials unavailable")
    data=(json.dumps(body,separators=(",",":"),allow_nan=False).encode("utf-8")
          if method=="POST" else b"")
    stamp=str(int(time.time()))
    headers=signed_headers(method,path,query,data,stamp,key,secret)
    url=TESTNET_HOST+path+("?"+query if query else "")
    request=Request(url,data=data if method=="POST" else None,
                    headers=headers,method=method)
    # Never retry a write after a transport timeout: order may already exist.
    with urlopen(request,timeout=timeout) as response:
        result=json.loads(response.read(150000))
    if not isinstance(result,(dict,list)):
        raise ValueError("Unexpected Testnet response")
    return result


def verify_testnet_protection(order: dict, positions: list, triggers: list,
                              *, expected_contract: str, expected_side: str,
                              stop_price: str, target_price: str) -> dict:
    """Fail-closed reconciliation of observed exchange data. Read-only/pure.

    An accepted order is not a fill; a position alone is not protection.
    Trigger orders must match the same contract and close the relevant side.
    """
    from decimal import Decimal, InvalidOperation
    if not isinstance(order,dict) or not isinstance(positions,list) or not isinstance(triggers,list):
        raise ValueError("Incomplete exchange readback")
    if expected_side not in ("LONG","SHORT"):
        raise ValueError("Unknown side")
    if order.get("contract") != expected_contract or str(order.get("finish_as","")).lower() != "filled":
        return dict(verified=False,reason="ENTRY_FILL_NOT_VERIFIED")
    close_action="close_long" if expected_side=="LONG" else "close_short"
    relevant=[p for p in positions if isinstance(p,dict)
              and p.get("contract")==expected_contract and
              p.get("mode")==("dual_long" if expected_side=="LONG" else "dual_short")
              and str(p.get("size","0")) not in ("0","0.0","")]
    if len(relevant)!=1:
        return dict(verified=False,reason="OPEN_POSITION_NOT_VERIFIED")
    required=[]
    for price,rule in ((stop_price,2 if expected_side=="LONG" else 1),
                       (target_price,1 if expected_side=="LONG" else 2)):
        try:
            target=Decimal(str(price))
        except (InvalidOperation,TypeError,ValueError):
            return dict(verified=False,reason="INVALID_PROTECTION_PRICE")
        matched=[]
        for item in triggers:
            if not isinstance(item,dict) or item.get("status")!="open":
                continue
            initial=item.get("initial") or {}
            trigger=item.get("trigger") or {}
            if initial.get("contract")!=expected_contract or initial.get("auto_size")!=close_action:
                continue
            try:
                ok=Decimal(str(trigger.get("price")))==target and int(trigger.get("rule"))==rule
            except (InvalidOperation,TypeError,ValueError):
                ok=False
            if ok:
                matched.append(str(item.get("id") or ""))
        if len(matched)!=1 or not matched[0]:
            return dict(verified=False,reason="NATIVE_STOP_OR_TARGET_NOT_VERIFIED")
        required.append(matched[0])
    if len(set(required))!=2:
        return dict(verified=False,reason="PROTECTIVE_ORDER_IDS_NOT_DISTINCT")
    return dict(verified=True,reason="ENTRY_POSITION_AND_TWO_NATIVE_TRIGGERS_VERIFIED",
                protective_order_ids=required)


def submit_testnet_entry(payload: dict, *, allow_unprotected_entry: bool = False) -> dict:
    """Implement an isolated signed futures TESTNET entry POST.

    Not called from webhook. Disabled by default via GATE_TESTNET_ENTRY_WRITE_ENABLED.
    Restricts entry to 1 contract and IOC only pending native protection proof.
    An HTTP timeout is UNKNOWN, never evidence of failure; never retry blindly.
    """
    if allow_unprotected_entry:
        raise TestnetExecutionDisabled("Unprotected entry override prohibited")
    if os.getenv("GATE_TESTNET_ENTRY_WRITE_ENABLED", "false").lower() != "true":
        raise TestnetExecutionDisabled("Testnet entry writing disabled by default")
    raise TestnetExecutionDisabled("Protected execution coordinator not ready; no entry sent")
    if not isinstance(payload, dict):
        raise ValueError("Order request must be a dict")
    validated=order_payload(payload["contract"],payload["size"],payload["order_type"],
                            payload.get("limit_price"),payload["client_id"])
    if abs(validated["size"]) != 1:
        raise TestnetExecutionDisabled("Testnet entry capped at exactly one contract")
    if validated["tif"] != "ioc":
        raise TestnetExecutionDisabled("Pending Limit orders require cancellation/recovery coordinator")
    # Explicitly only Testnet. Production URL and production keys are never loaded.
    response=signed_testnet_request("POST",ORDER_PATH,body=validated,timeout=8)
    if not isinstance(response,dict) or not response.get("id"):
        raise RuntimeError("Testnet entry response ambiguous; inspect exchange before any retry")
    return dict(exchange="GATE_FUTURES_TESTNET",order_id=str(response["id"]),
                order=response,submitted=True,fill_verified=False,
                native_protection_verified=False)


def send_protected_order(*_args, **_kwargs):
    """Fail closed until order, protective stops, reconciliation and recovery are integrated."""
    raise TestnetExecutionDisabled("Protected Testnet execution is not yet implemented; no order sent")
