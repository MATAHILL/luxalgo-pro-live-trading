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
    verified_account_equity, verify_ledger_position, dec, price_geometry,
    verify_execution_preflight)
from live_execution_coordinator import install,control,halt,read_rows,risk_reserve

def persist_state(open_connection, event_id, state, *, entry_order_id=None,
                  stop_order_id=None, take_order_id=None, detail=None,
                  acceptance_approval_id=None, acceptance_plan=None):
    """Compare-and-set milestones; late workers cannot overwrite HALT/closure.

    No generic route to CLOSED_VERIFIED: closure needs separate exchange proof.
    A rejected transition must halt the caller, never be retried as a new entry.
    """
    predecessors = {
        'SENDING': 'RESERVED',
        'ACK_UNKNOWN': 'SENDING',
        'ENTRY_CONFIRMED': 'SENDING',
        'PROTECTION_PENDING': 'ENTRY_CONFIRMED',
        'PROTECTED': 'PROTECTION_PENDING',
    }
    if state not in predecessors:
        raise LiveSafetyError('Unsupported journal transition')
    if state == 'ENTRY_CONFIRMED' and not ORDER_RE.fullmatch(str(entry_order_id or '')):
        raise LiveSafetyError('Confirmed entry requires verified order identity')
    if state == 'PROTECTED' and (
        not ORDER_RE.fullmatch(str(stop_order_id or '')) or
        not ORDER_RE.fullmatch(str(take_order_id or '')) or
        str(stop_order_id) == str(take_order_id)
    ):
        raise LiveSafetyError('Protected journal requires two distinct native order IDs')
    with open_connection() as con:
        with con.cursor() as cur:
            if state == 'SENDING':
                # Serialize the dispatch claim with operator OFF/HALT and
                # reservations. A check in a previous transaction is stale.
                from live_execution_coordinator import LOCK_ID
                cur.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK_ID,))
                if acceptance_approval_id is None:
                    from production_bot_control import require_operator_on
                    require_operator_on(con)
                else:
                    # Acceptance has its own exact-event authority. Recheck it
                    # in the dispatch transaction, not just before reservation.
                    if not EXCHANGE_WRITE_RELEASE or AUTOMATIC_WORKER_RELEASE:
                        raise LiveSafetyError('Acceptance dispatch release gates rejected')
                    if (not isinstance(acceptance_plan, dict) or
                        acceptance_plan.get('event_id') != event_id):
                        raise LiveSafetyError('Acceptance dispatch event mismatch')
                    if control(con)['halted'] is not False:
                        raise LiveSafetyError('Acceptance emergency HALT active at dispatch')
                    from acceptance_authorization import require_approval
                    require_approval(con, acceptance_approval_id, acceptance_plan)
            cur.execute("""UPDATE luxalgo_prod_execution_ledger
                           SET state=%s,
                               entry_order_id=COALESCE(%s,entry_order_id),
                               stop_order_id=COALESCE(%s,stop_order_id),
                               take_order_id=COALESCE(%s,take_order_id),
                               detail=COALESCE(%s::jsonb,detail),
                               updated_at=NOW()
                           WHERE event_id=%s AND state=%s
                             AND (entry_order_id IS NULL OR %s::text IS NULL OR entry_order_id=%s)
                             AND (stop_order_id IS NULL OR %s::text IS NULL OR stop_order_id=%s)
                             AND (take_order_id IS NULL OR %s::text IS NULL OR take_order_id=%s)""",
                        (state,entry_order_id,stop_order_id,take_order_id,
                         detail,event_id,predecessors[state],
                         entry_order_id,entry_order_id,stop_order_id,stop_order_id,
                         take_order_id,take_order_id))
            if cur.rowcount!=1:
                raise LiveSafetyError("Journal transition rejected: stale state or conflicting order identity")
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
    if (filled!=signed or Decimal(str(order.get('size',0)))!=signed or
        Decimal(str(order.get('left',0)))!=0 or
        order.get("status")!="finished" or
        Decimal(str(order.get("fill_price") or 0))<=0):
        raise AckUnknown("Order not fully filled and reconciled")
    fill=dec(order.get('fill_price'),'fill price')
    entry=dec(plan.get('entry'),'signal entry')
    slip=dec(plan.get('max_entry_slippage_ratio','0.001'),'entry slippage')
    if slip>Decimal('0.001') or (
        fill>entry*(1+slip) if plan['side']=='LONG' else fill<entry*(1-slip)
    ):
        raise AckUnknown('Confirmed fill exceeds permitted entry slippage')
    try:
        price_geometry(plan['side'],fill,plan['sl'],plan['tp'])
    except LiveSafetyError as exc:
        raise AckUnknown('Fill invalidates original LuxAlgo protection geometry') from exc
    snap=client.snapshot()
    matches=[p for p in snap["positions"] if p["contract"]==plan["contract"]]
    if len(matches)!=1 or Decimal(str(matches[0]["size"]))!=Decimal(signed):
        raise AckUnknown("Position size/direction mismatches fill readback")
    return order_id,snap


