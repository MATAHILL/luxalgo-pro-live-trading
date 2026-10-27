"""Gate.io Futures TESTNET protected-position watchdog (no entry submission).

An exchange fill with verified native TP/SL can lose protection later.
This watchdog re-checks only journaled, actually verified Testnet positions,
with PostgreSQL per-position leases to avoid concurrent audits.
"""
from decimal import Decimal, InvalidOperation
from testnet_automation import testnet_recovery_armed
from testnet_execution_journal import PostgresTestnetJournal
from testnet_protected_coordinator import TestnetIntent
from gate_testnet_broker import GateFuturesTestnetBroker


def _exposed(positions,contract,side):
    """True only after a complete, valid position snapshot."""
    if not isinstance(positions,list) or not all(isinstance(p,dict) for p in positions):
        raise ValueError("INVALID_POSITION_READBACK")
    mode="dual_long" if side=="LONG" else "dual_short"
    if side not in ("LONG","SHORT"):
        raise ValueError("INVALID_POSITION_SIDE")
    exposed=False
    for p in positions:
        if p.get("contract")!=contract or p.get("mode")!=mode:
            continue
        try:
            qty=Decimal(str(p["size"]))
            if not qty.is_finite():
                raise ValueError
        except (KeyError,ValueError,InvalidOperation,TypeError):
            raise ValueError("POSITION_SIZE_UNVERIFIED") from None
        if qty!=0:
            exposed=True
    return exposed


def audit_protected_testnet_once(*,broker_factory=None,journal=None,limit=3):
    """Monitor native stop and target throughout the trade, no fresh entry.

    If protection is gone while exposure remains, attempt ONE Testnet-only
    reduce-only emergency close and quarantine failures without blind retries.
    """
    if not testnet_recovery_armed():
        return {"state":"TESTNET_DISABLED","checked":0}
    ledger=journal or PostgresTestnetJournal()
    factory=broker_factory or GateFuturesTestnetBroker
    outcome=[]
    for item in ledger.protected_audit_candidates(limit=limit):
        claimed=ledger.claim_protected_audit(item["signal_id"])
        if not claimed:
            continue
        eid=claimed["signal_id"]
        payload=claimed["payload"]
        details=claimed["details"]
        contract=claimed["contract"]
        side=claimed["side"]
        try:
            if (not isinstance(payload,dict) or not isinstance(details,dict)
                or side not in ("LONG","SHORT")
                or payload.get("contract")!=contract
                or payload.get("size")!=(1 if side=="LONG" else -1)):
                raise ValueError("INVALID_JOURNAL_PAYLOAD")
            oid=str(details.get("order_id") or "")
            if not oid.isdigit() or not oid:
                raise ValueError("MISSING_VERIFIED_ORDER_ID")
            gateway=factory()
            gateway._contract=contract
            positions=gateway.positions()
            triggers=gateway.native_triggers()
            if not isinstance(triggers,list) or not all(isinstance(t,dict) for t in triggers):
                raise ValueError("INVALID_TRIGGER_READBACK")
            exposed=_exposed(positions,contract,side)
            if not exposed:
                # A flat position is not evidence of healthy TP/SL if orphan
                # child triggers still exist. Keep it quarantined for review.
                orphan=any(str(t.get("me_order_id") or "")==oid and
                           t.get("status")=="open" for t in triggers)
                state="HALTED_UNKNOWN" if orphan else "CLOSED_VERIFIED"
                ledger.transition(eid,state,{"reason":"ORPHAN_TPSL" if orphan else "POSITION_FLAT_ON_TESTNET"})
                outcome.append(state)
                continue
            order=gateway.order(oid)
            intent=TestnetIntent(eid,contract,side,
                str((Decimal(str(payload["tpsl_tp_trigger_price"]))+
                     Decimal(str(payload["tpsl_sl_trigger_price"])))/2),
                str(payload["tpsl_sl_trigger_price"]),
                str(payload["tpsl_tp_trigger_price"]),claimed["client_id"])
            if gateway.verify_protection(order,positions,triggers,intent,payload):
                facts={"reason":"NATIVE_TPSL_RECHECK_PASSED","order_id":oid}
                evidence=getattr(gateway,"last_protection_evidence",None)
                if isinstance(evidence,dict):
                    ids=evidence.get("native_protection_ids")
                    if (evidence.get("entry_id")==oid
                        and evidence.get("entry_finish_as")=="filled"
                        and isinstance(ids,list) and len(ids)==2
                        and len(set(str(x) for x in ids))==2):
                        facts["native_protection_ids"]=list(str(x) for x in ids)
                        facts["entry_finish_as"]="filled"
                ledger.transition(eid,"PROTECTED_VERIFIED",facts)
                outcome.append("PROTECTED_VERIFIED")
            else:
                ledger.transition(eid,"EMERGENCY_CLOSE_REQUIRED",{
                    "reason":"NATIVE_PROTECTION_LOST_DURING_TRADE","order_id":oid})
                closed=gateway.emergency_close(intent)
                state="EMERGENCY_CLOSED" if closed is True else "HALTED_UNPROTECTED"
                ledger.transition(eid,state,{"order_id":oid})
                outcome.append(state)
        except Exception:
            # Unknown readback is NOT a verified close. Do not blindly resubmit
            # either the entry or an emergency close.
            try:
                ledger.transition(eid,"HALTED_UNKNOWN",{"reason":"PROTECTION_AUDIT_UNCERTAIN"})
            except Exception:
                pass
            outcome.append("HALTED_UNKNOWN")
    return {"state":"AUDIT_COMPLETE","checked":len(outcome),"outcomes":outcome}
