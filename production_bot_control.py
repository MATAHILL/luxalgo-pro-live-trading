"""Operator-facing production BOT ON/OFF; never reuses archived Testnet state.

No exchange writes. Dashboard ON cannot lift persistent HALT or certify
trading. All safety gates must already be satisfied by separately verified,
explicitly authorized acceptance before the operator can enable new entries.
"""
from __future__ import annotations
import os
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
    return dict(bot_on=effective,operator_requested_on=bool(op["operator_on"]),
                can_arm=(source_released and env_released and not db_halt and certified),
                orders_enabled=effective, exchange="GATE_IO_LIVE_USDT_FUTURES",
                safety_gates=gates, halt_active=db_halt,certified=certified,
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

def require_operator_on(con):
    """Entry dispatch gate; a stored ON request is not sufficient."""
    value=state(con)
    if not value["bot_on"]:
        raise RuntimeError("PRODUCTION_BOT_OFF_OR_UNCERTIFIED")
    return True
