"""Fresh LuxAlgo -> Gate.io TESTNET only. Both opt-in switches default OFF.

No production Gate.io host or keys are present in this module.
This module does not run on import or replay prior signals.
"""
from datetime import datetime,timezone
from decimal import Decimal,InvalidOperation
import os
import re
import psycopg
from psycopg.rows import dict_row

from testnet_protected_coordinator import TestnetIntent, execute_protected_testnet
from testnet_execution_journal import PostgresTestnetJournal
from gate_testnet_broker import GateFuturesTestnetBroker


def testnet_automation_armed():
    """Arm Gate Testnet only with recovery on and all production writes OFF."""
    return (os.getenv("GATE_TESTNET_PROTECTED_EXECUTION_ENABLED","false").lower()=="true"
            and os.getenv("GATE_TESTNET_WEBHOOK_AUTOMATION_ENABLED","false").lower()=="true"
            and os.getenv("GATE_TESTNET_RECOVERY_ENABLED","false").lower()=="true"
            and os.getenv("GATE_TESTNET_ONE_SHOT_PROTECTED_TRIAL","false").lower()!="true"
            and os.getenv("LIVE_TRADING_ENABLED","false").lower()=="false"
            and os.getenv("ORDERS_ENABLED","false").lower()=="false")


def testnet_recovery_armed():
    """Independent Testnet protection recovery flag; never authorizes an entry."""
    return (os.getenv("GATE_TESTNET_RECOVERY_ENABLED","false").lower()=="true"
            or testnet_automation_armed())


def run_fresh_luxalgo_testnet_signal(event_id, *, broker=None, journal=None, now=None):
    """Execute only a newly RECEIVED & committed LuxAlgo event from PostgreSQL.

    Refuses historical replay, missing TP/SL, bot OFF, duplicate, or old signals.
    TESTNET only. No retries of unknown broker POST outcomes.
    """
    if not testnet_automation_armed():
        return {"state":"TESTNET_DISABLED","submitted":False}
    if not isinstance(event_id,str) or not re.fullmatch(r"[a-f0-9]{20}",event_id):
        raise ValueError("Invalid LuxAlgo event identifier")
    database_url=os.getenv("DATABASE_URL","")
    if not database_url:
        return {"state":"DB_UNAVAILABLE","submitted":False}
    with psycopg.connect(database_url,connect_timeout=5,row_factory=dict_row) as con:
        with con.cursor() as cur:
            cur.execute("""SELECT i.event_id,i.received_at,i.symbol,i.side,i.timeframe,
                           l.status AS level_status,l.entry_price,l.stop_loss,l.take_profit,
                           b.enabled AS bot_enabled
                           FROM luxalgo_dry_run_intents i
                           JOIN luxalgo_signal_levels l ON l.event_id=i.event_id
                           JOIN luxalgo_auto_paper_queue q ON q.event_id=i.event_id
                           JOIN luxalgo_bot_control b ON b.id=1
                           WHERE i.event_id=%s AND q.state IN ('PENDING','CREATED')""",(event_id,))
            r=cur.fetchone()
    if not r or r["bot_enabled"] is not True or r["level_status"]!="VALID":
        return {"state":"SIGNAL_NOT_ELIGIBLE","submitted":False}
    if r["timeframe"] not in ("5m","15m") or r["side"] not in ("LONG","SHORT"):
        return {"state":"SIGNAL_MODE_UNSUPPORTED","submitted":False}
    observed=now or datetime.now(timezone.utc)
    rec=r["received_at"]
    if rec.tzinfo is None:
        return {"state":"INVALID_SIGNAL_TIMESTAMP","submitted":False}
    age=(observed-rec).total_seconds()
    if not 0<=age<=60:
        return {"state":"SIGNAL_EXPIRED","submitted":False}
    symbol=r["symbol"]
    if not isinstance(symbol,str) or not re.fullmatch(r"[A-Z0-9]{2,28}USDT",symbol):
        return {"state":"INVALID_CONTRACT","submitted":False}
    try:
        entry,stop,target=(Decimal(str(r[k])) for k in ("entry_price","stop_loss","take_profit"))
        if not all(x.is_finite() and x>0 for x in (entry,stop,target)):
            return {"state":"INVALID_LEVELS","submitted":False}
    except (TypeError,InvalidOperation,ValueError):
        return {"state":"INVALID_LEVELS","submitted":False}
    intent=TestnetIntent(event_id,symbol[:-4]+"_USDT",r["side"],
                         str(entry),str(stop),str(target),"t-lx-"+event_id)
    gateway=broker or GateFuturesTestnetBroker()
    ledger=journal or PostgresTestnetJournal(database_url)
    return execute_protected_testnet(intent,gateway,ledger)
