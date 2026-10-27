"""Recover TESTNET positions following a crash, without ever replaying entries.

Read back original custom client ID, verify native linked TP/SL,
then halt or reduce-only emergency close for a confirmed exposed position.
"""
import os
from decimal import Decimal, InvalidOperation
from testnet_automation import testnet_automation_armed
from testnet_execution_journal import PostgresTestnetJournal
from gate_testnet_broker import GateFuturesTestnetBroker
from testnet_protected_coordinator import TestnetIntent
from testnet_protection_watchdog import _exposed


def recover_testnet_once(*, broker_factory=None, journal=None, limit=3):
    if not testnet_automation_armed():
        return {"state":"TESTNET_DISABLED","checked":0}
    ledger=journal or PostgresTestnetJournal()
    factory=broker_factory or GateFuturesTestnetBroker
    candidates=ledger.recovery_candidates(limit=limit)
    outcomes=[]
    for item in candidates:
        event_id=item["signal_id"]
        claim=ledger.claim_recovery(event_id)
        if not claim:
            continue
        gateway=factory()
        payload=claim["payload"]
        side=claim["side"]
        contract=claim["contract"]
        client_id=claim["client_id"]
        try:
            if (not isinstance(payload,dict) or payload.get("contract")!=contract or
                payload.get("text")!=client_id or
                payload.get("size")!=(1 if side=="LONG" else -1)):
                ledger.transition(event_id,"HALTED_UNKNOWN",{"reason":"JOURNAL_PAYLOAD_MISMATCH"})
                outcomes.append("HALTED_UNKNOWN")
                continue
            # No retry: the only possible first action is GET by original client_id.
            order=gateway.lookup_by_client_id(client_id)
            if str(order.get("text") or "")!=client_id or order.get("contract")!=contract:
                raise ValueError("Mismatched exchange recovery readback")
            if type(order.get("size")) is not int:
                size=int(order["size"])
            else:
                size=order["size"]
            if size!=payload["size"] or not order.get("id"):
                raise ValueError("Mismatched recovery order size or ID")
            if order.get("status")!="finished" or order.get("finish_as")!="filled":
                # IOC orders may be partially filled. Do not mark flat or retry;
                # require subsequent reconciliation or manual intervention.
                ledger.transition(event_id,"HALTED_UNKNOWN",{"reason":"FILL_NOT_VERIFIED_NO_RETRY"})
                outcomes.append("HALTED_UNKNOWN")
                continue
            intent=TestnetIntent(event_id,contract,side,
                str((Decimal(str(payload["tpsl_sl_trigger_price"]))+
                     Decimal(str(payload["tpsl_tp_trigger_price"])))/2),
                str(payload["tpsl_sl_trigger_price"]),
                str(payload["tpsl_tp_trigger_price"]),client_id)
            gateway._contract=contract
            positions=gateway.positions()
            triggers=gateway.native_triggers()
            if not isinstance(triggers,list) or not all(isinstance(x,dict) for x in triggers):
                raise ValueError("Unverified native trigger readback")
            if not _exposed(positions,contract,side):
                orphan=any(str(t.get("me_order_id") or "")==str(order["id"]) and
                           t.get("status")=="open" for t in triggers)
                if orphan:
                    ledger.transition(event_id,"HALTED_UNKNOWN",{
                        "reason":"FLAT_POSITION_WITH_ORPHAN_NATIVE_TPSL"})
                    outcomes.append("HALTED_UNKNOWN")
                else:
                    ledger.transition(event_id,"CLOSED_VERIFIED",{
                        "reason":"POSITION_CONFIRMED_FLAT_DURING_RECOVERY"})
                    outcomes.append("CLOSED_VERIFIED")
                continue
            if gateway.verify_protection(order,positions,triggers,intent,payload):
                ledger.transition(event_id,"PROTECTED_VERIFIED",{"order_id":str(order["id"]),
                                  "reason":"RECOVERED_WITH_LINKED_NATIVE_TPSL"})
                outcomes.append("PROTECTED_VERIFIED")
            else:
                ledger.transition(event_id,"EMERGENCY_CLOSE_REQUIRED",{"order_id":str(order["id"])})
                flat=gateway.emergency_close(intent)
                if flat is True:
                    ledger.transition(event_id,"EMERGENCY_CLOSED",{"order_id":str(order["id"])})
                    outcomes.append("EMERGENCY_CLOSED")
                else:
                    ledger.transition(event_id,"HALTED_UNPROTECTED",{"order_id":str(order["id"])})
                    outcomes.append("HALTED_UNPROTECTED")
        except Exception:
            # Never resend an entry, even if lookup returns 404 or times out.
            try:
                ledger.transition(event_id,"HALTED_UNKNOWN",{"reason":"RECOVERY_READBACK_UNCERTAIN"})
            except Exception:
                pass
            outcomes.append("HALTED_UNKNOWN")
    return {"state":"READBACK_COMPLETE","checked":len(outcomes),"outcomes":outcomes}
