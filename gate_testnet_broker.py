"""Gate Futures TESTNET-only broker for protected-order coordinator.

No webhook integration. Exchange writes require a fresh verified preflight,
a persistent journal from the coordinator, and an explicit TESTNET flag.
"""
import hashlib
import hmac
import json
import os
import re
import time
from decimal import Decimal, InvalidOperation
from urllib.request import Request, urlopen
from gate_testnet_execution import (
    TESTNET_HOST, ORDER_PATH, signed_headers,
    preflight_attached_testnet_entry, verify_testnet_protection,
    verify_attached_tpsl_readback, atomic_protected_entry_payload, TestnetExecutionDisabled,
)

ROOT="/api/v4/futures/usdt"


def _request_testnet(method, path, *, body=None, query=""):
    """Testnet-pinned transport; never retries writes or accesses production."""
    if method=="GET":
        permitted=(path in (ROOT+"/accounts",ROOT+"/positions",ROOT+"/price_orders",ORDER_PATH)
                   or bool(re.fullmatch(r"/api/v4/futures/usdt/contracts/[A-Z0-9]{2,28}_USDT",path))
                   or bool(re.fullmatch(r"/api/v4/futures/usdt/orders/[0-9]{1,20}",path))
                   or bool(re.fullmatch(r"/api/v4/futures/usdt/orders/t-[A-Za-z0-9_.-]{1,28}",path))
                   or bool(re.fullmatch(r"/api/v4/futures/usdt/get_leverage/[A-Z0-9]{2,28}_USDT",path)))
        if not permitted or body is not None:
            raise ValueError("Disallowed Testnet read")
        trigger_query=(path==ROOT+"/price_orders" and
            (query=="status=open" or re.fullmatch(r"status=open&contract=[A-Z0-9]{2,28}_USDT",query)))
        leverage_query=(path.startswith(ROOT+"/get_leverage/") and
            re.fullmatch(r"pos_margin_mode=cross&dual_side=dual_(long|short)",query))
        orders_query=(path==ORDER_PATH and query=="status=open")
        if query and not (trigger_query or leverage_query or orders_query):
            raise ValueError("Invalid Testnet read query")
    elif method=="POST":
        if path!=ORDER_PATH or query or not isinstance(body,dict):
            raise ValueError("Disallowed Testnet write")
        is_emergency=(body.get("size")==0 and body.get("reduce_only") is True
                      and body.get("auto_size") in ("close_long","close_short"))
        is_trial=(os.getenv("GATE_TESTNET_ONE_SHOT_PROTECTED_TRIAL","false").lower()=="true"
                  and os.getenv("GATE_TESTNET_WEBHOOK_AUTOMATION_ENABLED","false").lower()!="true"
                  and body.get("contract")=="ETH_USDT" and body.get("size")==1
                  and body.get("text")=="t-lx-trial-20261027-a")
        is_webhook=(os.getenv("GATE_TESTNET_WEBHOOK_AUTOMATION_ENABLED","false").lower()=="true")
        recovery=(os.getenv("GATE_TESTNET_RECOVERY_ENABLED","false").lower()=="true")
        entry_enabled=(os.getenv("GATE_TESTNET_PROTECTED_EXECUTION_ENABLED","false").lower()=="true")
        if (is_emergency and not recovery) or (not is_emergency and not
            (entry_enabled and (is_trial or is_webhook))):
            raise TestnetExecutionDisabled("Gate Testnet write permission not armed for this operation")
        if (body.get("tif")!="ioc" or str(body.get("price"))!="0"
            or body.get("size") not in (-1,1)
            or not body.get("tpsl_tp_trigger_price") or not body.get("tpsl_sl_trigger_price")):
            if body.get("size")==0 and body.get("reduce_only") is True and body.get("auto_size") in ("close_long","close_short"):
                pass  # Testnet emergency close only; caller must have a journaled open position.
            else:
                raise TestnetExecutionDisabled("Protected entry or reduce-only Testnet close required")
    else:
        raise ValueError("Method prohibited")
    key=os.getenv("GATE_TESTNET_API_KEY","").strip()
    secret=os.getenv("GATE_TESTNET_API_SECRET","").strip()
    if not key or not secret:
        raise TestnetExecutionDisabled("Testnet credentials missing")
    data=json.dumps(body,separators=(",",":"),allow_nan=False).encode() if method=="POST" else b""
    headers=signed_headers(method,path,query,data,str(int(time.time())),key,secret)
    req=Request(TESTNET_HOST+path+("?"+query if query else ""),
                data=data if method=="POST" else None,headers=headers,method=method)
    with urlopen(req,timeout=8) as response:
        result=json.loads(response.read(150000))
    if not isinstance(result,(list,dict)):
        raise ValueError("Unrecognized Testnet response")
    return result


