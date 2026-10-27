"""Read-only two-pass Gate Futures Testnet unknown-exposure audit.

This is NOT proof that a past entry was never sent or filled. It answers
whether CURRENT account exposure, pending entries or triggers exist on the
contract of an uncertain order. No exchange writes and no journal deletion.
"""
import os
from datetime import datetime,timezone,timedelta
from decimal import Decimal,InvalidOperation
import psycopg
from psycopg.rows import dict_row
from gate_testnet_broker import _request_testnet,ROOT

DDL="""CREATE TABLE IF NOT EXISTS luxalgo_testnet_uncertain_exposure_audit(
 signal_id TEXT PRIMARY KEY,
 contract TEXT NOT NULL,
 state TEXT NOT NULL,
 first_safe_at TIMESTAMPTZ,
 checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 safe_checks INTEGER NOT NULL DEFAULT 0,
 details JSONB NOT NULL DEFAULT '{}'::jsonb
)"""

def _open_position(position,contract):
    if position.get("contract")!=contract:return False
    size=Decimal(str(position["size"]))
    if not size.is_finite():raise ValueError("Invalid position size")
    return size!=0

def _snapshot(read,contract):
    positions=read("GET",ROOT+"/positions")
    orders=read("GET",ROOT+"/orders",query="status=open")
    triggers=read("GET",ROOT+"/price_orders",query="status=open")
    if any(not isinstance(x,list) or not all(isinstance(r,dict) for r in x)
           for x in (positions,orders,triggers)):
        raise ValueError("Incomplete account snapshot")
    if any(_open_position(p,contract) for p in positions):
        return "CURRENT_POSITION_EXPOSURE"
    if any(o.get("contract")==contract for o in orders):
        return "OPEN_ENTRY_ORDER"
    for trigger in triggers:
        init=trigger.get("initial")
        if not isinstance(init,dict):raise ValueError("Unverified trigger metadata")
        if init.get("contract")==contract:
            return "OPEN_PROTECTIVE_OR_TRIGGER_ORDER"
    return "NO_CURRENT_EXPOSURE"

def check_unknown_once(*,database_url=None,request=None,now=None):
    """Persist read-only results; >=2 safe snapshots >=15 seconds apart required."""
    url=database_url or os.getenv("DATABASE_URL","")
    if not url:raise RuntimeError("Database missing")
    read=request or _request_testnet
    instant=now or datetime.now(timezone.utc)
    if instant.tzinfo is None:raise ValueError("Naive timestamp")
    output=[]
    with psycopg.connect(url,connect_timeout=5,row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
            cur.execute("""SELECT signal_id,contract,details FROM luxalgo_testnet_protected_journal
                           WHERE state IN ('HALTED_UNKNOWN','RECOVERING')
                             AND COALESCE(details->>'order_id','')=''
                           ORDER BY created_at ASC LIMIT 5""")
            unknown=cur.fetchall()
    for row in unknown:
        signal_id,contract=row["signal_id"],row["contract"]
        try:
            observation=_snapshot(read,contract)
        except Exception as ex:
            observation="READBACK_UNAVAILABLE_"+type(ex).__name__.upper()
        with psycopg.connect(url,connect_timeout=5,row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(DDL)
                cur.execute("""SELECT state,first_safe_at,safe_checks FROM
                  luxalgo_testnet_uncertain_exposure_audit WHERE signal_id=%s
                  FOR UPDATE""",(signal_id,))
                previous=cur.fetchone()
                safe=observation=="NO_CURRENT_EXPOSURE"
                old_first=previous["first_safe_at"] if previous else None
                continuous=(safe and previous and old_first is not None and
                            timedelta(seconds=15)<=(instant-old_first)<=timedelta(minutes=10))
                if continuous:
                    first=old_first
                    checks=int(previous["safe_checks"])+1
                elif safe:
                    first=instant
                    checks=1
                else:
                    first=None
                    checks=0
                state=("NO_EXPOSURE_RECHECKED" if safe and checks>=2
                       else "NO_EXPOSURE_FIRST_READ" if safe else observation)
                cur.execute("""INSERT INTO luxalgo_testnet_uncertain_exposure_audit
                  (signal_id,contract,state,first_safe_at,checked_at,safe_checks,details)
                  VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb)
                  ON CONFLICT(signal_id) DO UPDATE SET
                    contract=EXCLUDED.contract,state=EXCLUDED.state,
                    first_safe_at=EXCLUDED.first_safe_at,checked_at=EXCLUDED.checked_at,
                    safe_checks=EXCLUDED.safe_checks,details=EXCLUDED.details""",
                  (signal_id,contract,state,first,instant,checks,
                   '{"exchange_order_fill_proven":false,"read_only":true}'))
                output.append(dict(contract=contract,signal_id=signal_id,state=state))
    return {"checked":len(output),"reports":output,"exchange_writes":False}

def unknown_exposure_gate(database_url=None,now=None):
    """Fail closed if ANY uncertain order lacks fresh two-pass flat evidence."""
    url=database_url or os.getenv("DATABASE_URL","")
    if not url:return False
    instant=now or datetime.now(timezone.utc)
    try:
        with psycopg.connect(url,connect_timeout=5,row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(DDL)
                cur.execute("""SELECT j.signal_id,a.state,a.checked_at FROM
                    luxalgo_testnet_protected_journal j
                    LEFT JOIN luxalgo_testnet_uncertain_exposure_audit a
                    ON a.signal_id=j.signal_id
                    WHERE j.state IN ('HALTED_UNKNOWN','HALTED_UNPROTECTED',
                                      'EMERGENCY_CLOSE_REQUIRED','RECOVERING')
                    ORDER BY j.created_at""")
                records=cur.fetchall()
        for row in records:
            checked=row["checked_at"]
            if (row["state"]!="NO_EXPOSURE_RECHECKED"
                or not checked or checked.tzinfo is None
                or not timedelta(0)<=(instant-checked)<=timedelta(seconds=90)):
                return False
        return True
    except (psycopg.Error,RuntimeError,OSError,ValueError,TypeError):
        return False