def require_linked_trigger_acknowledgments(stop, take, checked):
    """Native GET identities must match these exact two POST acknowledgments."""
    ids=[]
    for acknowledgment in (stop,take):
        identity=str(acknowledgment.get('id_string') or acknowledgment.get('id') or '') if isinstance(acknowledgment,dict) else ''
        if not ORDER_RE.fullmatch(identity):
            raise AckUnknown('Native protection acknowledgment identity missing')
        ids.append(identity)
    if (ids[0]==ids[1] or not checked.get('verified') or
        ids != [checked.get('stop_order_id'),checked.get('take_order_id')]):
        raise AckUnknown('Native protection readback does not match submitted orders')

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

def emergency_close_once(client,plan,*,durable_claim=None,open_connection=None):
    """A close POST requires a previously persisted one-shot claim.

    This helper is deliberately NOT called by the live execution exception
    path. It does not create an approval, certify recovery or lift source locks.
    A forged or missing claim must never reach the exchange transport.
    """
    if (not isinstance(durable_claim,dict) or
        durable_claim.get("claimed") is not True or
        durable_claim.get("state") != "CLAIMED" or
        durable_claim.get("event_id") != plan.get("event_id") or
        durable_claim.get("contract") != plan.get("contract") or
        durable_claim.get("close_client_text") !=
           "t-em-" + plan.get("client_text","")[2:25] or
        not durable_claim.get("approval_reference") or
        not callable(open_connection)):
        return {"closed_verified":False,"orders_sent":0,
                "requires_manual_exchange_review":True,
                "reason":"DURABLE_ONE_SHOT_OWNER_APPROVED_CLAIM_REQUIRED"}
    order=full_close_oneway_order(plan)
    from live_emergency_one_shot_claim import (
        consume_one_shot_recovery_claim,finalize_one_shot_recovery_claim)
    try:
        # A persisted atomic CAS is consumed BEFORE any possible POST.
        # A restart or duplicate worker cannot reuse the claim.
        consume_one_shot_recovery_claim(open_connection,durable_claim)
    except Exception:
        return {"closed_verified":False,"orders_sent":0,
                "requires_manual_exchange_review":True,
                "reason":"ONE_SHOT_CLAIM_CONSUMPTION_FAILED_NO_ORDER"}
    try:
        response=client.request("POST",PREFIX+"/orders",payload=order)
        rawid=(response.get("id_string") or response.get("id")) if isinstance(response,dict) else None
        close_id=str(rawid or "")
        if not ORDER_RE.fullmatch(close_id):
            raise AckUnknown("Close acknowledgment missing valid exchange identity")
        # IOC ack is not evidence of a filled reduce-only close.
        verified_order=client.request("GET",PREFIX+"/orders/"+close_id)
        if (not isinstance(verified_order,dict) or
            str(verified_order.get("id_string") or verified_order.get("id") or "")!=close_id or
            verified_order.get("contract")!=plan["contract"] or
            verified_order.get("text")!=order["text"] or
            verified_order.get("status")!="finished" or
            not (verified_order.get("is_close") is True or
                 verified_order.get("close") is True) or
            not (verified_order.get("is_reduce_only") is True or
                 verified_order.get("reduce_only") is True)):
            raise AckUnknown("Reduce-only close exchange order identity/readback unverified")
        snap=client.snapshot()
        if (snap.get("source") != "GATE_IO_LIVE_FUTURES" or
            not isinstance(snap.get("positions"),list) or
            not isinstance(snap.get("orders"),list) or
            not isinstance(snap.get("triggers"),list) or
            snap["orders"]):
            raise AckUnknown("Complete authenticated exchange close inventory unavailable")
        if any(p["contract"]==plan["contract"] for p in snap["positions"]):
            raise AckUnknown("Close not confirmed flat by exchange")
        if any((p.get("initial") or {}).get("contract")==plan["contract"]
               for p in snap["triggers"]):
            raise AckUnknown("Orphan native protective triggers remain")
        # Exchange flatness is NOT complete until the consumed claim is
        # durably marked verified. A failed DB write requires manual review.
        finalize_one_shot_recovery_claim(open_connection,durable_claim)
        return {"closed_verified":True,"orders_sent":1,
                "close_order_id":close_id,
                "claim_state_persisted":True,
                "final_recovery_certified":False}
    except Exception:
        # Any post-submit failure is ambiguous. Never retry the same close.
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
        if str(trigger.get("id_string") or trigger.get("id") or "") not in known_triggers:
            raise LiveSafetyError("Orphan or external native trigger")
    for p in snapshot["positions"]:
        row=tracked.get(p["contract"])
        if not row or row["state"]!="PROTECTED":
            raise LiveSafetyError("Open position without protected ledger")
        check=verify_ledger_position(row,p,snapshot["triggers"])
        if (not check["verified"] or
            check["stop_order_id"]!=str(row["stop_order_id"]) or
            check["take_order_id"]!=str(row["take_order_id"])):
            raise LiveSafetyError("Verified position missing linked native protection")
    # A SENDING or ACK_UNKNOWN row cannot be assumed flat just because the
    # position API momentarily returns no record.
    for row in open_rows:
        if row["state"] not in ("PROTECTED",):
            raise LiveSafetyError("Pending or uncertain order lifecycle blocks new entry")
        if sum(p['contract']==row['contract'] for p in snapshot['positions']) != 1:
            raise LiveSafetyError('Protected ledger position missing or ambiguous')
    return {"protected_open_positions":len(snapshot["positions"]),
            "unknown_orders":0,"unknown_triggers":0}


