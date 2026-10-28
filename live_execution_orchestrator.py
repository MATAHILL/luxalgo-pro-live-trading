"""Source-locked live futures order lifecycle coordinator.

Prepared for separately authorized limited exchange tests. This module cannot
submit real orders until a deliberate source-code release AND environment
permission AND the persistent database HALT is independently lifted.
No Testnet, no market-scanner involvement, no blind post-timeout retries.
"""
from __future__ import annotations
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import time
from live_futures_engine import (
    EXCHANGE_WRITE_RELEASE, LiveSafetyError, AckUnknown, GateLiveClient,
    PREFIX, ORDER_RE, build_plan, client_text, evaluate_protection,
    verified_account_equity)
from live_execution_coordinator import install,control,halt,read_rows,risk_reserve

def persist_state(open_connection, event_id, state, *, entry_order_id=None,
                  stop_order_id=None, take_order_id=None, detail=None):
    """Persist lifecycle milestone before/after external effects."""
    with open_connection() as con:
        with con.cursor() as cur:
            cur.execute("""UPDATE luxalgo_prod_execution_ledger
                           SET state=%s,
                               entry_order_id=COALESCE(%s,entry_order_id),
                               stop_order_id=COALESCE(%s,stop_order_id),
                               take_order_id=COALESCE(%s,take_order_id),
                               detail=COALESCE(%s::jsonb,detail),
                               updated_at=NOW()
                           WHERE event_id=%s""",
                        (state,entry_order_id,stop_order_id,take_order_id,
                         detail,event_id))
            if cur.rowcount!=1:
                raise LiveSafetyError("Journal state update was not persisted")
    return state

def emergency_persist_halt(open_connection, reason, event_id=None):
    """Emergency action is durable refusal of further entries, never false flat."""
    with open_connection() as con:
        install(con)
        out=halt(con,reason)
        if event_id:
            with con.cursor() as cur:
                cur.execute("""UPDATE luxalgo_prod_execution_ledger
                               SET state='HALTED',updated_at=NOW()
                               WHERE event_id=%s AND state<>'CLOSED_VERIFIED'""",
                            (event_id,))
    return out

def require_full_entry_readback(client,plan,entry_ack):
    """IOC ack alone is not fill proof. Require ID, exact text and live position."""
    rawid=entry_ack.get("id_string") or entry_ack.get("id") if isinstance(entry_ack,dict) else None
    order_id=str(rawid or "")
    if not ORDER_RE.fullmatch(order_id):
        raise AckUnknown("Order entry acknowledgment missing valid identity")
    order=client.request("GET",PREFIX+"/orders/"+order_id)
    if not isinstance(order,dict):
        raise AckUnknown("Entry readback malformed")
    if (str(order.get("id_string") or order.get("id"))!=order_id or
        order.get("text")!=plan["client_text"] or
        order.get("contract")!=plan["contract"]):
        raise AckUnknown("Entry readback identity mismatch")
    filled=Decimal(str(order.get("size",0)))-Decimal(str(order.get("left",0)))
    signed=int(plan["contracts"])*(1 if plan["side"]=="LONG" else -1)
    # The API uses signed size; IOC partially filled orders MUST NOT be mistaken
    # for the planned full size. Any ambiguity goes to HALT, not second entry.
    if (filled!=signed or
        order.get("status")!="finished" or
        Decimal(str(order.get("fill_price") or 0))<=0):
        raise AckUnknown("Order not fully filled and reconciled")
    snap=client.snapshot()
    matches=[p for p in snap["positions"] if p["contract"]==plan["contract"]]
    if len(matches)!=1 or Decimal(str(matches[0]["size"]))!=Decimal(signed):
        raise AckUnknown("Position size/direction mismatches fill readback")
    return order_id,snap

def full_close_oneway_order(plan):
    """Single/hedge-mode one-shot reduce-only emergency close.

    Exact Gate account mode must be independently verified before constructing.
    """
    if plan.get("side") not in ("LONG","SHORT"):
        raise LiveSafetyError("Unknown emergency close direction")
    mode=plan.get("position_mode")
    if mode not in ("single","dual"):
        raise LiveSafetyError("Emergency close requires verified account position mode")
    order={"contract":plan["contract"],"size":0,"reduce_only":True,
           "price":"0","tif":"ioc",
           "text":"t-em-" + plan["client_text"][2:25]}
    if mode=="single":
        order["close"]=True
    else:
        order["auto_size"]="close_long" if plan["side"]=="LONG" else "close_short"
    return order

