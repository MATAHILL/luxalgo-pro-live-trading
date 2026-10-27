"""Production execution ledger STAGING ONLY.

No exchange transport and no order-sending route exists in this module.
It must not be deployed as an authorization to trade. Real exchange order
dispatch requires a separately reviewed sender and exchange-side readback.
"""
import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import psycopg
from psycopg.rows import dict_row

LOCK_KEY=92813412
MAX_POSITIONS=5
MAX_LEVERAGE=Decimal("5")
PER_TRADE_RISK=Decimal("0.005")
TOTAL_RISK=Decimal("0.025")
ACTIVE=("RESERVED","SUBMISSION_UNKNOWN","ORDER_ACK","RECOVERING",
        "PROTECTED_VERIFIED","HALTED_UNKNOWN","UNPROTECTED")
TRANSITIONS={
    "RESERVED":{"SUBMISSION_UNKNOWN","REJECTED"},
    "SUBMISSION_UNKNOWN":{"ORDER_ACK","HALTED_UNKNOWN","RECOVERING"},
    "ORDER_ACK":{"PROTECTED_VERIFIED","HALTED_UNKNOWN","UNPROTECTED","RECOVERING"},
    "PROTECTED_VERIFIED":{"RECOVERING","UNPROTECTED","CLOSED_VERIFIED"},
    "RECOVERING":{"PROTECTED_VERIFIED","UNPROTECTED","HALTED_UNKNOWN","CLOSED_VERIFIED"},
    "HALTED_UNKNOWN":{"RECOVERING","UNPROTECTED"},
    "UNPROTECTED":{"RECOVERING","CLOSED_VERIFIED"},
    "REJECTED":set(),"CLOSED_VERIFIED":set(),
}
DDL="""
CREATE TABLE IF NOT EXISTS luxalgo_production_execution_ledger(
  event_id TEXT PRIMARY KEY,
  client_id TEXT NOT NULL UNIQUE,
  contract TEXT NOT NULL,
  side TEXT NOT NULL CHECK(side IN ('LONG','SHORT')),
  state TEXT NOT NULL,
  risk_usdt NUMERIC NOT NULL CHECK(risk_usdt>0),
  equity_snapshot_usdt NUMERIC NOT NULL CHECK(equity_snapshot_usdt>0),
  leverage NUMERIC NOT NULL CHECK(leverage>0 AND leverage<=5),
  planned_notional_usdt NUMERIC NOT NULL CHECK(planned_notional_usdt>0),
  details JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_prod_exec_state
 ON luxalgo_production_execution_ledger(state,updated_at);
"""

@dataclass(frozen=True)
class ProposedRisk:
    event_id: str
    contract: str
    side: str
    risk_usdt: str
    equity_usdt: str
    available_usdt: str
    leverage: str
    notional_usdt: str

def _d(value):
    try:
        d=Decimal(str(value))
    except (TypeError,ValueError,InvalidOperation):
        raise ValueError("Invalid numeric risk data") from None
    if not d.is_finite() or d<=0:
        raise ValueError("Risk inputs must be finite and positive")
    return d

def validate_risk(plan):
    """Pure risk guard; does not grant permission to send an order."""
    if not isinstance(plan,ProposedRisk):
        raise ValueError("Risk plan required")
    if not isinstance(plan.event_id,str) or not re.fullmatch(r"[a-z0-9]{20}",plan.event_id):
        raise ValueError("Invalid signal identity")
    if not isinstance(plan.contract,str) or not re.fullmatch(r"[A-Z0-9]{2,28}_USDT",plan.contract):
        raise ValueError("Invalid futures contract")
    if plan.side not in ("LONG","SHORT"):
        raise ValueError("Invalid position side")
    risk,equity,available,lev,notional=map(_d,(plan.risk_usdt,plan.equity_usdt,
                                               plan.available_usdt,plan.leverage,
                                               plan.notional_usdt))
    if lev>MAX_LEVERAGE:
        raise ValueError("Leverage exceeds 5x limit")
    if available>equity:
        raise ValueError("Account margin snapshot inconsistent")
    if risk>equity*PER_TRADE_RISK:
        raise ValueError("Per-trade risk exceeds conservative limit")
    if notional>available*lev:
        raise ValueError("Position exceeds available leveraged margin")
    cid="t-lux-"+hashlib.sha256(plan.event_id.encode("ascii")).hexdigest()[:24]
    return {"client_id":cid,"risk_usdt":risk,"equity_usdt":equity,
            "leverage":lev,"notional_usdt":notional}

