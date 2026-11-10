"""Owner-reviewed one-shot recovery using Gate's documented reduce-only close.

All live writes remain source locked. No automatic recovery worker is added.
Open orders/triggers, partial fills or unknown entry IDs require exchange-side
manual intervention. This module never cancels protection or clears HALT.
"""
import json
import os
import time
from datetime import datetime,timezone
from decimal import Decimal
import live_futures_engine as engine
import live_execution_orchestrator as lifecycle
from live_execution_coordinator import control,LOCK_ID
from live_emergency_one_shot_claim import evaluate_one_shot_close_candidate,claim_one_shot_recovery
from controlled_acceptance import canonical_digest

RECOVERY_RELEASE=False
LEDGER_SQL='''SELECT event_id,contract,side,position_mode,state,contracts,
 client_text,entry_order_id,entry_price,stop_loss,take_profit
 FROM luxalgo_prod_execution_ledger WHERE event_id=%s'''


def require_release():
    if not RECOVERY_RELEASE or not engine.EXCHANGE_WRITE_RELEASE:
        raise engine.LiveSafetyError('RECOVERY_SOURCE_LOCKED')
    if lifecycle.AUTOMATIC_WORKER_RELEASE:
        raise engine.LiveSafetyError('Continuous worker must remain locked during recovery')
    if any(os.getenv(k)!='true' for k in ('LIVE_TRADING_ENABLED','ORDERS_ENABLED',
                                        'LIVE_ORDERS_SEPARATELY_AUTHORIZED')):
        raise engine.LiveSafetyError('Recovery exchange-write permissions missing')


def prepare(open_connection,event_id,client=None):
    """GET-only exchange review of one positively identified unprotected fill."""
    client=client or engine.GateLiveClient()
    with open_connection() as con:
        ctrl=control(con)
        with con.cursor() as cur:
            cur.execute(LEDGER_SQL,(event_id,));row=cur.fetchone()
    if not row or ctrl['halted'] is not True:
        raise engine.LiveSafetyError('Known execution and active durable HALT required')
    row=dict(row)
    entry_id=str(row.get('entry_order_id') or '')
    if not engine.ORDER_RE.fullmatch(entry_id):
        raise engine.LiveSafetyError('Unknown bot entry ID; inspect the exchange manually')
    order=client.request('GET',engine.PREFIX+'/orders/'+entry_id)
    qty=engine.dec(row.get('contracts'),'contracts')
    if qty!=qty.to_integral_value():
        raise engine.LiveSafetyError('Whole recovery contract count required')
    signed=qty if row['side']=='LONG' else -qty
    if (not isinstance(order,dict)
            or str(order.get('id_string') or order.get('id') or '')!=entry_id
            or order.get('contract')!=row['contract'] or order.get('text')!=row['client_text']
            or order.get('status')!='finished'
            or Decimal(str(order.get('size')))!=signed
            or Decimal(str(order.get('left')))!=0):
        raise engine.LiveSafetyError('Entry fill identity is incomplete; manual exchange review required')
    engine.dec(order.get('fill_price'),'fill price')
    snap=client.snapshot()
    timestamp=snap.get('fetched_at')
    if type(timestamp) is not int:
        raise engine.LiveSafetyError('Fresh exchange timestamp required')
    candidate=dict(snap,readback_utc=datetime.fromtimestamp(timestamp,timezone.utc),
                   position_mode=snap.get('account',{}).get('position_mode'),halt_active=True)
    checked=evaluate_one_shot_close_candidate(row,candidate,
        now=datetime.fromtimestamp(int(time.time()),timezone.utc))
    if not checked['eligible_for_atomic_claim']:
        raise engine.LiveSafetyError(checked['reason'])
    plan=dict(event_id=row['event_id'],contract=row['contract'],side=row['side'],
              position_mode=row['position_mode'],contracts=str(qty),client_text=row['client_text'])
    # Serialize numbers as exact strings; no Decimal objects in the review.
    review=dict(plan=plan,entry_order_id=entry_id,prepared_epoch=int(time.time()),
                snapshot_epoch=timestamp,close_order=lifecycle.exact_reduce_only_close_order(plan))
    return dict(status='RECOVERY_REVIEW_ONLY',review=review,
                review_digest=canonical_digest(review),orders_submitted=0,can_execute=False)


def submit_once(open_connection,prepared,review_digest,client=None):
    """Manual submission only; source checks happen before DB/network.

    A user acknowledgment and session-bound stored review must be enforced
    by the HTTP caller. No code path from alerts invokes this function.
    """
    require_release()
    if (not isinstance(prepared,dict) or prepared.get('status')!='RECOVERY_REVIEW_ONLY'
            or prepared.get('review_digest')!=review_digest
            or canonical_digest(prepared.get('review'))!=review_digest):
        raise engine.LiveSafetyError('Exact recovery review digest required')
    review=prepared['review'];plan=review['plan']
    def fresh_review():
        now=int(time.time())
        if (type(review.get('prepared_epoch')) is not int
                or not 0<=now-review['prepared_epoch']<30):
            raise engine.LiveSafetyError('Recovery review expired')
    fresh_review()
    client=client or engine.GateLiveClient()
    fresh=prepare(open_connection,plan['event_id'],client)['review']
    if fresh['plan']!=plan or fresh['entry_order_id']!=review['entry_order_id']:
        raise engine.LiveSafetyError('Recovery position identity changed')
    fresh_review()
    with open_connection() as con:
        with con.cursor() as cur:
            cur.execute('SELECT pg_advisory_xact_lock(%s)',(LOCK_ID,))
            require_release();fresh_review()
            claim=claim_one_shot_recovery(con,event_id=plan['event_id'],contract=plan['contract'],
                approval_reference='owner-reviewed:'+review_digest,
                close_text=review['close_order']['text'])
    # The existing close helper re-reads current exposure, consumes the claim
    # durably before its only POST, then verifies close identity and flatness.
    result=lifecycle.emergency_close_once(client,plan,durable_claim=claim,
        open_connection=open_connection,close_order=review['close_order'])
    if not result.get('closed_verified'):
        return dict(result,continuous_trading_enabled=False,certified=False,halt_active=True)
    try:
        with open_connection() as con:
            with con.cursor() as cur:
                cur.execute('SELECT pg_advisory_xact_lock(%s)',(LOCK_ID,))
                if control(con)['halted'] is not True:
                    raise engine.LiveSafetyError('Durable HALT changed during recovery')
                cur.execute('''UPDATE luxalgo_prod_execution_ledger l
                    SET state='CLOSED_VERIFIED',detail=detail || %s::jsonb,updated_at=NOW()
                    WHERE l.event_id=%s AND l.contract=%s AND l.entry_order_id=%s
                    AND l.state IN ('ENTRY_CONFIRMED','PROTECTION_PENDING','HALTED')
                    AND EXISTS (SELECT 1 FROM luxalgo_prod_emergency_close_claims c
                        WHERE c.event_id=l.event_id AND c.contract=l.contract
                        AND c.approval_reference=%s AND c.state='FLAT_READBACK_VERIFIED')''',
                    (json.dumps({'recovery_close_order_id':result['close_order_id']}),
                     plan['event_id'],plan['contract'],review['entry_order_id'],claim['approval_reference']))
                if cur.rowcount!=1:
                    raise engine.LiveSafetyError('Verified close journal persistence failed')
    except Exception:
        return dict(result,ledger_closed_verified=False,requires_manual_exchange_review=True,
                    certified=False,continuous_trading_enabled=False,halt_active=True)
    return dict(result,ledger_closed_verified=True,certified=False,
                continuous_trading_enabled=False,halt_active=True)