def emergency_close_once(client,plan):
    """At most one close attempt; ambiguous response requires manual exchange review."""
    order=full_close_oneway_order(plan)
    try:
        response=client.request("POST",PREFIX+"/orders",payload=order)
        if not isinstance(response,dict) or not (response.get("id") or response.get("id_string")):
            raise AckUnknown("Close acknowledgment missing")
        snap=client.snapshot()
        if any(p["contract"]==plan["contract"] for p in snap["positions"]):
            raise AckUnknown("Close not confirmed flat by exchange")
        if any((p.get("initial") or {}).get("contract")==plan["contract"]
               for p in snap["triggers"]):
            raise AckUnknown("Orphan native protective triggers remain")
        return {"closed_verified":True,"orders_sent":1}
    except (LiveSafetyError,AckUnknown):
        return {"closed_verified":False,"requires_manual_exchange_review":True,
                "reason":"EMERGENCY_CLOSE_UNKNOWN_NO_RETRY"}

def assert_existing_protected_exposure(rows,snapshot):
    """Allows five separate protected positions while refusing unknown exposure."""
    open_rows=[r for r in rows if r["state"]!="CLOSED_VERIFIED"]
    tracked={r["contract"]:r for r in open_rows}
    if len(tracked)!=len(open_rows):
        raise LiveSafetyError("Multiple active ledger rows for a contract")
    if snapshot["orders"]:
        raise LiveSafetyError("Unreconciled working entry orders")
    known_triggers={str(r[k]) for r in open_rows
                    for k in ("stop_order_id","take_order_id") if r.get(k)}
    for trigger in snapshot["triggers"]:
        if str(trigger.get("id","")) not in known_triggers:
            raise LiveSafetyError("Orphan or external native trigger")
    for p in snapshot["positions"]:
        row=tracked.get(p["contract"])
        if not row or row["state"]!="PROTECTED":
            raise LiveSafetyError("Open position without protected ledger")
        expected_side="LONG" if Decimal(str(p["size"]))>0 else "SHORT"
        if expected_side!=row["side"]:
            raise LiveSafetyError("Position direction mismatch")
        plan={"contract":row["contract"],"side":row["side"],
              "entry":row["entry_price"],"sl":row["stop_loss"],
              "tp":row["take_profit"],"position_mode":row["position_mode"]}
        check=evaluate_protection(plan,snapshot["triggers"])
        if (not check["verified"] or
            check["stop_order_id"]!=str(row["stop_order_id"]) or
            check["take_order_id"]!=str(row["take_order_id"])):
            raise LiveSafetyError("Verified position missing linked native protection")
    # A SENDING or ACK_UNKNOWN row cannot be assumed flat just because the
    # position API momentarily returns no record.
    for row in open_rows:
        if row["state"] not in ("PROTECTED",):
            raise LiveSafetyError("Pending or uncertain order lifecycle blocks new entry")
    return {"protected_open_positions":len(snapshot["positions"]),
            "unknown_orders":0,"unknown_triggers":0}


def execute_confirmed_signal(open_connection,plan,client=None):
    """Serialized journal-to-exchange flow. NOT callable under current source lock.

    Deliberately refuses to run if exchange-write source constant is false.
    Do NOT invoke from market data handlers or dashboard buttons.
    """
    if not EXCHANGE_WRITE_RELEASE:
        raise LiveSafetyError("SOURCE_LOCKED: live order dispatch not released")
    client=client or GateLiveClient()
    with open_connection() as con:
        install(con)
        if control(con)["halted"]:
            raise LiveSafetyError("Production emergency halt active")
        # A fresh account snapshot must have no untracked orders before sending.
        snap=client.snapshot()
        existing=read_rows(con)
        assert_existing_protected_exposure(existing,snap)
        verified_equity,_=verified_account_equity(snap["account"])
        risk_reserve(con,plan,snap["positions"],verified_equity)
    event=plan["event_id"]
    entry_verified=False
    persist_state(open_connection,event,"SENDING")
    # From this point on, a lost response can still mean an accepted order.
    # Therefore never retry POST after an ambiguous outcome.
    try:
        entry_ack=client.submit_entry(plan)
        if not isinstance(entry_ack,dict):
            raise AckUnknown("Entry acknowledgement invalid")
        order_id,snap=require_full_entry_readback(client,plan,entry_ack)
        entry_verified=True
        persist_state(open_connection,event,"ENTRY_CONFIRMED",entry_order_id=order_id)
        persist_state(open_connection,event,"PROTECTION_PENDING")
        stop=client.submit_trigger(plan,"SL")
        take=client.submit_trigger(plan,"TP")
        if not isinstance(stop,dict) or not isinstance(take,dict):
            raise AckUnknown("Native stop/target acknowledgment incomplete")
        snapshot=client.snapshot()
        checked=evaluate_protection(plan,snapshot["triggers"])
        if not checked["verified"]:
            raise AckUnknown("Native exchange protection verification failed")
        persist_state(open_connection,event,"PROTECTED",
                      stop_order_id=checked["stop_order_id"],
                      take_order_id=checked["take_order_id"])
        return {"state":"PROTECTED","entry_order_id":order_id,
                "stop_order_id":checked["stop_order_id"],
                "take_order_id":checked["take_order_id"],
                "filled":True,"protection_verified":True}
    except (AckUnknown,LiveSafetyError,ValueError,InvalidOperation,TypeError,KeyError) as exc:
        # The entry could exist. Quarantine, halt future entries, attempt one
        # close only when the exchange definitely confirms a live position.
        # Do NOT auto-retry ambiguous entry acknowledgments.
        emergency_persist_halt(open_connection,"UNKNOWN_OR_UNPROTECTED_POST_ACK",event)
        close_result={"closed_verified":False,"reason":"UNVERIFIED_ENTRY_NO_AUTOMATIC_CLOSE"}
        if entry_verified:
            # A *verified* filled position with failed native protection may
            # be reduced once. Never close based only on ambiguous POST ACK.
            close_result=emergency_close_once(client,plan)
        return {"state":"HALTED_UNKNOWN","reason":type(exc).__name__,
                "exchange_position_may_be_open":not close_result["closed_verified"],
                "emergency_close":close_result,"automatic_retry":False,
                "requires_manual_exchange_review":not close_result["closed_verified"]}