class GateFuturesTestnetBroker:
    """Concrete isolated adapter injected into execute_protected_testnet."""
    def __init__(self, request=None):
        self.request=request or _request_testnet
        self._approved=None
        self._approved_at=0
        self._contract=None

    def positions(self):
        result=self.request("GET",ROOT+"/positions")
        if not isinstance(result,list):
            raise ValueError("Invalid Testnet positions")
        return result

    def native_triggers(self):
        if not self._contract:
            raise ValueError("Unknown protected contract")
        result=self.request("GET",ROOT+"/price_orders",
                            query="status=open&contract="+self._contract)
        if not isinstance(result,list):
            raise ValueError("Invalid Testnet native triggers")
        return result

    def preflight(self,intent,payload):
        self._approved=None
        if os.getenv("GATE_TESTNET_PROTECTED_EXECUTION_ENABLED","false").lower()!="true":
            return False
        account=self.request("GET",ROOT+"/accounts")
        spec=self.request("GET",ROOT+"/contracts/"+intent.contract)
        positions=self.positions()
        leverage_data=self.request("GET",ROOT+"/get_leverage/"+intent.contract,
            query="pos_margin_mode=cross&dual_side=dual_"+intent.side.lower())
        try:
            actual_leverage=Decimal(str(leverage_data["Lever"]))
            if not actual_leverage.is_finite() or actual_leverage<=0 or actual_leverage>5:
                return False
        except (KeyError,ValueError,TypeError,InvalidOperation):
            return False
        if not isinstance(account,dict) or account.get("currency")!="USDT" or account.get("in_dual_mode") is not True:
            return False
        check=preflight_attached_testnet_entry(payload,spec,positions,account,max_open_positions=5)
        if not check["approved"]:
            return False
        try:
            mark=Decimal(str(spec["mark_price"]))
            reference=Decimal(str(intent.reference))
            stop=Decimal(str(intent.stop))
            multiplier=Decimal(str(spec["quanto_multiplier"]))
            available=Decimal(str(account["available"]))
            if (not all(v.is_finite() for v in (mark,reference,stop,multiplier,available))
                or min(mark,reference,stop,multiplier,available)<=0):
                return False
            if abs(mark-reference)/mark>Decimal("0.005"):
                return False
            if abs(mark-stop)*multiplier>available*Decimal("0.005"):
                return False  # 0.5% Testnet risk ceiling; no live financial order.
        except (KeyError,ValueError,InvalidOperation,TypeError):
            return False
        try:
            required=atomic_protected_entry_payload(intent.contract,intent.side,
                intent.reference,intent.stop,intent.target,intent.client_id)
        except (ValueError,TypeError):
            return False
        if payload!=required:
            return False
        self._contract=intent.contract
        self._approved=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
        self._approved_at=time.monotonic()
        return True

    def submit(self,payload):
        fingerprint=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
        if not self._approved or fingerprint!=self._approved or time.monotonic()-self._approved_at>5:
            raise TestnetExecutionDisabled("Fresh matching exchange preflight required")
        # Consume one-use capability before any network IO; no blind retry.
        self._approved=None
        response=self.request("POST",ORDER_PATH,body=payload)
        if not isinstance(response,dict) or not response.get("id"):
            raise RuntimeError("Ambiguous Testnet order response, manual reconciliation required")
        return response

    def lookup_by_client_id(self,client_id):
        if not isinstance(client_id,str) or not re.fullmatch(r"t-[A-Za-z0-9_.-]{1,28}",client_id):
            raise ValueError("Invalid Testnet client ID")
        result=self.request("GET",ORDER_PATH+"/"+client_id)
        if not isinstance(result,dict) or result.get("text")!=client_id:
            raise ValueError("Uncertain Testnet order lookup")
        return result

    def order(self,order_id):
        if not re.fullmatch(r"[0-9]{1,20}",str(order_id)):
            raise ValueError("Unverified numeric order ID")
        result=self.request("GET",ORDER_PATH+"/"+str(order_id))
        if not isinstance(result,dict):
            raise ValueError("Invalid order readback")
        return result

    def verify_protection(self,order,positions,triggers,intent,payload):
        # Evidence belongs to this exact exchange readback, never a previous order.
        self.last_protection_evidence=None
        attached=verify_attached_tpsl_readback(order,payload)
        linked=verify_testnet_protection(order,positions,triggers,
                     expected_contract=intent.contract,expected_side=intent.side,
                     stop_price=intent.stop,target_price=intent.target)
        verified=(attached.get("verified") is True and linked.get("verified") is True)
        if verified:
            self.last_protection_evidence={
                "entry_id":str(order["id"]),
                "entry_status":str(order["status"]),
                "entry_finish_as":str(order["finish_as"]),
                "native_protection_ids":linked["protective_order_ids"],
            }
        return verified

    def emergency_close(self,intent):
        """Testnet-only reduce-only close; verify flat, otherwise halt."""
        close_side="close_long" if intent.side=="LONG" else "close_short"
        try:
            order={"contract":intent.contract,"size":0,"price":"0","tif":"ioc",
                   "auto_size":close_side,"reduce_only":True,
                   "text":"t-ec-"+hashlib.sha256(intent.client_id.encode()).hexdigest()[:20]}
            ack=self.request("POST",ORDER_PATH,body=order)
            if not isinstance(ack,dict) or not ack.get("id"):
                return False
            # A close-order acknowledgment alone is not evidence of a fill.
            close_order=self.order(str(ack["id"]))
            if (str(close_order.get("id"))!=str(ack["id"])
                or close_order.get("contract")!=intent.contract
                or str(close_order.get("finish_as","")).lower()!="filled"):
                return False
            positions=self.positions()
            side="dual_long" if intent.side=="LONG" else "dual_short"
            relevant=[p for p in positions if p.get("contract")==intent.contract and p.get("mode")==side]
            # An empty/missing position readback cannot prove the account is flat.
            return bool(relevant) and all(Decimal(str(p["size"]))==0 for p in relevant)
        except (ValueError,KeyError,TypeError,InvalidOperation,TimeoutError,OSError):
            return False
