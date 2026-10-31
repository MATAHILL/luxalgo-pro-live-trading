"""One-shot emergency close claim and conservative precheck, independent of sender.

A persisted claim is REQUIRED before any later real emergency reduce-only POST.
This module has no exchange-writing calls. It does not clear HALT or launch
a trade, and must undergo separate database/restart/live recovery certification
before anyone can use it as evidence for a production release.
"""
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from live_futures_engine import LiveSafetyError, CONTRACT_RE
from live_execution_coordinator import LOCK_ID

CLAIM_SCHEMA = """
CREATE TABLE IF NOT EXISTS luxalgo_prod_emergency_close_claims (
 event_id TEXT PRIMARY KEY REFERENCES luxalgo_prod_execution_ledger(event_id),
 contract TEXT NOT NULL,
 approval_reference TEXT NOT NULL,
 close_client_text TEXT NOT NULL UNIQUE,
 claimed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 state TEXT NOT NULL DEFAULT 'CLAIMED'
 CHECK (state IN ('CLAIMED','EXCHANGE_RESULT_UNKNOWN','FLAT_READBACK_VERIFIED'))
);
"""

def evaluate_one_shot_close_candidate(ledger, snapshot, now=None):
    """Never certify uncertain or orphaned exchange exposure as safely closeable."""
    try:
        if not isinstance(ledger, dict) or not isinstance(snapshot, dict):
            raise LiveSafetyError("MISSING_EXCHANGE_OR_JOURNAL")
        now = now or datetime.now(timezone.utc)
        if not isinstance(now,datetime) or now.tzinfo is None:
            raise LiveSafetyError("UNVERIFIED_CLOCK")
        updated=snapshot.get("readback_utc")
        if not isinstance(updated,datetime) or updated.tzinfo is None:
            raise LiveSafetyError("MISSING_FRESH_EXCHANGE_TIMESTAMP")
        drift=now.astimezone(timezone.utc)-updated.astimezone(timezone.utc)
        if drift < timedelta(0) or drift > timedelta(seconds=30):
            raise LiveSafetyError("STALE_EXCHANGE_INVENTORY")
        if snapshot.get("source")!="GATE_IO_LIVE_FUTURES":
            raise LiveSafetyError("UNAUTHENTICATED_EXCHANGE_SOURCE")
        if snapshot.get("halt_active") is not True:
            raise LiveSafetyError("DURABLE_EMERGENCY_HALT_MUST_ALREADY_BE_ACTIVE")
        if ledger.get("state") not in ("ENTRY_CONFIRMED","PROTECTION_PENDING","HALTED"):
            raise LiveSafetyError("INELIGIBLE_LEDGER_STATE")
        contract=ledger.get("contract")
        if not isinstance(contract,str) or not CONTRACT_RE.fullmatch(contract):
            raise LiveSafetyError("INVALID_CONTRACT")
        if ledger.get("side") not in ("LONG","SHORT"):
            raise LiveSafetyError("INVALID_LEDGER_DIRECTION")
        if ledger.get("position_mode") not in ("single","dual"):
            raise LiveSafetyError("UNKNOWN_HEDGE_MODE")
        if snapshot.get("position_mode")!=ledger["position_mode"]:
            raise LiveSafetyError("EXCHANGE_POSITION_MODE_CHANGED")
        if not all(isinstance(snapshot.get(key),list) for key in ("positions","orders","triggers")):
            raise LiveSafetyError("INCOMPLETE_EXCHANGE_INVENTORY")
        if snapshot["orders"] or snapshot["triggers"]:
            raise LiveSafetyError("OPEN_ORDERS_OR_ORPHAN_TRIGGERS_REQUIRE_MANUAL_REVIEW")
        positions=snapshot["positions"]
        if len(positions)!=1 or not isinstance(positions[0],dict):
            raise LiveSafetyError("UNKNOWN_MULTIPLE_OR_FLAT_EXCHANGE_POSITIONS")
        p=positions[0]
        if p.get("contract")!=contract:
            raise LiveSafetyError("EXCHANGE_POSITION_CONTRACT_MISMATCH")
        try:
            actual=Decimal(str(p.get("size")))
            expected=Decimal(str(ledger.get("contracts")))
        except (InvalidOperation,TypeError,ValueError) as exc:
            raise LiveSafetyError("UNKNOWN_POSITION_SIZE") from exc
        if (not actual.is_finite() or not expected.is_finite()
                or expected<=0 or actual!=(expected if ledger["side"]=="LONG" else -expected)):
            raise LiveSafetyError("EXCHANGE_POSITION_NOT_EXACT_LEDGER_SIZE")
        if not isinstance(ledger.get("event_id"),str) or not ledger["event_id"]:
            raise LiveSafetyError("MISSING_EVENT_IDENTIFIER")
        return {"eligible_for_atomic_claim":True,"orders_sent":0,
                "contract":contract,"event_id":ledger["event_id"],
                "reason":"REQUIRES_DURABLE_ONE_SHOT_CLAIM_AND_SEPARATE_AUTHORIZATION"}
    except LiveSafetyError as exc:
        return {"eligible_for_atomic_claim":False,"orders_sent":0,
                "reason":str(exc)}


