"""Persistent fail-closed production risk ledger and read-only restart reconciliation.

No exchange-writing operations are invoked here. Live sender remains SOURCE_LOCKED.
PostgreSQL advisory transaction locks protect simultaneous reservations across workers.
"""
from __future__ import annotations
from decimal import Decimal
from datetime import datetime, timezone
from live_futures_engine import (
    MIGRATION_SQL, LiveSafetyError, MAX_POSITIONS, MAX_AGGREGATE_RISK,
    GateLiveClient, build_plan, evaluate_protection, reconcile_ledger, active_positions,
)

LOCK_ID = 805513217

def install(con):
    con.execute(MIGRATION_SQL)

def read_rows(con):
    with con.cursor() as cur:
        cur.execute("""SELECT event_id,client_text,contract,side,timeframe,state,
                        risk_usdt,entry_price,stop_loss,take_profit,contracts,
                        entry_order_id,stop_order_id,take_order_id,created_at,updated_at
                       FROM luxalgo_prod_execution_ledger
                       ORDER BY created_at DESC LIMIT 150""")
        return [dict(r) for r in cur.fetchall()]

def control(con):
    with con.cursor() as cur:
        cur.execute("SELECT halted,reason,updated_at FROM luxalgo_prod_execution_control WHERE id=1")
        row=cur.fetchone()
    if row is None:
        raise LiveSafetyError("Production emergency control missing")
    return dict(row)

def halt(con,reason="OPERATOR_REQUESTED"):
    if not isinstance(reason,str) or len(reason)>200:
        raise LiveSafetyError("Invalid emergency halt reason")
    with con.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)",(LOCK_ID,))
        cur.execute("""UPDATE luxalgo_prod_execution_control
                       SET halted=TRUE,reason=%s,updated_at=NOW()
                       WHERE id=1""",(reason,))
        if cur.rowcount!=1:
            raise LiveSafetyError("Emergency halt database update failed")
    return {"halted":True,"reason":reason,"exchange_orders_cancelled":0,
            "positions_closed":0,"note":"DB halt blocks future dispatch; it does not itself close exchange positions"}

def risk_reserve(con,plan,exchange_positions,account_equity):
    """Atomic five-slot + total risk reservation; fails when globally halted.

    Must be invoked only after fresh authenticated account+position+order snapshots.
    A reservation is not an exchange order. No future worker can auto-submit it.
    """
    positions=active_positions(exchange_positions)
    with con.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)",(LOCK_ID,))
        cur.execute("SELECT halted FROM luxalgo_prod_execution_control WHERE id=1")
        control_row=cur.fetchone()
        if not control_row or control_row["halted"] is not False:
            raise LiveSafetyError("Persistent production HALT active")
        cur.execute("""SELECT contract,state,risk_usdt
                       FROM luxalgo_prod_execution_ledger
                       WHERE state<>'CLOSED_VERIFIED'""")
        existing=cur.fetchall()
        exchange_contracts={p["contract"] for p in positions}
        reserved_contracts={r["contract"] for r in existing}
        if plan["contract"] in exchange_contracts|reserved_contracts:
            raise LiveSafetyError("Concurrent or already open contract")
        # Count the union: a protected position and its ledger record are
        # ONE slot, not two. Unknown manual exposure fails closed.
        if exchange_contracts-reserved_contracts:
            raise LiveSafetyError("Untracked open position: halt risk reservation")
        if len(exchange_contracts|reserved_contracts)>=MAX_POSITIONS:
            raise LiveSafetyError("Five-position cap including pending reservations")
        used=sum((Decimal(str(r["risk_usdt"])) for r in existing),Decimal("0"))
        incremental=Decimal(plan["estimated_risk_usdt"])
        equity=Decimal(str(account_equity))
        if not equity.is_finite() or equity<=0 or used+incremental>equity*MAX_AGGREGATE_RISK:
            raise LiveSafetyError("Atomic account-wide risk cap")
        cur.execute("""INSERT INTO luxalgo_prod_execution_ledger(
                        event_id,client_text,contract,side,timeframe,state,risk_usdt,
                        entry_price,stop_loss,take_profit,contracts)
                        VALUES (%s,%s,%s,%s,%s,'RESERVED',%s,%s,%s,%s,%s)
                        ON CONFLICT (event_id) DO NOTHING RETURNING event_id""",
                    (plan["event_id"],plan["client_text"],plan["contract"],
                     plan["side"],plan["timeframe"],incremental,
                     plan["entry"],plan["sl"],plan["tp"],plan["contracts"]))
        if not cur.fetchone():
            raise LiveSafetyError("Duplicate TradingView event; no new entry")
    return {"state":"RESERVED","event_id":plan["event_id"],"orders_submitted":0}

