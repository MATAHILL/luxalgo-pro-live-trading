"""Persistent, fail-closed Gate Futures TESTNET execution journal.

An isolated PostgreSQL journal. Never starts exchange orders or production trading.
Transactions serialize reservations across Render workers with an advisory xact lock.
"""
import json
import os
import re
import psycopg
from psycopg.rows import dict_row

LOCK_ID=667420215
OPEN_STATES=("RESERVED","SUBMISSION_UNKNOWN","ENTRY_ACK","HALTED_UNKNOWN",
             "EMERGENCY_CLOSE_REQUIRED","HALTED_UNPROTECTED","PROTECTED_VERIFIED","RECOVERING")
STATES=set(OPEN_STATES) | {"BLOCKED","DUPLICATE_BLOCKED","EMERGENCY_CLOSED","CLOSED_VERIFIED"}
ALLOWED_TRANSITIONS={
    "RESERVED":{"BLOCKED","SUBMISSION_UNKNOWN","HALTED_UNKNOWN"},
    "SUBMISSION_UNKNOWN":{"ENTRY_ACK","HALTED_UNKNOWN","EMERGENCY_CLOSE_REQUIRED","RECOVERING"},
    "ENTRY_ACK":{"PROTECTED_VERIFIED","HALTED_UNKNOWN","EMERGENCY_CLOSE_REQUIRED","RECOVERING"},
    "HALTED_UNKNOWN":{"ENTRY_ACK","EMERGENCY_CLOSE_REQUIRED","HALTED_UNPROTECTED","RECOVERING"},
    "EMERGENCY_CLOSE_REQUIRED":{"EMERGENCY_CLOSED","HALTED_UNPROTECTED","HALTED_UNKNOWN","RECOVERING"},
    "HALTED_UNPROTECTED":{"EMERGENCY_CLOSED","RECOVERING"},
    "RECOVERING":{"HALTED_UNKNOWN","EMERGENCY_CLOSE_REQUIRED","PROTECTED_VERIFIED","CLOSED_VERIFIED"},

    "PROTECTED_VERIFIED":{"RECOVERING","CLOSED_VERIFIED","HALTED_UNPROTECTED"},
    "BLOCKED":set(),"EMERGENCY_CLOSED":set(),"CLOSED_VERIFIED":set(),
}
# ACTIVE verified positions keep a slot until an exchange reconciliation retires them.
DDL="""
CREATE TABLE IF NOT EXISTS luxalgo_testnet_protected_journal (
    signal_id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL UNIQUE,
    contract TEXT NOT NULL,
    side TEXT NOT NULL,
    state TEXT NOT NULL,
    payload JSONB NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_testnet_protected_state
  ON luxalgo_testnet_protected_journal(state,updated_at);
"""


