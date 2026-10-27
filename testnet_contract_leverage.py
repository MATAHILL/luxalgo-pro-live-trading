"""Guarded Gate.io FUTURES TESTNET 5x leverage preparation for a fresh contract.

Not an order sender. Only changes an exchange account setting when the entire
Testnet account is provably flat and there are no open orders or native triggers.
Never accesses production credentials, hosts or account settings.
"""
import json
import os
import re
import time
from decimal import Decimal,InvalidOperation
from urllib.request import Request,urlopen
from gate_testnet_execution import TESTNET_HOST,signed_headers
from gate_testnet_broker import _request_testnet,ROOT

def _leverage(read,contract,side):
    result=read("GET",ROOT+"/get_leverage/"+contract,
                query="pos_margin_mode=cross&dual_side=dual_"+side)
    value=Decimal(str(result["Lever"]))
    if not value.is_finite() or value<=0:
        raise ValueError("Invalid Testnet leverage readback")
    return value

def _write_5x(contract):
    if not re.fullmatch(r"[A-Z0-9]{2,28}_USDT",contract):
        raise ValueError("Invalid contract for Testnet leverage")
    if (os.getenv("GATE_TESTNET_LEVERAGE_AUTO_SETUP","false").lower()!="true" or
        os.getenv("LIVE_TRADING_ENABLED","false").lower()!="false" or
        os.getenv("ORDERS_ENABLED","false").lower()!="false"):
        raise RuntimeError("Testnet leverage settings are not authorized")
    key=os.getenv("GATE_TESTNET_API_KEY","").strip()
    secret=os.getenv("GATE_TESTNET_API_SECRET","").strip()
    if not key or not secret:
        raise RuntimeError("Gate Testnet credentials missing")
    path=ROOT+"/positions/"+contract+"/set_leverage"
    query="leverage=5&margin_mode=cross"
    headers=signed_headers("POST",path,query,b"",str(int(time.time())),key,secret)
    req=Request(TESTNET_HOST+path+"?"+query,data=b"",headers=headers,method="POST")
    with urlopen(req,timeout=8) as res:
        result=json.loads(res.read(65536))
    if not isinstance(result,(dict,list)):
        raise ValueError("Unverified Testnet leverage setup acknowledgment")
    return result

def prepare_contract_5x(contract,*,read=None,writer=None):
    """One GET-validated setting write at most. Never retries uncertain POST."""
    report={"contract":contract,"setting":"CROSS_LEVERAGE_5X","changed":False}
    if not isinstance(contract,str) or not re.fullmatch(r"[A-Z0-9]{2,28}_USDT",contract):
        return {**report,"state":"INVALID_TESTNET_CONTRACT"}
    if (os.getenv("GATE_TESTNET_LEVERAGE_AUTO_SETUP","false").lower()!="true"
        or os.getenv("GATE_TESTNET_PROTECTED_EXECUTION_ENABLED","false").lower()!="true"
        or os.getenv("LIVE_TRADING_ENABLED","false").lower()!="false"
        or os.getenv("ORDERS_ENABLED","false").lower()!="false"):
        return {**report,"state":"TESTNET_LEVERAGE_SETUP_DISABLED"}
    get=read or _request_testnet
    post=writer or _write_5x
    try:
        account=get("GET",ROOT+"/accounts")
        spec=get("GET",ROOT+"/contracts/"+contract)
        positions=get("GET",ROOT+"/positions")
        orders=get("GET",ROOT+"/orders",query="status=open")
        triggers=get("GET",ROOT+"/price_orders",query="status=open")
        if (not isinstance(account,dict) or account.get("currency")!="USDT"
            or account.get("in_dual_mode") is not True
            or not isinstance(spec,dict) or spec.get("name")!=contract
            or spec.get("in_delisting") is True):
            return {**report,"state":"ACCOUNT_OR_CONTRACT_UNVERIFIED"}
        if any(not isinstance(items,list) or not all(isinstance(x,dict) for x in items)
               for items in (positions,orders,triggers)):
            return {**report,"state":"FLAT_READBACK_INVALID"}
        for pos in positions:
            size=Decimal(str(pos["size"]))
            if not size.is_finite():
                raise ValueError("Invalid position size")
            if size!=0:
                return {**report,"state":"ACCOUNT_NOT_FLAT"}
        if orders or triggers:
            return {**report,"state":"ACCOUNT_HAS_OPEN_ORDERS_OR_TRIGGERS"}
        try:
            limit=Decimal(str(spec["leverage_max"]))
            if not limit.is_finite() or limit<Decimal("5"):
                return {**report,"state":"CONTRACT_CANNOT_SUPPORT_5X"}
        except (KeyError,ValueError,TypeError,InvalidOperation):
            return {**report,"state":"CONTRACT_LEVERAGE_LIMIT_UNVERIFIED"}
        long=_leverage(get,contract,"long")
        short=_leverage(get,contract,"short")
        if long<=Decimal("5") and short<=Decimal("5"):
            return {**report,"state":"LEVERAGE_WITHIN_5X_VERIFIED"}
        post(contract)   # exactly one Testnet-only settings POST; never retry
        long=_leverage(get,contract,"long")
        short=_leverage(get,contract,"short")
        if long!=Decimal("5") or short!=Decimal("5"):
            return {**report,"state":"LEVERAGE_UPDATE_READBACK_FAILED"}
        return {**report,"state":"LEVERAGE_5X_EXCHANGE_VERIFIED","changed":True}
    except Exception as exc:
        code=getattr(exc,"code",None)
        reason=("HTTP_"+str(code) if isinstance(code,int) else type(exc).__name__.upper())
        if not re.fullmatch(r"[A-Z0-9_]{1,40}",reason):
            reason="READBACK_UNAVAILABLE"
        return {**report,"state":"LEVERAGE_SETUP_"+reason}