# Separate release gate for continuous automation, independent of a one-order
# live acceptance test. Changing environment variables cannot bypass this.
AUTOMATIC_WORKER_RELEASE = False

def process_stored_luxalgo_alert(open_connection,event_id,client=None):
    """Authenticated, validated TradingView receipt -> protected order workflow.

    Requires a stable TradingView event/bar ID and MARKET order declaration.
    Never treats scanner candidates or unstamped alerts as executable signals.
    Both source locks are currently OFF.
    """
    if not EXCHANGE_WRITE_RELEASE or not AUTOMATIC_WORKER_RELEASE:
        raise LiveSafetyError("AUTOMATION_SOURCE_LOCKED")
    client=client or GateLiveClient()
    with open_connection() as con:
        install(con)
        if control(con)["halted"]:
            raise LiveSafetyError("Persistent live execution HALT")
        with con.cursor() as cur:
            cur.execute("""SELECT h.event_id,h.source_bar_id,h.symbol,h.side,
                           h.timeframe,h.order_type,h.level_status,h.received_at,
                           l.entry_price,l.stop_loss,l.take_profit
                           FROM luxalgo_live_signal_handoff h
                           JOIN luxalgo_signal_levels l ON h.event_id=l.event_id
                           WHERE h.event_id=%s LIMIT 1""",(event_id,))
            row=cur.fetchone()
        ledger=read_rows(con)
    if (not row or not row["source_bar_id"] or
        row["level_status"]!="VALID" or row["order_type"]!="MARKET"):
        raise LiveSafetyError("Missing validated LuxAlgo identity/levels or MARKET declaration")
    if row["symbol"].endswith("USDT"):
        contract=row["symbol"][:-4]+"_USDT"
    else:
        raise LiveSafetyError("Non-USDT signal")
    # Contract metadata and balance are checked against the live exchange,
    # never inferred from TradingView or a scanner ranking.
    snap=client.snapshot()
    verified_mode=snap["account"].get("position_mode")
    if verified_mode not in ("single","dual"):
        raise LiveSafetyError("Authenticated live position mode unknown or unsupported")
    meta=client.request("GET",PREFIX+"/contracts/"+contract)
    if not isinstance(meta,dict):
        raise LiveSafetyError("Live contract metadata invalid")
    used=sum((Decimal(str(r["risk_usdt"])) for r in ledger
              if r["state"]!="CLOSED_VERIFIED"),Decimal("0"))
    equity,available=verified_account_equity(snap["account"])
    plan=build_plan(dict(event_id=event_id,contract=contract,
                    side=row["side"],timeframe=row["timeframe"],
                    levels_status=row["level_status"],entry=row["entry_price"],
                    sl=row["stop_loss"],tp=row["take_profit"],
                    received_epoch=int(row["received_at"].timestamp()),
                    position_mode=verified_mode,leverage=5),
                    meta,equity,available,
                    snap["positions"],reserved_risk=used)
    return execute_confirmed_signal(open_connection,plan,client)


def restart_reconcile(open_connection,client=None):
    """Reconcile durable rows against live exchange and persist HALT on ambiguity."""
    from live_execution_coordinator import reconcile_and_halt_if_needed
    client=client or GateLiveClient()
    with open_connection() as con:
        result=reconcile_and_halt_if_needed(con,client)
    if result["reconciliation"]["requires_halt"] or result["untracked_open_orders"]:
        return {"status":"HALTED_REVIEW_REQUIRED","orders_resubmitted":0,"readback":result}
    return {"status":"RECONCILED_READ_ONLY","orders_resubmitted":0,"readback":result}
