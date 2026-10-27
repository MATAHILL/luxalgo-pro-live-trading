"""Durable, TESTNET-ONLY per-signal worker outcomes for dashboard diagnostics.

No exchange access, order dispatch, secret values, or historical replays.
"""
import os
import re
import psycopg
from psycopg.rows import dict_row

DDL="""CREATE TABLE IF NOT EXISTS luxalgo_testnet_execution_attempts(
 event_id TEXT PRIMARY KEY,
 state TEXT NOT NULL,
 detail TEXT NOT NULL DEFAULT '',
 submitted TEXT NOT NULL DEFAULT 'false',
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)"""
SAFE_STATE=re.compile(r"^[A-Z0-9_]{2,96}$")
SAFE_DETAIL=re.compile(r"^[A-Z0-9_,.:-]{0,200}$")

def record_testnet_attempt(event_id,result,*,database_url=None):
    if not isinstance(event_id,str) or not re.fullmatch(r"[a-f0-9]{20}",event_id):
        raise ValueError("Invalid signal identity")
    if not isinstance(result,dict):
        raise ValueError("Result mapping required")
    state=str(result.get("state","UNKNOWN")).upper()
    detail=str(result.get("detail","")).upper()
    if not SAFE_STATE.fullmatch(state) or not SAFE_DETAIL.fullmatch(detail):
        raise ValueError("Unsafe execution audit description")
    submitted=str(result.get("submitted",False)).lower()
    if submitted not in ("true","false","unknown"):
        submitted="unknown"
    url=database_url or os.getenv("DATABASE_URL","")
    if not url:raise RuntimeError("Testnet audit database unavailable")
    with psycopg.connect(url,connect_timeout=5,row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
            cur.execute("""INSERT INTO luxalgo_testnet_execution_attempts
                (event_id,state,detail,submitted)
                VALUES(%s,%s,%s,%s)
                ON CONFLICT(event_id) DO UPDATE SET
                 state=EXCLUDED.state, detail=EXCLUDED.detail,
                 submitted=EXCLUDED.submitted, updated_at=now()
                WHERE luxalgo_testnet_execution_attempts.state NOT IN
                 ('PROTECTED_VERIFIED','EMERGENCY_CLOSED','CLOSED_VERIFIED')
                AND (EXCLUDED.state <> 'WORKER_QUEUED'
                     OR luxalgo_testnet_execution_attempts.state = 'WORKER_QUEUED')""",
                (event_id,state,detail,submitted))
    return True
