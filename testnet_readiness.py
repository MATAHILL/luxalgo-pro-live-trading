"""Read-only, exchange-backed Gate Futures TESTNET trial readiness snapshot.

No exchange writes. Automatically records safe diagnostic facts, never secrets.
"""
import os
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from gate_testnet_broker import _request_testnet, ROOT

CHECK_CONTRACT="ETH_USDT"

def inspect_testnet_readiness(*, request=None):
    request=request or _request_testnet
    blockers=[]
    result={"environment":"GATE_FUTURES_TESTNET","read_only":True,
            "checked_at":datetime.now(timezone.utc).isoformat(),
            "probe_contract":CHECK_CONTRACT,"order_submitted":False}
    if not os.getenv("GATE_TESTNET_API_KEY","").strip() or not os.getenv("GATE_TESTNET_API_SECRET","").strip():
        return {**result,"ready":False,"blockers":["TESTNET_API_CREDENTIALS_MISSING"]}
    try:
        account=request("GET",ROOT+"/accounts")
        if not isinstance(account,dict) or account.get("currency")!="USDT":
            raise ValueError("Unexpected testnet account response")
        result["account_connected"]=True
        result["dual_mode"]=account.get("in_dual_mode") is True
        if result["dual_mode"] is not True:
            blockers.append("HEDGE_DUAL_MODE_NOT_CONFIRMED")
        try:
            available=Decimal(str(account["available"]))
            if not available.is_finite() or available<=0:
                blockers.append("TESTNET_AVAILABLE_MARGIN_NOT_POSITIVE")
            result["available_margin_positive"]=available.is_finite() and available>0
        except (KeyError,ValueError,TypeError,InvalidOperation):
            blockers.append("TESTNET_AVAILABLE_MARGIN_UNVERIFIED")
        positions=request("GET",ROOT+"/positions")
        if not isinstance(positions,list) or not all(isinstance(p,dict) for p in positions):
            raise ValueError("Unparseable testnet positions")
        result["positions_readback"]=True
        result["positions_count"]=sum(1 for p in positions if _nonzero_position(p))
        if result["positions_count"]>=5:
            blockers.append("OPEN_TESTNET_POSITIONS_LIMIT")
        contract=request("GET",ROOT+"/contracts/"+CHECK_CONTRACT)
        if not isinstance(contract,dict) or contract.get("name")!=CHECK_CONTRACT or contract.get("in_delisting"):
            blockers.append("TESTNET_CONTRACT_NOT_ACTIVE")
        else:
            result["contract_readback"]=True
            try:
                multiplier=Decimal(str(contract["quanto_multiplier"]))
                size_min=Decimal(str(contract["order_size_min"]))
                size_max=Decimal(str(contract["order_size_max"]))
                mark=Decimal(str(contract["mark_price"]))
                levmax=Decimal(str(contract["leverage_max"]))
                if not all(x.is_finite() for x in (multiplier,size_min,size_max,mark,levmax)):
                    raise ValueError
                if multiplier<=0 or mark<=0 or size_min>1 or size_max<1 or levmax<5:
                    blockers.append("ONE_CONTRACT_TRIAL_INCOMPATIBLE")
            except (KeyError,ValueError,TypeError,InvalidOperation):
                blockers.append("TESTNET_CONTRACT_METADATA_INVALID")
        # Always verify configured leverage against exchange, not leverage_max.
        if result["dual_mode"]:
            response=request("GET",ROOT+"/get_leverage/"+CHECK_CONTRACT,
                      query="pos_margin_mode=cross&dual_side=dual_long")
            if not isinstance(response,dict):
                blockers.append("LEVERAGE_READBACK_INVALID")
            else:
                try:
                    configured=Decimal(str(response["Lever"]))
                    result["configured_leverage"]=str(configured)
                    if not configured.is_finite() or configured<=0 or configured>5:
                        blockers.append("TESTNET_LEVERAGE_NOT_WITHIN_5X")
                except (KeyError,ValueError,TypeError,InvalidOperation):
                    blockers.append("LEVERAGE_READBACK_INVALID")
        triggers=request("GET",ROOT+"/price_orders",query="status=open&contract="+CHECK_CONTRACT)
        if not isinstance(triggers,list):
            blockers.append("PROTECTION_READBACK_UNAVAILABLE")
        else:
            result["native_protection_readback"]=True
    except Exception as exc:
        blockers.append("READ_ONLY_API_UNAVAILABLE_"+type(exc).__name__.upper())
    return {**result,"ready":not blockers,"blockers":blockers,
            "production_trading_enabled":False}

def _nonzero_position(position):
    try:
        value=Decimal(str(position.get("size","0")))
        return not value.is_finite() or value!=0
    except (InvalidOperation,TypeError,ValueError):
        return True
