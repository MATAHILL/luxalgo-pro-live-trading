"""Explicit user-requested, exchange-verified Gate Futures TESTNET close.

No real-money host, no position opening, no automatic retries after network
writes. Does not replace the independent native protection watchdog.
"""
import os
import re
from decimal import Decimal,InvalidOperation
from testnet_protected_coordinator import TestnetIntent
from testnet_execution_journal import PostgresTestnetJournal
from gate_testnet_broker import GateFuturesTestnetBroker

def request_testnet_close(signal_id,contract,side,entry_order_id,*,broker=None,journal=None):
    if (os.getenv("LIVE_TRADING_ENABLED","false").lower()!="false"
        or os.getenv("ORDERS_ENABLED","false").lower()!="false"
        or os.getenv("GATE_TESTNET_RECOVERY_ENABLED","false").lower()!="true"
        or os.getenv("GATE_TESTNET_ONE_SHOT_PROTECTED_TRIAL","false").lower()=="true"):
        return {"state":"TESTNET_CLOSE_DISABLED","submitted":False}
    if (not isinstance(signal_id,str) or not re.fullmatch(r"[0-9A-Za-z_.-]{4,128}",signal_id)
        or not isinstance(contract,str) or not re.fullmatch(r"[A-Z0-9]{2,28}_USDT",contract)
        or side not in ("LONG","SHORT")
        or not re.fullmatch(r"[0-9]{1,20}",str(entry_order_id))):
        raise ValueError("Invalid testnet position identity")
    ledger=journal or PostgresTestnetJournal()
    # Claim first. A double-click or concurrent watchdog cannot send a second close.
    claimed=ledger.claim_manual_close(signal_id,contract,side,str(entry_order_id))
    if not claimed:
        return {"state":"POSITION_NOT_CLAIMABLE","submitted":False}
    sent=False
    try:
        payload=claimed["payload"]
        if (not isinstance(payload,dict) or payload.get("contract")!=contract
            or payload.get("size")!=(1 if side=="LONG" else -1)
            or payload.get("text")!=claimed["client_id"]):
            raise ValueError("Testnet journal identity mismatch")
        gw=broker or GateFuturesTestnetBroker()
        gw._contract=contract
        order=gw.order(str(entry_order_id))
        if (not isinstance(order,dict) or str(order.get("id"))!=str(entry_order_id)
            or order.get("contract")!=contract or order.get("finish_as")!="filled"
            or order.get("text")!=claimed["client_id"]):
            raise ValueError("Original exchange entry not verified")
        positions=gw.positions()
        if not isinstance(positions,list) or not all(isinstance(p,dict) for p in positions):
            raise ValueError("Incomplete testnet position snapshot")
        mode="dual_long" if side=="LONG" else "dual_short"
        active=[]
        for p in positions:
            if p.get("contract")!=contract or p.get("mode")!=mode:
                continue
            n=Decimal(str(p["size"]))
            if not n.is_finite(): raise ValueError("Invalid position size")
            if n>0: active.append(p)
        if len(active)!=1:
            raise ValueError("Position is not uniquely open")
        intent=TestnetIntent(signal_id,contract,side,str(order.get("fill_price") or "0"),
            str(payload["tpsl_sl_trigger_price"]),str(payload["tpsl_tp_trigger_price"]),
            claimed["client_id"])
        sent=True
        closed=gw.emergency_close(intent)   # One TESTNET reduce-only IOC, verified.
        if closed is not True:
            ledger.transition(signal_id,"HALTED_UNKNOWN",
                              {"reason":"USER_CLOSE_FILL_OR_FLAT_NOT_VERIFIED"})
            return {"state":"HALTED_UNKNOWN","submitted":True}
        # A successful close must not leave order-attached triggers orphaned.
        triggers=gw.native_triggers()
        if not isinstance(triggers,list) or not all(isinstance(t,dict) for t in triggers):
            raise ValueError("Trigger cleanup readback unavailable")
        if any(t.get("status")=="open" and str(t.get("me_order_id") or "")==str(entry_order_id)
               for t in triggers):
            ledger.transition(signal_id,"HALTED_UNKNOWN",
                              {"reason":"TESTNET_FLAT_BUT_ORPHAN_TRIGGER_PRESENT"})
            return {"state":"HALTED_UNKNOWN","submitted":True}
        ledger.transition(signal_id,"CLOSED_VERIFIED",
                          {"reason":"USER_REQUESTED_REDUCE_ONLY_TESTNET_CLOSE",
                           "entry_order_id":str(entry_order_id)})
        return {"state":"CLOSED_VERIFIED","submitted":True}
    except Exception:
        # After ambiguous exchange IO do not retry. Recovery may inspect readbacks.
        try:
            ledger.transition(signal_id,"HALTED_UNKNOWN",
                              {"reason":"USER_CLOSE_UNCERTAIN_NO_RETRY"})
        except Exception:
            pass
        return {"state":"HALTED_UNKNOWN","submitted":sent}
