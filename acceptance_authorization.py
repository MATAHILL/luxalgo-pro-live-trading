"""Single-use, event-bound acceptance authorization; no exchange or release writes.

An operator must create this record explicitly. It replaces only the production
certificate requirement for one attempt, never source/env locks or emergency HALT.
Approval creation is deliberately not exposed through the webhook or dashboard.
"""
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json

from live_futures_engine import LiveSafetyError

SCHEMA = """
CREATE TABLE IF NOT EXISTS luxalgo_acceptance_authorizations (
 approval_id TEXT PRIMARY KEY,
 event_id TEXT NOT NULL UNIQUE,
 plan_digest TEXT NOT NULL,
 starts_at TIMESTAMPTZ NOT NULL,
 expires_at TIMESTAMPTZ NOT NULL,
 operator_reference TEXT NOT NULL,
 claimed_at TIMESTAMPTZ,
 revoked BOOLEAN NOT NULL DEFAULT FALSE,
 CHECK (expires_at > starts_at)
);
"""
FIELDS = ('event_id', 'contract', 'side', 'timeframe', 'received_epoch',
          'contracts', 'entry', 'sl', 'tp', 'leverage', 'position_mode',
          'estimated_risk_usdt', 'max_entry_slippage_ratio')


def fingerprint(plan):
    try:
        data = {key: str(plan[key]) for key in FIELDS}
    except (KeyError, TypeError) as exc:
        raise LiveSafetyError('Incomplete acceptance plan') from exc
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def validate_scope(plan, starts_at, expires_at, now):
    if any(not isinstance(t, datetime) or t.tzinfo is None
           for t in (starts_at, expires_at, now)):
        raise LiveSafetyError('Acceptance requires timezone-aware timestamps')
    if not timedelta(0) < expires_at - starts_at <= timedelta(minutes=15):
        raise LiveSafetyError('Acceptance window must be positive and at most 15 minutes')
    if not starts_at <= now < expires_at:
        raise LiveSafetyError('Acceptance approval outside its window')
    try:
        count = Decimal(str(plan['contracts']))
        risk = Decimal(str(plan['estimated_risk_usdt']))
        leverage = Decimal(str(plan['leverage']))
        slip = Decimal(str(plan['max_entry_slippage_ratio']))
        if any(not n.is_finite() for n in (count, risk, leverage, slip)):
            raise ValueError('Non-finite scope')
        if count != 1 or not 0 < risk or not 0 < leverage <= 5 or not 0 <= slip <= Decimal('0.001'):
            raise ValueError('Scope exceeds one-contract acceptance limits')
        if plan['side'] not in ('LONG', 'SHORT'):
            raise ValueError('Invalid direction')
        if plan['contract'] not in ('XRP_USDT', 'NEAR_USDT', 'BNB_USDT', 'INJ_USDT', 'SAND_USDT'):
            raise ValueError('Contract outside configured acceptance scope')
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise LiveSafetyError('Invalid one-contract acceptance scope') from exc
    return fingerprint(plan)


def create_approval(con, approval_id, plan, starts_at, expires_at, operator_reference, *, now=None):
    """Persist an exact reviewed plan. Caller owns commit; never clears HALT.

    This function is for an authenticated operator tool, not an incoming alert.
    It does not verify an exchange fill or grant continuous trading permission.
    """
    now = now or datetime.now(timezone.utc)
    digest = validate_scope(plan, starts_at, expires_at, now)
    if not isinstance(approval_id, str) or not 8 <= len(approval_id) <= 128:
        raise LiveSafetyError('Invalid acceptance approval identity')
    if not isinstance(operator_reference, str) or not operator_reference.strip() or len(operator_reference) > 200:
        raise LiveSafetyError('Audited operator reference required')
    con.execute(SCHEMA)
    con.execute('''INSERT INTO luxalgo_acceptance_authorizations
        (approval_id,event_id,plan_digest,starts_at,expires_at,operator_reference)
        VALUES (%s,%s,%s,%s,%s,%s)''',
        (approval_id, plan['event_id'], digest, starts_at, expires_at, operator_reference))
    return {'approval_id': approval_id, 'event_id': plan['event_id'],
            'plan_digest': digest, 'orders_sent': 0, 'certified': False}


def require_approval(con, approval_id, plan, *, claim=False, now=None):
    """Lock the record and consume exactly once in the reservation transaction.

    A second read immediately before POST rechecks expiration and revocation.
    Claimed approvals never revert, including after timeouts or failed entries.
    """
    with con.cursor() as cur:
        cur.execute('''SELECT * FROM luxalgo_acceptance_authorizations
                       WHERE approval_id=%s FOR UPDATE''', (approval_id,))
        row = cur.fetchone()
        if row is None or row['revoked'] is not False:
            raise LiveSafetyError('Acceptance approval missing or revoked')
        # Row-lock contention can outlast the approval window. Sample the
        # clock after acquiring the lock so a stale timestamp cannot dispatch.
        now = now or datetime.now(timezone.utc)
        digest = validate_scope(plan, row['starts_at'], row['expires_at'], now)
        if row['event_id'] != plan['event_id'] or row['plan_digest'] != digest:
            raise LiveSafetyError('Acceptance plan differs from operator approval')
        if claim:
            if row['claimed_at'] is not None:
                raise LiveSafetyError('Acceptance approval already consumed')
            cur.execute('''UPDATE luxalgo_acceptance_authorizations SET claimed_at=%s
                           WHERE approval_id=%s AND claimed_at IS NULL AND revoked=FALSE''',
                        (now, approval_id))
            if cur.rowcount != 1:
                raise LiveSafetyError('Acceptance claim failed')
        elif row['claimed_at'] is None:
            raise LiveSafetyError('Acceptance approval has not been claimed')
    return True