def execute_confirmed_signal(open_connection,plan,client=None,*,acceptance_approval_id=None):
    """Serialized journal-to-exchange flow. NOT callable under current source lock.

    Deliberately refuses to run if exchange-write source constant is false.
    Do NOT invoke from market data handlers or dashboard buttons.
    """
    if not EXCHANGE_WRITE_RELEASE:
        raise LiveSafetyError("SOURCE_LOCKED: live order dispatch not released")
    if acceptance_approval_id is not None and AUTOMATIC_WORKER_RELEASE:
        raise LiveSafetyError('Acceptance requires continuous automation to remain locked')
    client=client or GateLiveClient()
    with open_connection() as con:
        install(con)
        if acceptance_approval_id is not None:
            from live_execution_coordinator import LOCK_ID
            # Serialize inventory checks and reservation with every risk writer.
            con.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK_ID,))
        if control(con)["halted"]:
            raise LiveSafetyError("Production emergency halt active")
        if acceptance_approval_id is None:
            from production_bot_control import require_operator_on
            require_operator_on(con)
        else:
            # A reviewed one-shot acceptance does not require a certificate
            # from an earlier trade. All source/env/HALT/risk gates still apply.
            from acceptance_authorization import require_approval
            require_approval(con,acceptance_approval_id,plan,claim=True)
        # A fresh account snapshot must have no untracked orders before sending.
        snap=client.snapshot()
        metadata=client.request('GET',PREFIX+'/contracts/'+plan['contract'])
        verify_execution_preflight(plan,metadata,snap)
        existing=read_rows(con)
        assert_existing_protected_exposure(existing,snap)
        if acceptance_approval_id is not None and (
            snap['positions'] or snap['orders'] or snap['triggers'] or
            any(row['state']!='CLOSED_VERIFIED' for row in existing)
        ):
            raise LiveSafetyError('Acceptance requires a flat reconciled account')
        verified_equity,_=verified_account_equity(snap["account"])
        risk_reserve(con,plan,snap["positions"],verified_equity)
    event=plan["event_id"]
    entry_verified=False
    # Re-read operator intent after reservation and before exchange POST.
    with open_connection() as con:
        if acceptance_approval_id is None:
            from production_bot_control import require_operator_on
            require_operator_on(con)
        else:
            from acceptance_authorization import require_approval
            require_approval(con,acceptance_approval_id,plan)
            if control(con)['halted'] is not False:
                raise LiveSafetyError('Acceptance emergency HALT active before dispatch')
    if acceptance_approval_id is None:
        persist_state(open_connection,event,"SENDING")
    else:
        persist_state(open_connection,event,"SENDING",
                      acceptance_approval_id=acceptance_approval_id,
                      acceptance_plan=plan)
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
        require_linked_trigger_acknowledgments(stop,take,checked)
        persist_state(open_connection,event,"PROTECTED",
                      stop_order_id=checked["stop_order_id"],
                      take_order_id=checked["take_order_id"])
        return {"state":"PROTECTED","entry_order_id":order_id,
                "stop_order_id":checked["stop_order_id"],
                "take_order_id":checked["take_order_id"],
                "filled":True,"protection_verified":True}
    except Exception as exc:
        # A database failure *after* the entry POST can leave live exposure
        # without a journal record. Treat ALL ordinary runtime exceptions as
        # potentially post-submit, never as evidence that the entry failed.
        # This branch never retries an entry or transmits a close order.
        try:
            emergency_persist_halt(open_connection,"UNKNOWN_OR_UNPROTECTED_POST_ACK",event)
        except Exception as halt_exc:
            # Do not claim durable HALT or attempt any exchange write when
            # the database cannot confirm its emergency control record.
            return {"state":"HALT_PERSISTENCE_UNVERIFIED",
                    "reason":type(exc).__name__,
                    "halt_error":type(halt_exc).__name__,
                    "halt_persisted":False,
                    "exchange_position_may_be_open":True,
                    "emergency_close":{"closed_verified":False,
                                       "reason":"DURABLE_HALT_UNAVAILABLE_NO_CLOSE"},
                    "automatic_retry":False,
                    "requires_manual_exchange_review":True}
        # An automatic emergency close needs a persisted one-shot close claim
        # and transaction-specific authorization. Neither is implemented here.
        # A verified entry alone does NOT confer permission to transmit a close.
        return {"state":"HALTED_UNKNOWN","reason":type(exc).__name__,
                "halt_persisted":True,
                "exchange_position_may_be_open":True,
                "entry_fill_readback_verified":entry_verified,
                "emergency_close":{"closed_verified":False,
                                   "reason":"ONE_SHOT_CLOSE_NOT_CERTIFIED"},
                "automatic_retry":False,
                "requires_manual_exchange_review":True}

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
    if (result["reconciliation"]["requires_halt"] or result["untracked_open_orders"]
            or result['untracked_open_triggers']):
        return {"status":"HALTED_REVIEW_REQUIRED","orders_resubmitted":0,"readback":result}
    return {"status":"RECONCILED_READ_ONLY","orders_resubmitted":0,"readback":result}