class PostgresTestnetJournal:
    def __init__(self, database_url=None, *, max_slots=5):
        self.url=database_url if database_url is not None else os.getenv("DATABASE_URL","")
        if not self.url:
            raise RuntimeError("PostgreSQL DATABASE_URL missing")
        if type(max_slots) is not int or not 1<=max_slots<=5:
            raise ValueError("Invalid maximum Testnet positions")
        self.max_slots=max_slots

    def _connect(self):
        return psycopg.connect(self.url,connect_timeout=5,row_factory=dict_row)

    def reserve(self, signal_id, payload):
        """Reserve a single client ID and a concurrency slot atomically.
        A duplicate/uncertain client ID is never retried.
        """
        if not isinstance(signal_id,str) or not re.fullmatch(r"[0-9A-Za-z_.-]{4,128}",signal_id):
            raise ValueError("Invalid signal ID")
        if not isinstance(payload,dict):
            raise ValueError("Missing protected Testnet payload")
        contract=payload.get("contract")
        cid=payload.get("text")
        size=payload.get("size")
        if (not isinstance(contract,str) or not re.fullmatch(r"[A-Z0-9]{2,28}_USDT",contract)
            or not isinstance(cid,str) or not re.fullmatch(r"t-[A-Za-z0-9_.-]{1,28}",cid)
            or type(size) is not int or size not in (-1,1)
            or not payload.get("tpsl_tp_trigger_price")
            or not payload.get("tpsl_sl_trigger_price")):
            raise ValueError("Protected intent invalid")
        side="LONG" if size==1 else "SHORT"
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(DDL)
                cur.execute("SELECT pg_advisory_xact_lock(%s)",(LOCK_ID,))
                cur.execute("SELECT state FROM luxalgo_testnet_protected_journal WHERE signal_id=%s OR client_id=%s",
                            (signal_id,cid))
                if cur.fetchone() is not None:
                    return False
                cur.execute("""SELECT COUNT(*) AS n FROM luxalgo_testnet_protected_journal
                            WHERE state = ANY(%s)""",(list(OPEN_STATES),))
                if int(cur.fetchone()["n"])>=self.max_slots:
                    return False
                cur.execute("""SELECT 1 FROM luxalgo_testnet_protected_journal
                               WHERE contract=%s AND state = ANY(%s) LIMIT 1""",
                            (contract,list(OPEN_STATES)))
                if cur.fetchone() is not None:
                    return False
                cur.execute("""INSERT INTO luxalgo_testnet_protected_journal
                    (signal_id,client_id,contract,side,state,payload)
                    VALUES (%s,%s,%s,%s,'RESERVED',%s::jsonb)""",
                    (signal_id,cid,contract,side,json.dumps(payload,allow_nan=False)))
                return True

    def transition(self, signal_id, state, details):
        if state not in STATES:
            raise ValueError("Unknown Testnet journal state")
        if not isinstance(details,dict):
            raise ValueError("Journal metadata must be a mapping")
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT state FROM luxalgo_testnet_protected_journal WHERE signal_id=%s FOR UPDATE",
                            (signal_id,))
                existing=cur.fetchone()
                if not existing:
                    raise RuntimeError("Unreserved Testnet signal cannot transition")
                prior=existing["state"]
                if state not in ALLOWED_TRANSITIONS.get(prior,set()):
                    raise RuntimeError("Invalid Testnet journal transition: "+prior+" to "+state)
                cur.execute("""UPDATE luxalgo_testnet_protected_journal
                    SET state=%s, details=COALESCE(details,'{}'::jsonb)||%s::jsonb,
                        updated_at=NOW() WHERE signal_id=%s""",
                    (state,json.dumps(details,allow_nan=False),signal_id))

    def claim_manual_close(self, signal_id, contract, side, order_id):
        """Atomically claim a verified position for one user-requested Testnet close.

        No order is sent here; the RECOVERING state prevents duplicate clicks
        and protects against concurrent watchdog or bot actions.
        """
        if (not isinstance(signal_id,str)
            or not re.fullmatch(r"[0-9A-Za-z_.-]{4,128}",signal_id)
            or not isinstance(contract,str)
            or not re.fullmatch(r"[A-Z0-9]{2,28}_USDT",contract)
            or side not in ("LONG","SHORT")
            or not re.fullmatch(r"[0-9]{1,20}",str(order_id))):
            raise ValueError("Invalid Testnet position identity")
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""UPDATE luxalgo_testnet_protected_journal
                  SET state='RECOVERING',updated_at=NOW()
                  WHERE signal_id=%s AND contract=%s AND side=%s
                    AND state='PROTECTED_VERIFIED'
                    AND details->>'order_id'=%s
                  RETURNING signal_id,client_id,contract,side,payload,details,state""",
                  (signal_id,contract,side,str(order_id)))
                return cur.fetchone()

    def claim_recovery(self, signal_id):
        """Atomically claim an uncertain order for readback, never re-submit entry.

        A 2-minute stale claim may be reclaimed after a dead worker restarts.
        """
        if not isinstance(signal_id,str) or not re.fullmatch(r"[0-9A-Za-z_.-]{4,128}",signal_id):
            raise ValueError("Invalid recovery signal ID")
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""UPDATE luxalgo_testnet_protected_journal
                  SET state='RECOVERING', updated_at=NOW()
                  WHERE signal_id=%s AND (
                    state IN ('SUBMISSION_UNKNOWN','ENTRY_ACK','HALTED_UNKNOWN',
                              'EMERGENCY_CLOSE_REQUIRED')
                    OR (state='RECOVERING' AND updated_at < NOW()-INTERVAL '120 seconds')
                  ) RETURNING signal_id,client_id,contract,side,payload,state,details""",
                  (signal_id,))
                return cur.fetchone()

    def protected_audit_candidates(self, limit=5):
        """Return due protected positions for a read-only exchange-side audit."""
        if type(limit) is not int or not 1<=limit<=5:
            raise ValueError("Invalid audit limit")
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT signal_id,client_id,contract,side,state,payload,details,updated_at
                  FROM luxalgo_testnet_protected_journal
                  WHERE state='PROTECTED_VERIFIED'
                    AND updated_at < NOW()-INTERVAL '30 seconds'
                  ORDER BY updated_at LIMIT %s""",(limit,))
                return list(cur.fetchall())

    def claim_protected_audit(self, signal_id):
        """Acquire an atomic lease before checking an already filled position."""
        if not isinstance(signal_id,str) or not re.fullmatch(r"[0-9A-Za-z_.-]{4,128}",signal_id):
            raise ValueError("Invalid audit signal ID")
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""UPDATE luxalgo_testnet_protected_journal
                  SET state='RECOVERING',updated_at=NOW()
                  WHERE signal_id=%s AND state='PROTECTED_VERIFIED'
                    AND updated_at < NOW()-INTERVAL '30 seconds'
                  RETURNING signal_id,client_id,contract,side,payload,details,state""",
                  (signal_id,))
                return cur.fetchone()

    def recovery_candidates(self, limit=20):
        """Read-only candidates, never replays orders."""
        if type(limit) is not int or not 1<=limit<=100:
            raise ValueError("Invalid recovery limit")
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT signal_id,client_id,contract,side,state,payload,details,updated_at
                  FROM luxalgo_testnet_protected_journal
                  WHERE (state IN ('SUBMISSION_UNKNOWN','ENTRY_ACK','HALTED_UNKNOWN',
                                   'EMERGENCY_CLOSE_REQUIRED')
                        AND updated_at < NOW()-INTERVAL '10 seconds')
                     OR (state='RECOVERING' AND updated_at < NOW()-INTERVAL '120 seconds')
                  ORDER BY updated_at LIMIT %s""",(limit,))
                return list(cur.fetchall())
