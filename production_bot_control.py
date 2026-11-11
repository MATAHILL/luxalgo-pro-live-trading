"""Operator-facing production BOT ON/OFF; never reuses archived Testnet state.

No exchange writes. Dashboard ON cannot lift persistent HALT or certify
trading. All safety gates must already be satisfied by separately verified,
explicitly authorized acceptance before the operator can enable new entries.
"""
from __future__ import annotations
import os
import re
from datetime import datetime, timezone
from live_futures_engine import EXCHANGE_WRITE_RELEASE
from live_execution_orchestrator import AUTOMATIC_WORKER_RELEASE
from live_execution_coordinator import halt, LOCK_ID, control, install as install_ledger

SCHEMA = """
CREATE TABLE IF NOT EXISTS luxalgo_prod_bot_operator_control (
 id SMALLINT PRIMARY KEY CHECK(id=1),
 operator_on BOOLEAN NOT NULL DEFAULT FALSE,
 reason TEXT NOT NULL DEFAULT 'NOT_CERTIFIED',
 changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO luxalgo_prod_bot_operator_control(id,operator_on,reason)
VALUES (1,FALSE,'NOT_CERTIFIED') ON CONFLICT(id) DO NOTHING;
CREATE TABLE IF NOT EXISTS luxalgo_prod_activation_certification (
 id SMALLINT PRIMARY KEY CHECK(id=1),
 exchange TEXT NOT NULL,
 acceptance_verified BOOLEAN NOT NULL DEFAULT FALSE,
 final_operator_authorized BOOLEAN NOT NULL DEFAULT FALSE,
 approved_until TIMESTAMPTZ NOT NULL,
 evidence_sha TEXT NOT NULL,
 signed_approval_id TEXT NOT NULL
);
"""

def install(con):
    install_ledger(con)
    con.execute(SCHEMA)

def _read_row(con, sql):
    with con.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()

def state(con, *, install_schema=True):
    if install_schema:
        install(con)
    op=_read_row(con,"SELECT operator_on,reason,changed_at FROM luxalgo_prod_bot_operator_control WHERE id=1")
    protected=control(con)
    cert=_read_row(con,"SELECT exchange,acceptance_verified,final_operator_authorized,approved_until,evidence_sha,signed_approval_id FROM luxalgo_prod_activation_certification WHERE id=1")
    if op is None:
        raise RuntimeError("Production operator control unavailable")
    now=datetime.now(timezone.utc)
    certified=(bool(cert) and cert["exchange"]=="GATE_IO_LIVE_USDT_FUTURES" and
               cert["acceptance_verified"] is True and
               cert["final_operator_authorized"] is True and
               bool(cert["evidence_sha"]) and bool(cert["signed_approval_id"]) and
               cert["approved_until"]>now)
    source_released=EXCHANGE_WRITE_RELEASE is True and AUTOMATIC_WORKER_RELEASE is True
    env_released=(all(os.getenv(k,"false")=="true" for k in
                 ("LIVE_TRADING_ENABLED","ORDERS_ENABLED","LIVE_ORDERS_SEPARATELY_AUTHORIZED")))
    db_halt=protected["halted"] is not False
    gates=[]
    if not source_released:gates.append("SOURCE_LOCKED")
    if not env_released:gates.append("ENVIRONMENT_LOCKED")
    if db_halt:gates.append("EMERGENCY_HALT")
    if not certified:gates.append("ACCEPTANCE_AND_FINAL_AUTHORIZATION_MISSING")
    if not op["operator_on"]:gates.append("OPERATOR_OFF")
    effective=not gates
    from live_risk_policy import policy_status
    # Surface WHY the halt is latched and until when the certificate runs.
    # Without these the operator sees only the gate NAME and has to guess
    # whether the halt came from a button press, a reconciliation anomaly or
    # an unreachable exchange. Read-only disclosure: no gate is relaxed.
    halt_reason=(protected.get("reason") or "") if db_halt else None
    certified_until=(cert["approved_until"].isoformat()
                     if certified and cert.get("approved_until") else None)
    return dict(risk_policy=policy_status(), bot_on=effective,operator_requested_on=bool(op["operator_on"]),
                can_arm=(source_released and env_released and not db_halt and certified),
                orders_enabled=effective, exchange="GATE_IO_LIVE_USDT_FUTURES",
                safety_gates=gates, halt_active=db_halt,certified=certified,
                halt_reason=halt_reason,
                operator_reason=op["reason"] or None,
                certified_until=certified_until,
                reason="ARMED" if effective else ", ".join(gates),
                changed_at=op["changed_at"].isoformat() if op["changed_at"] else None,
                monitoring_independent=True)

