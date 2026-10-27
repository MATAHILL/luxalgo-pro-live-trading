"""Isolated Testnet protected-entry coordinator. Never invoked by production webhook.

Inject exchange adapter and durable journal. No live/production credentials.
Entry is permitted only if a durable reservation and broker verification exist.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import time
import re
from gate_testnet_execution import atomic_protected_entry_payload, TestnetExecutionDisabled


@dataclass(frozen=True)
class TestnetIntent:
    signal_id: str
    contract: str
    side: str
    reference: str
    stop: str
    target: str
    client_id: str


def execute_protected_testnet(intent, broker, journal):
    """Submit once with attached TP/SL; reconcile, or halt and emergency-close.

    broker: preflight, submit, order, positions, native_triggers, emergency_close
    journal: reserve, transition. Both MUST be durable/exchange-backed.
    No automatic retry on unknown write outcome.
    """
    if not isinstance(intent, TestnetIntent) or not intent.signal_id:
        raise ValueError("Invalid Testnet intent")
    payload=atomic_protected_entry_payload(intent.contract,intent.side,intent.reference,
                                            intent.stop,intent.target,intent.client_id)
    if not journal.reserve(intent.signal_id, payload):
        return {"state":"DUPLICATE_BLOCKED", "submitted":False}
    try:
        check=broker.preflight(intent, payload)
        if check is not True:
            journal.transition(intent.signal_id,"BLOCKED",{"reason":"PREFLIGHT_NOT_VERIFIED"})
            return {"state":"BLOCKED","submitted":False}
        journal.transition(intent.signal_id,"SUBMISSION_UNKNOWN",{"client_id":intent.client_id})
        # Mark unknown BEFORE network write so a timeout cannot trigger a duplicate order.
        ack=broker.submit(payload)
        if not isinstance(ack,dict) or not ack.get("id"):
            journal.transition(intent.signal_id,"HALTED_UNKNOWN",{"reason":"NO_ORDER_ID"})
            return {"state":"HALTED_UNKNOWN","submitted":True}
        order_id=str(ack["id"])
        journal.transition(intent.signal_id,"ENTRY_ACK",{"order_id":order_id})
        order=broker.order(order_id)
        # A successful API response is not proof of a fill.
        if not isinstance(order,dict) or str(order.get("id"))!=order_id:
            journal.transition(intent.signal_id,"HALTED_UNKNOWN",{"reason":"ORDER_READBACK_UNCERTAIN"})
            return {"state":"HALTED_UNKNOWN","submitted":True}
        if order.get("contract")!=intent.contract:
            journal.transition(intent.signal_id,"HALTED_UNKNOWN",{"reason":"CONTRACT_MISMATCH"})
            return {"state":"HALTED_UNKNOWN","submitted":True}
        if str(order.get("finish_as","")).lower()!="filled":
            journal.transition(intent.signal_id,"HALTED_UNKNOWN",{"reason":"FILL_NOT_VERIFIED"})
            return {"state":"HALTED_UNKNOWN","submitted":True}
        # Gate may publish attached native triggers just AFTER an IOC fill.
        # Retry authenticated GET only, with a hard short bound; NEVER resend
        # the entry. If coverage is still unverified, reduce-only close.
        coverage=False
        for check_index in range(3):
            if check_index:
                time.sleep(0.65)
            position=broker.positions()
            triggers=broker.native_triggers()
            coverage=broker.verify_protection(order,position,triggers,intent,payload)
            if coverage is True:
                break
        if coverage is not True:
            why=str(getattr(broker,"last_protection_reason","UNVERIFIED") or "UNVERIFIED")
            if not re.fullmatch(r"[A-Z0-9_]{2,90}",why):
                why="UNVERIFIED"
            journal.transition(intent.signal_id,"EMERGENCY_CLOSE_REQUIRED",
                               {"order_id":order_id,
                                "reason":"NATIVE_TPSL_NOT_VERIFIED_AFTER_THREE_READBACKS",
                                "protection_readback_reason":why})
            # Broker adapter must use reduce-only/close side on Testnet and verify flat.
            flat=broker.emergency_close(intent)
            if flat is True:
                # A filled reduce-only close does not prove attached native
                # triggers were canceled. Recheck them before releasing slot.
                remaining=broker.native_triggers()
                if (not isinstance(remaining,list) or
                    not all(isinstance(t,dict) for t in remaining)):
                    journal.transition(intent.signal_id,"HALTED_UNPROTECTED",
                        {"order_id":order_id,"reason":"ORPHAN_TPSL_READBACK_UNAVAILABLE"})
                    return {"state":"HALTED_UNPROTECTED","submitted":True}
                orphans=[t for t in remaining if t.get("status")=="open" and
                         str(t.get("me_order_id") or "")==order_id]
                if orphans:
                    journal.transition(intent.signal_id,"HALTED_UNPROTECTED",
                        {"order_id":order_id,"reason":"ORPHAN_NATIVE_TPSL_AFTER_CLOSE"})
                    return {"state":"HALTED_UNPROTECTED","submitted":True}
                journal.transition(intent.signal_id,"EMERGENCY_CLOSED",
                                   {"order_id":order_id,"reason":"FLAT_WITHOUT_LINKED_OPEN_TPSL"})
                return {"state":"EMERGENCY_CLOSED","submitted":True}
            journal.transition(intent.signal_id,"HALTED_UNPROTECTED",{"order_id":order_id})
            return {"state":"HALTED_UNPROTECTED","submitted":True}
        journal.transition(intent.signal_id,"PROTECTED_VERIFIED",{"order_id":order_id})
        return {"state":"PROTECTED_VERIFIED","submitted":True,"order_id":order_id}
    except Exception:
        # IMPORTANT: do not assume exception means order was never accepted.
        # An independent recovery service must reconcile UNKNOWN states.
        journal.transition(intent.signal_id,"HALTED_UNKNOWN",{"reason":"EXECUTION_EXCEPTION"})
        raise
