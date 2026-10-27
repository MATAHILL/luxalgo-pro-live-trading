"""STAGING-ONLY production execution coordinator.

No exchange credentials, live sender, or Flask route is provided.
This module can operate with an injected broker in offline tests only.
Production use remains blocked until independent authorization and full
exchange-side fill, SL/TP, recovery, and account-risk verification.
"""
import os
from live_execution_safety import LiveLedger, ProposedRisk, validate_risk

def _switches_safe():
    return (os.getenv("LIVE_TRADING_ENABLED","false").lower()=="true"
        and os.getenv("ORDERS_ENABLED","false").lower()=="true"
        and os.getenv("PRODUCTION_EXECUTION_REVIEWED","false").lower()=="true")

def attempt_guarded_live_execution(plan, broker, journal):
    """One reservation, at most one entry attempt, fail closed on unknown fills.

    broker methods: preflight(plan), submit(plan,client_id), order(order_id),
    positions(), native_triggers(), verify_protection(...), emergency_close(plan).
    None is implemented by this module. This is not a live sender.
    """
    if not _switches_safe():
        return {"state":"DISABLED_NO_REAL_MONEY","order_submitted":False}
    if not isinstance(plan,ProposedRisk) or broker is None or journal is None:
        raise ValueError("Verified risk plan and exchange adapter required")
    validate_risk(plan)
    reservation=journal.reserve(plan)
    if reservation.get("accepted") is not True:
        return {"state":reservation.get("reason","RESERVATION_BLOCKED"),
                "order_submitted":False}
    cid=reservation["client_id"]
    submitted=False
    state="RESERVED"
    try:
        if broker.preflight(plan,cid) is not True:
            journal.transition(plan.event_id,"REJECTED",{"reason":"EXCHANGE_PREFLIGHT_FAILED"})
            return {"state":"REJECTED","order_submitted":False}
        # The unknown state is durable BEFORE network IO, to forbid a blind retry.
        journal.transition(plan.event_id,"SUBMISSION_UNKNOWN",{"client_id":cid})
        state="SUBMISSION_UNKNOWN"
        submitted=True
        ack=broker.submit(plan,cid)
        if not isinstance(ack,dict) or not str(ack.get("id","")).isdigit():
            journal.transition(plan.event_id,"HALTED_UNKNOWN",{"reason":"AMBIGUOUS_ORDER_ACK"})
            return {"state":"HALTED_UNKNOWN","order_submitted":"UNKNOWN"}
        oid=str(ack["id"])
        journal.transition(plan.event_id,"ORDER_ACK",{"entry_order_id":oid})
        state="ORDER_ACK"
        order=broker.order(oid)
        if (not isinstance(order,dict)
            or str(order.get("id"))!=oid or order.get("contract")!=plan.contract
            or order.get("status")!="finished" or order.get("finish_as")!="filled"):
            journal.transition(plan.event_id,"HALTED_UNKNOWN",{"reason":"FILL_NOT_VERIFIED"})
            return {"state":"HALTED_UNKNOWN","order_submitted":True}
        positions=broker.positions()
        triggers=broker.native_triggers(plan.contract)
        evidence=broker.verify_protection(order,positions,triggers,plan,cid)
        if (not isinstance(evidence,dict) or evidence.get("verified") is not True
            or evidence.get("entry_order_id")!=oid
            or not str(evidence.get("stop_order_id","")).isdigit()
            or not str(evidence.get("take_order_id","")).isdigit()
            or str(evidence["stop_order_id"])==str(evidence["take_order_id"])):
            journal.transition(plan.event_id,"UNPROTECTED",{"reason":"PROTECTION_NOT_VERIFIED"})
            if broker.emergency_close(plan) is True:
                journal.transition(plan.event_id,"CLOSED_VERIFIED",{"reason":"VERIFIED_EMERGENCY_FLAT"})
                return {"state":"CLOSED_VERIFIED","order_submitted":True}
            return {"state":"UNPROTECTED","order_submitted":True}
        proof={"entry_order_id":oid,
               "stop_order_id":str(evidence["stop_order_id"]),
               "take_order_id":str(evidence["take_order_id"]),
               "entry_fill_status":"filled"}
        journal.transition(plan.event_id,"PROTECTED_VERIFIED",proof)
        return {"state":"PROTECTED_VERIFIED","order_submitted":True,
                "entry_order_id":oid}
    except Exception:
        # Do not ever replay after an unknown POST, including timeouts.
        if state in ("SUBMISSION_UNKNOWN","ORDER_ACK"):
            try:
                journal.transition(plan.event_id,"HALTED_UNKNOWN",
                                   {"reason":"INDETERMINATE_EXCHANGE_READBACK_NO_RETRY"})
            except Exception:
                pass
        raise