def turn_off(con):
    """Atomic operator OFF and persistent HALT. Never cancels native SL/TP."""
    install(con)
    with con.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)",(LOCK_ID,))
        cur.execute("""UPDATE luxalgo_prod_bot_operator_control
            SET operator_on=FALSE,reason='OPERATOR_OFF',changed_at=NOW() WHERE id=1""")
        if cur.rowcount!=1:
            raise RuntimeError("Operator OFF persistence failed")
    halt(con,"OPERATOR_OFF_NO_NEW_ENTRIES")
    return state(con,install_schema=False)

def turn_on(con):
    """Fails closed until source, environment, HALT and acceptance all ready.

    This endpoint NEVER enables source/env flags and NEVER clears HALT.
    """
    install(con)
    with con.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)",(LOCK_ID,))
        before=state(con,install_schema=False)
        if not before["can_arm"]:
            return before
        cur.execute("""UPDATE luxalgo_prod_bot_operator_control
            SET operator_on=TRUE,reason='OPERATOR_ON',changed_at=NOW() WHERE id=1""")
        if cur.rowcount!=1:raise RuntimeError("Operator ON persistence failed")
    return state(con,install_schema=False)

def certify(con, *, evidence_sha, signed_approval_id, valid_hours=24):
    """Record the operator's final acceptance authorization.

    This is the missing write path for the certification row that state()
    requires. It records an explicit, time-bounded owner authorization after a
    verified acceptance trade. It does NOT itself send an order, clear the
    HALT, or enable source/env flags; those remain independent gates.
    """
    install(con)
    if not isinstance(evidence_sha,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{8,128}',evidence_sha):
        raise RuntimeError("A valid evidence reference is required")
    if not isinstance(signed_approval_id,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{4,128}',signed_approval_id):
        raise RuntimeError("A valid signed approval id is required")
    if type(valid_hours) is not int or not 1 <= valid_hours <= 168:
        raise RuntimeError("Validity window must be 1-168 hours")
    with con.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)",(LOCK_ID,))
        cur.execute("""INSERT INTO luxalgo_prod_activation_certification
            (id,exchange,acceptance_verified,final_operator_authorized,approved_until,evidence_sha,signed_approval_id)
            VALUES (1,'GATE_IO_LIVE_USDT_FUTURES',TRUE,TRUE,NOW() + (%s || ' hours')::interval,%s,%s)
            ON CONFLICT (id) DO UPDATE SET
              exchange=EXCLUDED.exchange,
              acceptance_verified=EXCLUDED.acceptance_verified,
              final_operator_authorized=EXCLUDED.final_operator_authorized,
              approved_until=EXCLUDED.approved_until,
              evidence_sha=EXCLUDED.evidence_sha,
              signed_approval_id=EXCLUDED.signed_approval_id""",
            (str(valid_hours),evidence_sha,signed_approval_id))
        if cur.rowcount!=1:
            raise RuntimeError("Certification persistence failed")
    return state(con,install_schema=False)

def revoke_certification(con):
    """Immediately invalidate the acceptance certification (fail closed)."""
    install(con)
    with con.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)",(LOCK_ID,))
        cur.execute("""UPDATE luxalgo_prod_activation_certification
            SET acceptance_verified=FALSE,final_operator_authorized=FALSE,
                approved_until=NOW() WHERE id=1""")
    return state(con,install_schema=False)

def require_operator_on(con):
    """Entry dispatch gate; a stored ON request is not sufficient."""
    value=state(con)
    if not value["bot_on"]:
        raise RuntimeError("PRODUCTION_BOT_OFF_OR_UNCERTIFIED")
    return True
