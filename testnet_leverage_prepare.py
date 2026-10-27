"""One-shot, TESTNET-only cross leverage adjustment to 5x.

Does not place an order. Verifies zero open positions, entry orders and triggers,
then reads both sides' exchange leverage after write. Not for production keys.
"""
import json
import os
import time
from decimal import Decimal, InvalidOperation
from urllib.request import Request, urlopen
from gate_testnet_execution import TESTNET_HOST, signed_headers, TestnetExecutionDisabled
from gate_testnet_broker import _request_testnet, ROOT
from testnet_readiness import _nonzero_position

CONTRACT="ETH_USDT"
PATH=ROOT+"/positions/"+CONTRACT+"/set_leverage"
QUERY="leverage=5&margin_mode=cross"

def _write_testnet_leverage():
    if os.getenv("GATE_TESTNET_PREPARE_LEVERAGE_5X","false").lower()!="true":
        raise TestnetExecutionDisabled("One-time Testnet leverage adjustment not enabled")
    key=os.getenv("GATE_TESTNET_API_KEY","").strip()
    secret=os.getenv("GATE_TESTNET_API_SECRET","").strip()
    if not key or not secret:
        raise TestnetExecutionDisabled("Missing Testnet credentials")
    data=b""
    headers=signed_headers("POST",PATH,QUERY,data,str(int(time.time())),key,secret)
    req=Request(TESTNET_HOST+PATH+"?"+QUERY,data=data,headers=headers,method="POST")
    with urlopen(req,timeout=8) as res:
        payload=json.loads(res.read(100000))
    if not isinstance(payload,(dict,list)):
        raise ValueError("Unexpected Gate leverage update response")
    return payload

def _read_side(request,side):
    r=request("GET",ROOT+"/get_leverage/"+CONTRACT,
              query="pos_margin_mode=cross&dual_side=dual_"+side)
    if not isinstance(r,dict):
        raise ValueError("Invalid leverage readback")
    value=Decimal(str(r["Lever"]))
    if not value.is_finite() or value<=0:
        raise ValueError("Nonpositive leverage")
    return value

def prepare_testnet_leverage_5x(*, request=None, writer=None):
    """One-shot preparation, never trade. Fail closed on any unknown response."""
    request=request or _request_testnet
    writer=writer or _write_testnet_leverage
    if os.getenv("GATE_TESTNET_PREPARE_LEVERAGE_5X","false").lower()!="true":
        return dict(state="LEVERAGE_SETUP_DISABLED",changed=False)
    try:
        account=request("GET",ROOT+"/accounts")
        if (not isinstance(account,dict) or account.get("currency")!="USDT"
            or account.get("in_dual_mode") is not True):
            return dict(state="TESTNET_DUAL_ACCOUNT_UNVERIFIED",changed=False)
        positions=request("GET",ROOT+"/positions")
        orders=request("GET",ROOT+"/orders",query="status=open")
        triggers=request("GET",ROOT+"/price_orders",query="status=open")
        if not all(isinstance(x,list) for x in (positions,orders,triggers)):
            return dict(state="OPEN_ORDERS_READBACK_INVALID",changed=False)
        if (not all(isinstance(p,dict) for p in positions+orders+triggers)
            or any(_nonzero_position(p) for p in positions) or orders or triggers):
            return dict(state="TESTNET_ACCOUNT_NOT_FLAT",changed=False)
        current_long=_read_side(request,"long")
        current_short=_read_side(request,"short")
        if current_long==Decimal(5) and current_short==Decimal(5):
            return dict(state="LEVERAGE_5X_ALREADY_VERIFIED",changed=False)
        writer() # no blind retry; verify after any uncertainty on a future invocation
        long=_read_side(request,"long")
        short=_read_side(request,"short")
        if long!=Decimal(5) or short!=Decimal(5):
            return dict(state="LEVERAGE_5X_READBACK_FAILED",changed=False)
        return dict(state="TESTNET_LEVERAGE_5X_VERIFIED",changed=True)
    except Exception as exc:
        return dict(state="LEVERAGE_PREPARATION_FAILED_"+type(exc).__name__.upper(),changed=False)
