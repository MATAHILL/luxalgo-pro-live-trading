"""Manually submitted, single-order acceptance path. Never run by alerts/workers.

This revision is SOURCE LOCKED. Preparation is GET-only. Submission is built
for later reviewed release; it never clears HALT or enables continuous trading.
The caller must supply an exact scope and review its canonical digest.
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import time
from decimal import Decimal

import live_futures_engine as engine
import live_execution_orchestrator as lifecycle
from live_execution_coordinator import LOCK_ID, control, halt, risk_reserve

ACCEPTANCE_RELEASE = False
CAPS = {'max_margin':'1.70', 'max_notional':'8.30', 'max_modeled_loss':'0.10'}
SCHEMA = """CREATE TABLE IF NOT EXISTS luxalgo_single_acceptance (
 scope_id TEXT PRIMARY KEY, event_id TEXT NOT NULL UNIQUE,
 review_digest TEXT NOT NULL, review JSONB NOT NULL, state TEXT NOT NULL
 CHECK(state IN ('SENDING','PROTECTED_REVIEW_REQUIRED','UNKNOWN_REVIEW_REQUIRED')),
 evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"""


def canonical_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def epoch(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise engine.LiveSafetyError('Invalid '+name)
    return value


def validate_scope(scope, now):
    if not isinstance(scope, dict):
        raise engine.LiveSafetyError('Exact owner scope required')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{16,80}', str(scope.get('scope_id',''))):
        raise engine.LiveSafetyError('Unique scope ID required')
    if not engine.CONTRACT_RE.fullmatch(str(scope.get('contract',''))):
        raise engine.LiveSafetyError('Exact USDT contract required')
    if scope.get('side') not in ('LONG','SHORT'):
        raise engine.LiveSafetyError('Exact side required')
    start=epoch(scope.get('start_epoch'),'start')
    end=epoch(scope.get('end_epoch'),'end')
    if not 0 < end-start <= 900 or not start <= now < end:
        raise engine.LiveSafetyError('Outside bounded acceptance window')
    if type(scope.get('contracts')) is not int or scope['contracts'] != 1:
        raise engine.LiveSafetyError('Exactly one whole contract required')
    for key, ceiling in CAPS.items():
        if engine.dec(scope.get(key),key) > Decimal(ceiling):
            raise engine.LiveSafetyError('Acceptance ceiling exceeded: '+key)
    if engine.dec(scope.get('leverage'),'leverage') > 5:
        raise engine.LiveSafetyError('Acceptance leverage exceeds 5')
    return scope


def require_release():
    # Evaluate actual module constants, not a caller-supplied bypass parameter.
    if not ACCEPTANCE_RELEASE or not engine.EXCHANGE_WRITE_RELEASE:
        raise engine.LiveSafetyError('ACCEPTANCE_SOURCE_LOCKED')
    if lifecycle.AUTOMATIC_WORKER_RELEASE:
        raise engine.LiveSafetyError('Continuous worker must remain source locked')
    if any(os.getenv(key) != 'true' for key in (
            'LIVE_TRADING_ENABLED','ORDERS_ENABLED','LIVE_ORDERS_SEPARATELY_AUTHORIZED')):
        raise engine.LiveSafetyError('Explicit exchange-write environment permission missing')


def prepare(scope, signal, client, *, now=None):
    """Fresh original signal -> one-contract review; only exchange GETs allowed.

    signal is a stored handoff with source_bar_id, declared order_type, and
    original levels. Never synthesize a signal timestamp or round SL/TP.
    """
    from live_signal_identity import validated_bar_id
    clock=(lambda:int(time.time())) if now is None else (lambda:epoch(now,'now'))
    now=clock()
    validate_scope(scope,now)
    if (not isinstance(signal,dict) or signal.get('order_type') != 'MARKET'
            or not validated_bar_id(signal.get('source_bar_id'))
            or signal.get('contract') != scope['contract']
            or signal.get('side') != scope['side']):
        raise engine.LiveSafetyError('Matching original qualified LuxAlgo signal required')
    snap=client.snapshot()
    meta=client.request('GET',engine.PREFIX+'/contracts/'+scope['contract'])
    now=clock()
    validate_scope(scope,now)
    if (not isinstance(snap,dict) or snap.get('source') != 'GATE_IO_LIVE_FUTURES'
            or not 0 <= now-epoch(snap.get('fetched_at'),'snapshot') <= 30
            or any(snap.get(key) != [] for key in ('positions','orders','triggers'))):
        raise engine.LiveSafetyError('Fresh flat authenticated inventory required')
    equity,available=engine.verified_account_equity(snap['account'])
    enriched=dict(signal,position_mode=snap['account'].get('position_mode'),
                  leverage=scope['leverage'])
    plan=engine.build_plan(enriched,meta,equity,available,[],now=now)
    # The normal planner returns the maximum risk-sized count. Acceptance is
    # explicitly one lot; scale its modeled loss down, never round a lot up.
    plan['estimated_risk_usdt']=str(Decimal(plan['estimated_risk_usdt']) / plan['contracts'])
    plan['contracts']=1
    entry=engine.dec(plan['entry']); mark=engine.dec(meta.get('mark_price'))
    multiplier=engine.dec(meta.get('quanto_multiplier'))
    slip=Decimal(plan['max_entry_slippage_ratio'])
    fee=engine.dec(meta.get('taker_fee_rate'))
    if abs(mark-entry) > entry*slip:
        raise engine.LiveSafetyError('Live mark outside reviewed signal slippage tolerance')
    notional=max(entry,mark)*multiplier*(1+slip)
    margin=notional/Decimal(plan['leverage'])+notional*fee
    risk=multiplier*(abs(entry-Decimal(plan['sl']))+max(entry,mark)*(2*fee+2*slip))
    plan['estimated_risk_usdt']=str(risk)
    plan['modeled_stop_risk_usdt']=str(abs(entry-Decimal(plan['sl']))*multiplier)
    plan['modeled_fee_slippage_usdt']=str(risk-Decimal(plan['modeled_stop_risk_usdt']))
    if (notional > Decimal(scope['max_notional']) or margin > Decimal(scope['max_margin'])
            or risk > min(Decimal(scope['max_modeled_loss']),equity*Decimal('0.005'))):
        raise engine.LiveSafetyError('One contract exceeds scoped financial limits')
    engine.verify_execution_preflight(plan,meta,snap,now=now)
    review=dict(scope=dict(scope),plan=plan,prepared_epoch=now,
                snapshot_epoch=snap['fetched_at'],equity_usdt=str(equity),
                notional_usdt=str(notional),margin_usdt=str(margin),
                modeled_loss_usdt=str(risk),source_bar_id=signal['source_bar_id'])
    return dict(status='REVIEW_ONLY_NO_ORDER',review=review,
                review_digest=canonical_digest(review),orders_submitted=0,
                can_execute=False,continuous_trading_enabled=False)


def claim_and_reserve(open_connection, review, digest):
    """One atomic cross-worker claim and production ledger reservation.

    Uses the existing global advisory lock. No operator certification is
    invented: this is an isolated acceptance claim, not the automatic path.
    Any duplicate, failed DB commit, missing control or active HALT stops POST.
    """
    with open_connection() as con:
        con.execute(SCHEMA)
        with con.cursor() as cur:
            cur.execute('SELECT pg_advisory_xact_lock(%s)',(LOCK_ID,))
            require_release()
            current=int(time.time())
            validate_scope(review['scope'],current)
            if (not 0 <= current-review['prepared_epoch'] <= 30
                    or not 0 <= current-review['snapshot_epoch'] <= 30
                    or not 0 <= current-review['plan']['received_epoch'] <= 60):
                raise engine.LiveSafetyError('Acceptance review or signal expired')
            if control(con)['halted'] is not False:
                raise engine.LiveSafetyError('Global HALT remains active')
            cur.execute("SELECT 1 FROM luxalgo_prod_execution_ledger WHERE state<>'CLOSED_VERIFIED' LIMIT 1")
            if cur.fetchone():
                raise engine.LiveSafetyError('Unresolved production execution exists')
            cur.execute("""INSERT INTO luxalgo_single_acceptance(scope_id,event_id,review_digest,review,state)
                VALUES (%s,%s,%s,%s::jsonb,'SENDING') ON CONFLICT DO NOTHING RETURNING scope_id""",
                (review['scope']['scope_id'],review['plan']['event_id'],digest,json.dumps(review)))
            if not cur.fetchone():
                raise engine.LiveSafetyError('Acceptance scope or signal already consumed')
            risk_reserve(con,review['plan'],[],Decimal(review['equity_usdt']))
            cur.execute("""UPDATE luxalgo_prod_execution_ledger SET state='SENDING',updated_at=NOW()
                WHERE event_id=%s AND state='RESERVED'""",(review['plan']['event_id'],))
            if cur.rowcount != 1:
                raise engine.LiveSafetyError('Acceptance ledger dispatch claim failed')


def record_outcome(open_connection, scope_id, outcome):
    with open_connection() as con:
        with con.cursor() as cur:
            cur.execute("""UPDATE luxalgo_single_acceptance SET state=%s,evidence=%s::jsonb,updated_at=NOW()
                WHERE scope_id=%s AND state='SENDING'""",
                (outcome['state'],json.dumps(outcome),scope_id))
            if cur.rowcount != 1:
                raise engine.LiveSafetyError('Acceptance evidence persistence failed')


def submit_once(open_connection, prepared, review_digest, signal, client=None):
    """Manual integration entry point used by the authenticated review interface.

    Never invoked by development tests against a real exchange. Source and
    environment locks are checked BEFORE DB or network. No retry or auto-close.
    """
    require_release()
    if (not isinstance(prepared,dict) or prepared.get('status') != 'REVIEW_ONLY_NO_ORDER'
            or canonical_digest(prepared.get('review')) != review_digest
            or prepared.get('review_digest') != review_digest):
        raise engine.LiveSafetyError('Exact reviewed digest required')
    now=int(time.time())
    review=prepared['review']; plan=review['plan']
    if not 0 <= now-epoch(review.get('prepared_epoch'),'prepared') <= 30:
        raise engine.LiveSafetyError('Reviewed preview expired')
    client=client or engine.GateLiveClient()
    # Rebuild from stored signal and fresh account/metadata; reject any material
    # price, setting, size or scope change rather than silently revise approval.
    fresh=prepare(review['scope'],signal,client)['review']
    if fresh['plan'] != plan or fresh['source_bar_id'] != review['source_bar_id']:
        raise engine.LiveSafetyError('Reviewed plan changed; review a new preview')
    # Persist exactly what the user reviewed. Fresh readbacks validate this
    # plan but cannot renew its original expiry or change its audit digest.
    claim_and_reserve(open_connection,review,review_digest)
    # Claim is committed before the sole entry attempt. Any later failure is
    # uncertain exposure, even if no acknowledgment was received. Never replay.
    try:
        # Serialize the final entry attempt against HALT. The claim already
        # committed; a failed transaction/ack never makes it reusable.
        with open_connection() as con:
            with con.cursor() as cur:
                cur.execute('SELECT pg_advisory_xact_lock(%s)',(LOCK_ID,))
                require_release()
                current=int(time.time())
                validate_scope(review['scope'],current)
                if (not 0 <= current-plan['received_epoch'] <= 60
                        or not 0 <= current-fresh['snapshot_epoch'] <= 30
                        or control(con)['halted'] is not False):
                    raise engine.LiveSafetyError('HALT or expired signal before entry')
                protection_started=time.monotonic()
                ack=client.submit_entry(plan)
        order_id,filled=lifecycle.require_full_entry_readback(client,plan,ack)
        require_isolated_position(plan,filled,protected=False)
        lifecycle.persist_state(open_connection,plan['event_id'],'ENTRY_CONFIRMED',entry_order_id=order_id)
        lifecycle.persist_state(open_connection,plan['event_id'],'PROTECTION_PENDING')
        stop=client.submit_trigger(plan,'SL')
        take=client.submit_trigger(plan,'TP')
        snapshot=client.snapshot()
        require_isolated_position(plan,snapshot,protected=True)
        checked=engine.evaluate_protection(plan,snapshot['triggers'])
        lifecycle.require_linked_trigger_acknowledgments(stop,take,checked)
        if time.monotonic()-protection_started > 5:
            raise engine.LiveSafetyError('Native protection readback exceeded acceptance deadline')
        lifecycle.persist_state(open_connection,plan['event_id'],'PROTECTED',
            stop_order_id=checked['stop_order_id'],take_order_id=checked['take_order_id'])
        outcome=dict(state='PROTECTED_REVIEW_REQUIRED',entry_order_id=order_id,
                     stop_order_id=checked['stop_order_id'],take_order_id=checked['take_order_id'],
                     automatic_retry=False,continuous_trading_enabled=False,certified=False)
        record_outcome(open_connection,review['scope']['scope_id'],outcome)
        with open_connection() as con:
            halt(con,'SINGLE_ACCEPTANCE_COMPLETE_REVIEW_REQUIRED')
        return outcome
    except Exception as exc:
        outcome=dict(state='UNKNOWN_REVIEW_REQUIRED',reason=type(exc).__name__,
                     automatic_retry=False,requires_manual_exchange_review=True,
                     continuous_trading_enabled=False,certified=False)
        try:
            lifecycle.emergency_persist_halt(open_connection,'SINGLE_ACCEPTANCE_UNKNOWN',plan['event_id'])
            outcome['halt_persisted']=True
        except Exception:
            outcome['halt_persisted']=False
        try:
            record_outcome(open_connection,review['scope']['scope_id'],outcome)
        except Exception:
            outcome['evidence_persisted']=False
        return outcome


def require_isolated_position(plan,snapshot,*,protected):
    """A test cannot certify amid a mode change, extra exposure or orphan order."""
    expected=1 if plan['side']=='LONG' else -1
    if (snapshot.get('source') != 'GATE_IO_LIVE_FUTURES'
            or not all(isinstance(snapshot.get(key),list) for key in ('positions','orders','triggers'))
            or snapshot.get('account',{}).get('position_mode') != plan['position_mode']
            or snapshot.get('orders') != [] or len(snapshot.get('positions',[])) != 1
            or len(snapshot.get('triggers',[])) != (2 if protected else 0)):
        raise engine.LiveSafetyError('Acceptance exposure changed or unknown')
    position=snapshot['positions'][0]
    if position.get('contract') != plan['contract'] or Decimal(str(position.get('size'))) != expected:
        raise engine.LiveSafetyError('Acceptance position changed')