class LiveLedger:
    """Durable reservations only. Caller must separately verify exchange state."""
    def __init__(self,database_url):
        if not database_url:
            raise ValueError("Database URL required")
        self.url=database_url

    def reserve(self,plan):
        facts=validate_risk(plan)
        with psycopg.connect(self.url,connect_timeout=5,row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(DDL)
                cur.execute("SELECT pg_advisory_xact_lock(%s)",(LOCK_KEY,))
                cur.execute("""SELECT 1 FROM luxalgo_production_execution_ledger
                               WHERE event_id=%s OR client_id=%s LIMIT 1""",
                            (plan.event_id,facts["client_id"]))
                if cur.fetchone():
                    return {"accepted":False,"reason":"DUPLICATE_SIGNAL"}
                cur.execute("""SELECT COUNT(*) AS positions,
                                      COALESCE(SUM(risk_usdt),0) AS active_risk
                               FROM luxalgo_production_execution_ledger
                               WHERE state = ANY(%s)""",(list(ACTIVE),))
                inventory=cur.fetchone()
                if int(inventory["positions"])>=MAX_POSITIONS:
                    return {"accepted":False,"reason":"POSITION_LIMIT"}
                if _d(inventory["active_risk"]+facts["risk_usdt"])>facts["equity_usdt"]*TOTAL_RISK:
                    return {"accepted":False,"reason":"ACCOUNT_RISK_LIMIT"}
                cur.execute("""SELECT 1 FROM luxalgo_production_execution_ledger
                               WHERE contract=%s AND state=ANY(%s) LIMIT 1""",
                            (plan.contract,list(ACTIVE)))
                if cur.fetchone():
                    return {"accepted":False,"reason":"CONTRACT_ALREADY_ACTIVE"}
                cur.execute("""INSERT INTO luxalgo_production_execution_ledger
                     (event_id,client_id,contract,side,state,risk_usdt,
                      equity_snapshot_usdt,leverage,planned_notional_usdt)
                     VALUES (%s,%s,%s,%s,'RESERVED',%s,%s,%s,%s)""",
                    (plan.event_id,facts["client_id"],plan.contract,plan.side,
                     facts["risk_usdt"],facts["equity_usdt"],facts["leverage"],
                     facts["notional_usdt"]))
                return {"accepted":True,"client_id":facts["client_id"],
                        "state":"RESERVED","exchange_order_submitted":False}

    def transition(self,event_id,new_state,details):
        if new_state not in TRANSITIONS or not isinstance(details,dict):
            raise ValueError("Invalid ledger update")
        if new_state=="PROTECTED_VERIFIED":
            needed=("entry_order_id","stop_order_id","take_order_id","entry_fill_status")
            if any(not details.get(k) for k in needed):
                raise ValueError("Missing protection readback evidence")
            if (str(details["entry_fill_status"]).lower()!="filled"
                or len({str(details[k]) for k in needed[:3]})!=3):
                raise ValueError("Inconsistent protection evidence")
        with psycopg.connect(self.url,connect_timeout=5,row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT state FROM luxalgo_production_execution_ledger
                               WHERE event_id=%s FOR UPDATE""",(event_id,))
                row=cur.fetchone()
                if not row or new_state not in TRANSITIONS.get(row["state"],set()):
                    raise RuntimeError("Unsafe ledger state transition blocked")
                cur.execute("""UPDATE luxalgo_production_execution_ledger
                               SET state=%s,details=details||%s::jsonb,updated_at=now()
                               WHERE event_id=%s""",
                            (new_state,json.dumps(details,allow_nan=False),event_id))
