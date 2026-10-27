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
        """Read-only stepwise Testnet preflight; diagnose without relaxing limits."""
        self._approved=None
        self.last_preflight_reason="UNVERIFIED"
        self.last_preflight_stage="START"
        def failed(reason):
            self.last_preflight_reason=reason
            return False
        if os.getenv("GATE_TESTNET_PROTECTED_EXECUTION_ENABLED","false").lower()!="true":
            return failed("TESTNET_ENTRY_PERMISSION_OFF")
        try:
            self.last_preflight_stage="ACCOUNT"
            account=self.request("GET",ROOT+"/accounts")
            self.last_preflight_stage="CONTRACT"
            spec=self.request("GET",ROOT+"/contracts/"+intent.contract)
            self.last_preflight_stage="POSITIONS"
            positions=self.positions()
            self.last_preflight_stage="LEVERAGE"
            leverage_data=self.request("GET",ROOT+"/get_leverage/"+intent.contract,
                query="pos_margin_mode=cross&dual_side=dual_"+intent.side.lower())
        except Exception as exc:
            # Expose only the check name and HTTP status/type, never API keys,
            # request authorization headers, response body or credentials.
            code=getattr(exc,"code",None)
            reason=("HTTP_"+str(code) if isinstance(code,int) else type(exc).__name__.upper())
            return failed(self.last_preflight_stage+"_"+reason)
        try:
            actual_leverage=Decimal(str(leverage_data["Lever"]))
            if not actual_leverage.is_finite() or actual_leverage<=0:
                return failed("LEVERAGE_READBACK_INVALID")
            if actual_leverage>Decimal("5"):
                # This optional setting action is isolated to Testnet and
                # requires an independently verified, entirely flat account.
                # It never submits a position and never retries an unknown POST.
                from testnet_contract_leverage import prepare_contract_5x
                setup=prepare_contract_5x(intent.contract,read=self.request)
                if setup.get("state") not in (
                        "LEVERAGE_WITHIN_5X_VERIFIED","LEVERAGE_5X_EXCHANGE_VERIFIED"):
                    return failed(setup.get("state","LEVERAGE_SETUP_UNVERIFIED"))
                self.last_preflight_stage="LEVERAGE_READBACK_AFTER_SETUP"
                leverage_data=self.request("GET",ROOT+"/get_leverage/"+intent.contract,
                    query="pos_margin_mode=cross&dual_side=dual_"+intent.side.lower())
                actual_leverage=Decimal(str(leverage_data["Lever"]))
                if not actual_leverage.is_finite() or actual_leverage<=0 or actual_leverage>5:
                    return failed("LEVERAGE_STILL_EXCEEDS_TESTNET_MAX_5X")
                self.last_preflight_stage="ACCOUNT_AFTER_LEVERAGE"
                account=self.request("GET",ROOT+"/accounts")
                positions=self.positions()
        except (KeyError,ValueError,TypeError,InvalidOperation):
            return failed("LEVERAGE_READBACK_INVALID")
        except Exception as exc:
            code=getattr(exc,"code",None)
            reason=("HTTP_"+str(code) if isinstance(code,int) else type(exc).__name__.upper())
            if not re.fullmatch(r"[A-Z0-9_]{1,40}",reason):
                reason="UNVERIFIED"
            return failed("LEVERAGE_SETUP_"+reason)
        if not isinstance(account,dict) or account.get("currency")!="USDT" or account.get("in_dual_mode") is not True:
            return failed("ACCOUNT_USDT_HEDGE_MODE_UNVERIFIED")
        check=preflight_attached_testnet_entry(payload,spec,positions,account,max_open_positions=5)
        if not check["approved"]:
            return failed("CONTRACT_PREFLIGHT_"+",".join(check.get("reasons",["UNVERIFIED"])))
        try:
            mark=Decimal(str(spec["mark_price"]))
            reference=Decimal(str(intent.reference))
            stop=Decimal(str(intent.stop))
            multiplier=Decimal(str(spec["quanto_multiplier"]))
            available=Decimal(str(account["available"]))
            if (not all(v.is_finite() for v in (mark,reference,stop,multiplier,available))
                or min(mark,reference,stop,multiplier,available)<=0):
                return failed("MARK_RISK_METADATA_INVALID")
            if abs(mark-reference)/mark>Decimal("0.005"):
                return failed("REFERENCE_PRICE_OUTSIDE_0_5_PERCENT_MARK_BAND")
            if abs(mark-stop)*multiplier>available*Decimal("0.005"):
                return failed("STOP_RISK_EXCEEDS_0_5_PERCENT_MARGIN")
        except (KeyError,ValueError,InvalidOperation,TypeError):
            return failed("MARK_RISK_METADATA_INVALID")
        try:
            required=atomic_protected_entry_payload(intent.contract,intent.side,
                intent.reference,intent.stop,intent.target,intent.client_id)
        except (ValueError,TypeError):
            return failed("PROTECTED_TPSL_GEOMETRY_INVALID")
        if payload!=required:
            return failed("PROTECTED_ORDER_PAYLOAD_MISMATCH")
        self._contract=intent.contract
        self._approved=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
        self._approved_at=time.monotonic()
        self.last_preflight_reason="PASS"
        self.last_preflight_stage="COMPLETE"
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
        if not verified:
            # The reason is a bounded enum from validated exchange readbacks.
            reason=(attached.get("reason") if attached.get("verified") is not True
                    else linked.get("reason"))
            value=str(reason or "UNVERIFIED")
            self.last_protection_reason=(value if re.fullmatch(r"[A-Z0-9_]{2,90}",value)
                                         else "UNVERIFIED")
        else:
            self.last_protection_reason="NATIVE_TPSL_VERIFIED"
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
                or str(close_order.get("status","")).lower()!="finished"
                or str(close_order.get("finish_as","")).lower()!="filled"):
                return False
            # Gate may omit fully closed positions entirely. Treat that as
            # flat only with TWO valid complete position-list readbacks after
            # the uniquely filled reduce-only close; never rely on an empty
            # partial/invalid response or the POST acknowledgment.
            side="dual_long" if intent.side=="LONG" else "dual_short"
            for _ in range(2):
                positions=self.positions()
                if not isinstance(positions,list) or not all(isinstance(p,dict) for p in positions):
                    return False
                for p in positions:
                    if p.get("contract")!=intent.contract or p.get("mode")!=side:
                        continue
                    qty=Decimal(str(p["size"]))
                    if not qty.is_finite() or qty!=0:
                        return False
            return True
        except (ValueError,KeyError,TypeError,InvalidOperation,TimeoutError,OSError):
            return False
