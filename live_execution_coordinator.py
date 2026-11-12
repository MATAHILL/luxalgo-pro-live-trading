"""Persistent fail-closed production risk ledger and read-only restart reconciliation.

No exchange-writing operations are invoked here. Live sender remains SOURCE_LOCKED.
PostgreSQL advisory transaction locks protect simultaneous reservations across workers.
"""
from __future__ import annotations
import json
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from live_futures_engine import (
    MIGRATION_SQL, LiveSafetyError, MAX_POSITIONS, MAX_AGGREGATE_RISK,
    GateLiveClient, build_plan, evaluate_protection, reconcile_ledger, active_positions,
    verify_ledger_position,
)

from live_risk_policy import per_entry_risk

LOCK_ID = 805513217

def install(con):
    con.execute(MIGRATION_SQL)

def read_rows(con):
    with con.cursor() as cur:
        cur.execute("""SELECT event_id,client_text,contract,side,timeframe,position_mode,state,
                        risk_usdt,entry_price,stop_loss,take_profit,contracts,
                        entry_order_id,stop_order_id,take_order_id,created_at,updated_at
                       FROM luxalgo_prod_execution_ledger
                       WHERE state <> 'CLOSED_VERIFIED'
                          OR event_id IN (
                              SELECT event_id FROM luxalgo_prod_execution_ledger
                              WHERE state = 'CLOSED_VERIFIED'
                              ORDER BY created_at DESC LIMIT 150)
                       ORDER BY (state <> 'CLOSED_VERIFIED') DESC,
                                created_at DESC""")
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
        # Independently enforce the per-trade limit even if the caller
        # supplies a stale, malformed or manipulated trading plan.
        try:
            reserved=[Decimal(str(r["risk_usdt"])) for r in existing]
            incremental=Decimal(str(plan["estimated_risk_usdt"]))
            equity=Decimal(str(account_equity))
        except (InvalidOperation,TypeError,ValueError,KeyError) as exc:
            raise LiveSafetyError("Invalid reserved risk or account equity") from exc
        if (not equity.is_finite() or equity<=0 or
            not incremental.is_finite() or incremental<=0 or
            any(not amount.is_finite() or amount<=0 for amount in reserved)):
            raise LiveSafetyError("Invalid reserved risk or account equity")
        used=sum(reserved,Decimal("0"))
        if incremental>equity*per_entry_risk():
            raise LiveSafetyError("Atomic per-entry " + format((per_entry_risk()*100).normalize(), 'f') + "% risk cap")
        if used+incremental>equity*MAX_AGGREGATE_RISK:
            raise LiveSafetyError("Atomic account-wide risk cap")
        cur.execute("""INSERT INTO luxalgo_prod_execution_ledger(
                        event_id,client_text,contract,side,timeframe,position_mode,state,risk_usdt,
                        entry_price,stop_loss,take_profit,contracts)
                        VALUES (%s,%s,%s,%s,%s,%s,'RESERVED',%s,%s,%s,%s,%s)
                        ON CONFLICT (event_id) DO NOTHING RETURNING event_id""",
                    (plan["event_id"],plan["client_text"],plan["contract"],
                     plan["side"],plan["timeframe"],plan["position_mode"],incremental,
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
            coverage=verify_ledger_position(item,position,snap["triggers"])
            if sum(r['contract']==position['contract'] and r['state']!='CLOSED_VERIFIED'
                   for r in rows) != 1:
                coverage={'verified':False,'reason':'DUPLICATE_ACTIVE_LEDGER_CONTRACT'}
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
                        if str(o.get("id_string") or o.get("id") or "") not in known_trigger_ids]
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

# A ledger row in this state was written by risk_reserve() and never advanced.
# It is a durable RESERVATION: no order id was ever attached, and the only two
# writers of 'SENDING' (controlled_acceptance.claim_dispatch and
# orchestrator.persist_state) both update a pre-existing row unconditionally in
# the same transaction as the dispatch claim, so a sent order can never be left
# behind reading 'RESERVED'. That makes a RESERVED row proof of "never sent".
STALE_RESERVATION_STATE = "RESERVED"

def diagnose_halt(con, client=None):
    """Read-only: report exactly WHAT blocks a halt clear, in operator words.

    The old clear_halt collapsed five distinct causes into one line, so the
    operator could not tell an exchange position from a leftover reservation.
    This never mutates the exchange, the ledger or the control row.
    """
    out={"read_only":True,"exchange_reachable":None,"exchange_positions":None,
         "exchange_open_orders":None,"exchange_open_triggers":None,
         "unresolved_ledger_rows":None,"unresolved_rows":[],
         "stale_reservation_rows":[],"unrecoverable_rows":[],
         "blocking_reasons":[],"can_clear_with_stale_release":False,
         "can_clear":False}
    status=None
    try:
        install(con)
        status=inspect(con,client)
        out["exchange_reachable"]=True
        out["exchange_positions"]=status["open_positions"]
        out["exchange_open_orders"]=status["exchange_open_orders"]
        out["exchange_open_triggers"]=status["exchange_open_triggers"]
        if status["reconciliation"]["requires_halt"]:
            out["blocking_reasons"].append(
                "EXCHANGE_OR_LEDGER_RECONCILIATION_ANOMALY")
        if status["untracked_open_orders"]:
            out["blocking_reasons"].append("UNTRACKED_EXCHANGE_OPEN_ORDERS")
        if status["untracked_open_triggers"]:
            out["blocking_reasons"].append("UNTRACKED_EXCHANGE_TRIGGERS")
    except Exception as exc:
        # An unreadable exchange is UNKNOWN exposure, never a flat account.
        out["exchange_reachable"]=False
        out["blocking_reasons"].append(
            "EXCHANGE_UNREADABLE:" + type(exc).__name__)
        # Still report the ledger, because that is the other half of the answer.
    with con.cursor() as cur:
        cur.execute("""SELECT event_id,contract,state,contracts,entry_order_id,
                              stop_order_id,take_order_id,created_at,updated_at
                       FROM luxalgo_prod_execution_ledger
                       WHERE state <> 'CLOSED_VERIFIED'
                       ORDER BY created_at ASC""")
        rows=[dict(r) for r in cur.fetchall()]
    out["unresolved_ledger_rows"]=len(rows)
    for r in rows:
        row={"event_id":r["event_id"],"contract":r["contract"],
             "state":r["state"],"contracts":str(r["contracts"]),
             "entry_order_id":r["entry_order_id"],
             "created_at":r["created_at"].isoformat() if r.get("created_at") else None}
        if r["state"]==STALE_RESERVATION_STATE and not r["entry_order_id"]:
            out["stale_reservation_rows"].append(row)
        else:
            out["unrecoverable_rows"].append(row)
    if rows:
        out["blocking_reasons"].append("UNRESOLVED_EXECUTION_LEDGER_ROWS")
    # A None status means the exchange readback failed: unreadable is UNKNOWN
    # exposure, so this stays False and the HALT is preserved.
    clean_exchange=(out["exchange_reachable"] is True and status is not None
                    and not status["reconciliation"]["requires_halt"]
                    and not status["untracked_open_orders"]
                    and not status["untracked_open_triggers"])
    out["can_clear_with_stale_release"]=bool(
        clean_exchange and out["stale_reservation_rows"]
        and not out["unrecoverable_rows"])
    out["can_clear"]=bool(clean_exchange and not rows)
    return out

def clear_halt(con, client=None, reason="OPERATOR_RECONCILED_CLEAR",
               release_stale_reservations=False):
    """Clear the persistent HALT ONLY after a clean authenticated reconciliation.

    This is the missing counterpart to halt(). It never clears on a flat account
    alone: it re-reads the live exchange inventory and refuses unless the
    reconciliation reports no untracked positions, orders or triggers and no
    unresolved ledger rows. Any ambiguity leaves the HALT in place.

    release_stale_reservations=True additionally marks provably-unsent
    RESERVED rows as CLOSED_VERIFIED, so a leftover reservation cannot block
    the operator forever. It is still refused whenever the exchange reports any
    position, any open order, any open trigger, an unreadable account, or any
    ledger row that might have reached the exchange. The safety property is
    unchanged: real exposure always blocks.
    """
    if not isinstance(reason,str) or not reason or len(reason)>200:
        raise LiveSafetyError("Invalid halt-clear reason")
    if type(release_stale_reservations) is not bool:
        raise LiveSafetyError("Invalid stale-reservation release flag")
    client=client or GateLiveClient()
    install(con)
    # A clean readback is mandatory; an exception here propagates and the
    # caller must treat the HALT as still active.
    status=inspect(con,client)
    if (status["reconciliation"]["requires_halt"] or
        status["untracked_open_orders"] or status["untracked_open_triggers"]):
        raise LiveSafetyError("Reconciliation still reports unknown or unprotected exposure")
    with con.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)",(LOCK_ID,))
        cur.execute("""SELECT event_id,contract,state,entry_order_id
                       FROM luxalgo_prod_execution_ledger
                       WHERE state <> 'CLOSED_VERIFIED'""")
        open_rows=[dict(r) for r in cur.fetchall()]
        released=[]
        if open_rows:
            if not release_stale_reservations:
                # Name the blocker instead of a generic refusal, so the
                # operator knows whether to look at Gate.io or at the ledger.
                raise LiveSafetyError(
                    "Unresolved execution rows block halt clear: "
                    + ", ".join(str(r["event_id"]) + "/" + str(r["contract"])
                                + "/" + str(r["state"]) for r in open_rows[:5])
                    + (" (+" + str(len(open_rows)-5) + " more)" if len(open_rows)>5 else ""))
            # Only rows that provably never reached the exchange may be closed.
            unsafe=[r for r in open_rows
                    if r["state"] != STALE_RESERVATION_STATE or r["entry_order_id"]]
            if unsafe:
                raise LiveSafetyError(
                    "Halt clear refused: " + str(len(unsafe))
                    + " execution row(s) may have reached the exchange and need"
                    " manual exchange review (" + ", ".join(
                        str(r["event_id"]) + "/" + str(r["state"])
                        for r in unsafe[:5]) + ")")
            for r in open_rows:
                cur.execute("""UPDATE luxalgo_prod_execution_ledger
                               SET state='CLOSED_VERIFIED',
                                   detail=COALESCE(detail,'{}'::jsonb) || %s::jsonb,
                                   updated_at=NOW()
                               WHERE event_id=%s AND state='RESERVED'
                                 AND entry_order_id IS NULL""",
                            (json.dumps({"stale_reservation_released":True,
                                         "released_reason":reason,
                                         "release_note":"never sent to exchange:"
                                                       " reservation had no entry order id"}),
                             r["event_id"]))
                if cur.rowcount != 1:
                    raise LiveSafetyError("Stale reservation release was not atomic")
                released.append(r["event_id"])
        cur.execute("""SELECT COUNT(*)::int AS unresolved
                       FROM luxalgo_prod_execution_ledger
                       WHERE state <> 'CLOSED_VERIFIED'""")
        row=cur.fetchone()
        if not row or row["unresolved"] != 0:
            raise LiveSafetyError("Unresolved execution rows block halt clear")
        cur.execute("""UPDATE luxalgo_prod_execution_control
                       SET halted=FALSE,reason=%s,updated_at=NOW()
                       WHERE id=1""",(reason,))
        if cur.rowcount!=1:
            raise LiveSafetyError("Halt clear database update failed")
    return {"halted":False,"reason":reason,"reconciled":True,
            "untracked_open_orders":0,"untracked_open_triggers":0,
            "stale_reservations_released":released,
            "note":("HALT cleared after a clean authenticated reconciliation"
                    + (" and " + str(len(released)) + " provably-unsent stale"
                       " reservation(s) released" if released else ""))}

def status_fallback(reason):
    """Explicitly distinguish unknown account state from an empty account."""
    return {"status":"UNAVAILABLE","halted":True,"halt_reason":reason,
            "positions_verified":False,"positions":None,"open_positions":None,
            "orders_enabled":False,"live_trading":False}