def inspect(con, client=None):
    """Read-only exchange inventory against the durable ledger.

    Fails closed when order/trigger API fails or fills are ambiguous.
    Never silently mark missing positions as closed.
    """
    client=client or GateLiveClient()
    install(con)
    rows=read_rows(con)
    ctrl=control(con)
    snap=client.snapshot()
    comparison=reconcile_ledger(rows,snap["positions"],snap["triggers"])
    by_contract={r["contract"]:r for r in rows if r["state"]!="CLOSED_VERIFIED"}
    cards=[]
    for position in snap["positions"]:
        item=by_contract.get(position["contract"])
        coverage={"verified":False,"reason":"UNTRACKED_EXCHANGE_POSITION"}
        if item:
            expectation={"contract":item["contract"],"side":item["side"],
                         "entry":item["entry_price"],"sl":item["stop_loss"],
                         "tp":item["take_profit"]}
            coverage=evaluate_protection(expectation,snap["triggers"])
            if (coverage["verified"] and
                (coverage["stop_order_id"]!=str(item.get("stop_order_id") or "") or
                 coverage["take_order_id"]!=str(item.get("take_order_id") or ""))):
                coverage={"verified":False,"reason":"PROTECTION_ORDER_IDS_NOT_LINKED_TO_LEDGER"}
        cards.append({
            "contract":position["contract"],
            "side":"LONG" if Decimal(str(position["size"]))>0 else "SHORT",
            "size_contracts":str(position["size"]),
            "entry_price":position.get("entry_price"),
            "mark_price":position.get("mark_price"),
            "unrealized_pnl":position.get("unrealised_pnl",
                                        position.get("unrealized_pnl")),
            "execution_status":item["state"] if item else "EXTERNAL_OR_UNTRACKED",
            "protection_status":"EXCHANGE_VERIFIED" if coverage["verified"] else "NOT_VERIFIED",
            "protection_reason":coverage["reason"],
            "stop_loss":str(item["stop_loss"]) if item and coverage["verified"] else None,
            "take_profit":str(item["take_profit"]) if item and coverage["verified"] else None,
            "stop_order_id":coverage.get("stop_order_id"),
            "take_order_id":coverage.get("take_order_id"),
        })
    known_text={r["client_text"] for r in rows}
    known_trigger_ids={str(r[k]) for r in rows for k in
                       ("stop_order_id","take_order_id") if r.get(k)}
    untracked_orders=[o for o in snap["orders"]
                      if str(o.get("text","")) not in known_text]
    untracked_triggers=[o for o in snap["triggers"]
                        if str(o.get("id","")) not in known_trigger_ids]
    anomalies=(comparison["requires_halt"] or bool(untracked_orders)
               or bool(untracked_triggers))
    return {"status":"READ_ONLY_RECONCILED" if not anomalies else "HALT_REVIEW_REQUIRED",
            "halted":bool(ctrl["halted"]) or anomalies,
            "halt_reason":ctrl["reason"] if not anomalies else "UNKNOWN_OR_UNPROTECTED_EXPOSURE",
            "positions":cards,"open_positions":len(cards),
            "exchange_open_orders":len(snap["orders"]),
            "exchange_open_triggers":len(snap["triggers"]),
            "untracked_open_orders":len(untracked_orders),
            "untracked_open_triggers":len(untracked_triggers),
            "reconciliation":comparison,
            "orders_enabled":False,"live_trading":False,
            "exchange_source":snap["source"],
            "fetched_at":snap["fetched_at"]}

def reconcile_and_halt_if_needed(con, client=None):
    """Run at restart and on periodic refresh; persist halt on any anomaly."""
    try:
        status=inspect(con,client)
    except Exception:
        # If exchange readback fails, refuse new entries and persist the halt.
        install(con)
        halt(con,"EXCHANGE_RECONCILIATION_FAILED")
        raise
    if (status["reconciliation"]["requires_halt"] or
        status["untracked_open_orders"] or status["untracked_open_triggers"]):
        halt(con,"UNPROTECTED_OR_UNKNOWN_EXCHANGE_EXPOSURE")
    return status

def status_fallback(reason):
    """Explicitly distinguish unknown account state from an empty account."""
    return {"status":"UNAVAILABLE","halted":True,"halt_reason":reason,
            "positions_verified":False,"positions":None,"open_positions":None,
            "orders_enabled":False,"live_trading":False}