def claim_one_shot_recovery(con, *, event_id, contract, approval_reference, close_text):
    """Atomically persist one close claim; a retry can never insert a second.

    Requires dedicated, transaction-specific owner approval, a durable HALT,
    and a known non-closed ledger row. The caller must still independently
    validate exact live exchange inventory *before* and *after* claiming.
    Claims never send orders or certify actual close results.
    """
    if (not isinstance(event_id,str) or not event_id
            or not isinstance(contract,str) or not CONTRACT_RE.fullmatch(contract)
            or not isinstance(approval_reference,str) or len(approval_reference)<8
            or not isinstance(close_text,str) or not close_text.startswith("t-em-")
            or len(close_text)>30):
        raise LiveSafetyError("MISSING_EXACT_EMERGENCY_APPROVAL_OR_IDENTITY")
    con.execute(CLAIM_SCHEMA)
    with con.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)",(LOCK_ID,))
        cur.execute("SELECT halted FROM luxalgo_prod_execution_control WHERE id=1 FOR UPDATE")
        gate=cur.fetchone()
        if not gate or gate["halted"] is not True:
            raise LiveSafetyError("EMERGENCY_HALT_NOT_DURABLY_CONFIRMED")
        cur.execute("""SELECT event_id,contract,state FROM luxalgo_prod_execution_ledger
                       WHERE event_id=%s FOR UPDATE""",(event_id,))
        row=cur.fetchone()
        if (not row or row["contract"]!=contract or
                row["state"] not in ("ENTRY_CONFIRMED","PROTECTION_PENDING","HALTED")):
            raise LiveSafetyError("EMERGENCY_CLOSE_LEDGER_IDENTITY_NOT_VERIFIED")
        cur.execute("""INSERT INTO luxalgo_prod_emergency_close_claims
                       (event_id,contract,approval_reference,close_client_text)
                       VALUES (%s,%s,%s,%s)
                       ON CONFLICT DO NOTHING RETURNING event_id""",
                    (event_id,contract,approval_reference,close_text))
        claimed=cur.fetchone()
        if not claimed or claimed["event_id"]!=event_id:
            raise LiveSafetyError("ONE_SHOT_CLAIM_ALREADY_USED_OR_CONFLICTED")
    return {"claimed":True,"orders_sent":0,"event_id":event_id,
            "contract":contract,"close_client_text":close_text,
            "state":"CLAIMED","approval_reference":approval_reference}


def consume_one_shot_recovery_claim(open_connection, claim):
    """Atomically consume a claim once BEFORE any possible exchange POST.

    The initial CLAIMED row is never enough by itself to dispatch. A
    transactionally committed state transition to EXCHANGE_RESULT_UNKNOWN
    ensures a lost acknowledgment or process restart cannot POST a retry.
    """
    if not isinstance(claim,dict) or claim.get("claimed") is not True:
        raise LiveSafetyError("NO_DURABLE_CLOSE_CLAIM")
    with open_connection() as con:
        with con.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)",(LOCK_ID,))
            cur.execute("SELECT halted FROM luxalgo_prod_execution_control WHERE id=1 FOR UPDATE")
            control=cur.fetchone()
            if not control or control.get("halted") is not True:
                raise LiveSafetyError("EMERGENCY_HALT_NOT_CONFIRMED_BEFORE_CLOSE")
            cur.execute("""UPDATE luxalgo_prod_emergency_close_claims
                           SET state='EXCHANGE_RESULT_UNKNOWN'
                           WHERE event_id=%s AND contract=%s
                             AND close_client_text=%s
                             AND approval_reference=%s
                             AND state='CLAIMED'
                           RETURNING event_id""",(
                           claim.get("event_id"),claim.get("contract"),
                           claim.get("close_client_text"),
                           claim.get("approval_reference")))
            claimed=cur.fetchone()
            if not claimed or claimed.get("event_id")!=claim.get("event_id"):
                raise LiveSafetyError("CLOSE_CLAIM_CONSUMED_OR_INVALID_NO_RETRY")
    return True


def finalize_one_shot_recovery_claim(open_connection,claim):
    """Only after exchange order readback AND flat/open-order trigger checks."""
    with open_connection() as con:
        with con.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)",(LOCK_ID,))
            cur.execute("""UPDATE luxalgo_prod_emergency_close_claims
                           SET state='FLAT_READBACK_VERIFIED'
                           WHERE event_id=%s AND contract=%s
                             AND close_client_text=%s
                             AND state='EXCHANGE_RESULT_UNKNOWN'
                           RETURNING event_id""",(
                           claim.get("event_id"),claim.get("contract"),
                           claim.get("close_client_text")))
            row=cur.fetchone()
            if not row or row.get("event_id")!=claim.get("event_id"):
                raise LiveSafetyError("CLOSE_VERIFICATION_NOT_PERSISTED")
    return True
