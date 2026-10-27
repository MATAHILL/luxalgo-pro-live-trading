"""One-shot Testnet Hedge Mode preparation, never sends trade orders.

Changes TESTNET position mode only when account is fully flat and no orders
or native triggers remain. Disabled unless PREPARE flag is explicitly true.
"""
import os
import json
import time
from urllib.request import Request, urlopen
from gate_testnet_execution import TESTNET_HOST, signed_headers, TestnetExecutionDisabled
from gate_testnet_broker import _request_testnet, ROOT
from testnet_readiness import _nonzero_position

MODE_PATH=ROOT+"/set_position_mode"
MODE_QUERY="position_mode=dual"


def _write_testnet_hedge_mode():
    if os.getenv("GATE_TESTNET_PREPARE_HEDGE_MODE","false").lower()!="true":
        raise TestnetExecutionDisabled("Testnet-only mode setup is OFF")
    key=os.getenv("GATE_TESTNET_API_KEY","").strip()
    secret=os.getenv("GATE_TESTNET_API_SECRET","").strip()
    if not key or not secret:
        raise TestnetExecutionDisabled("Testnet credentials missing")
    data=b""
    headers=signed_headers("POST",MODE_PATH,MODE_QUERY,data,str(int(time.time())),key,secret)
    req=Request(TESTNET_HOST+MODE_PATH+"?"+MODE_QUERY,data=data,
                headers=headers,method="POST")
    # Never repeat a possibly accepted request following network uncertainty.
    with urlopen(req,timeout=8) as res:
        result=json.loads(res.read(65536))
    if not isinstance(result,dict):
        raise ValueError("Unexpected Testnet mode-change response")
    return result


def prepare_testnet_hedge_mode(*, request=None, writer=None):
    """Check account-wide orders and holdings, optionally switch to dual mode.

    This is an account setting on TESTNET only. No order creation.
    """
    request=request or _request_testnet
    writer=writer or _write_testnet_hedge_mode
    try:
        account=request("GET",ROOT+"/accounts")
        if not isinstance(account,dict) or account.get("currency")!="USDT":
            return {"state":"ACCOUNT_UNVERIFIED","changed":False}
        if account.get("in_dual_mode") is True and account.get("position_mode") in (None,"dual"):
            return {"state":"DUAL_MODE_ALREADY_CONFIRMED","changed":False}
        if os.getenv("GATE_TESTNET_PREPARE_HEDGE_MODE","false").lower()!="true":
            return {"state":"MODE_SETUP_DISABLED","changed":False}
        positions=request("GET",ROOT+"/positions")
        orders=request("GET",ROOT+"/orders",query="status=open")
        triggers=request("GET",ROOT+"/price_orders",query="status=open")
        if (not isinstance(positions,list) or not isinstance(orders,list) or not isinstance(triggers,list)
            or not all(isinstance(x,dict) for x in positions+orders+triggers)):
            return {"state":"PENDING_ORDER_READBACK_INVALID","changed":False}
        if any(_nonzero_position(p) for p in positions) or orders or triggers:
            return {"state":"OPEN_POSITIONS_OR_ORDERS_BLOCK_MODE_SWITCH","changed":False}
        result=writer()
        if not isinstance(result,dict) or result.get("currency")!="USDT":
            return {"state":"MODE_SWITCH_RESPONSE_INVALID","changed":False}
        verified=request("GET",ROOT+"/accounts")
        if (not isinstance(verified,dict) or verified.get("in_dual_mode") is not True
            or verified.get("position_mode","dual")!="dual"):
            return {"state":"MODE_SWITCH_READBACK_FAILED","changed":False}
        return {"state":"TESTNET_DUAL_MODE_CONFIRMED","changed":True}
    except Exception as exc:
        return {"state":"MODE_PREPARATION_FAILED_"+type(exc).__name__.upper(),
                "changed":False}
