"""Gate.io Futures authenticated reconciliation audit, READ ONLY (Python 3.9+).

Run: py gate_readonly_reconcile_UPGRADED.py
Optionally set GATE_API_KEY/GATE_API_SECRET in a trusted environment for account snapshot.
Only HTTP GET is permitted. No live trading, order submission or cancellation.
Protective coverage is NEVER declared verified without an independently validated
exchange-side order/position linkage, full size coverage and freshness check.
"""
import hashlib
import hmac
import json
import os
import time
from collections import Counter
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = 'https://api.gateio.ws'
PREFIX = '/api/v4'
ENDPOINTS = (
    ('positions', '/futures/usdt/positions', ''),
    ('open_orders', '/futures/usdt/orders', 'status=open'),
    ('trigger_orders', '/futures/usdt/price_orders', 'status=open'),
)


def signed_headers(path, query, key, secret):
    timestamp = str(int(time.time()))
    digest = hashlib.sha512(b'').hexdigest()
    signing = '\n'.join(['GET', PREFIX + path, query, digest, timestamp])
    signature = hmac.new(secret.encode(), signing.encode(), hashlib.sha512).hexdigest()
    return {'KEY': key, 'SIGN': signature, 'Timestamp': timestamp,
            'Accept': 'application/json', 'User-Agent': 'LuxAlgo-ReadOnly-Audit/2.0'}


def fetch_readonly(path, query, key, secret):
    if (path, query) not in {(p, q) for _, p, q in ENDPOINTS}:
        raise RuntimeError('Unapproved read-only endpoint')
    url = BASE + PREFIX + path + ('?' + query if query else '')
    req = Request(url, headers=signed_headers(path, query, key, secret), method='GET')
    try:
        with urlopen(req, timeout=12) as response:
            data = json.load(response)
        if not isinstance(data, list):
            raise RuntimeError('Unexpected API response format')
        if not all(isinstance(item, dict) for item in data):
            raise RuntimeError('Unexpected API item format')
        return data
    except HTTPError as exc:
        raise RuntimeError('Gate.io HTTP status ' + str(exc.code)) from None
    except (URLError, TimeoutError, OSError):
        raise RuntimeError('Gate.io network request failed') from None


def _number(value):
    try:
        n = Decimal(str(value))
        return n if n.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _contract(item):
    return str(item.get('contract') or '').upper().strip()


def _side(position):
    size = _number(position.get('size'))
    if size is None or size == 0:
        return None
    # Gate.io hedged positions may expose mode fields; do not silently infer
    # direction if a conflicting explicit position side is present.
    raw = str(position.get('mode') or position.get('side') or '').lower()
    inferred = 'LONG' if size > 0 else 'SHORT'
    if raw in ('long', 'short') and raw.upper() != inferred:
        return 'AMBIGUOUS'
    return inferred


def _trigger_target(trigger):
    kind = str(trigger.get('order_type') or '').lower()
    if 'close-long' in kind:
        return 'LONG'
    if 'close-short' in kind:
        return 'SHORT'
    initial = trigger.get('initial') or {}
    auto = str(initial.get('auto_size') or '').lower() if isinstance(initial, dict) else ''
    if auto == 'close_long':
        return 'LONG'
    if auto == 'close_short':
        return 'SHORT'
    return 'UNKNOWN'


def summarize(data):
    if any(name not in data or not isinstance(data[name], list) or
           not all(isinstance(x, dict) for x in data[name]) for name, _, _ in ENDPOINTS):
        raise ValueError('Incomplete/unusable exchange snapshot; reconciliation blocked')
    active = []
    invalid_positions = 0
    for p in data['positions']:
        n = _number(p.get('size'))
        if n is None:
            invalid_positions += 1
        elif n != 0:
            active.append(p)
    orders = data['open_orders']
    triggers = data['trigger_orders']
    trigger_counts = Counter()
    unknown_trigger_count = 0
    for t in triggers:
        init = t.get('initial')
        contract = _contract(init) if isinstance(init, dict) else ''
        side = _trigger_target(t)
        if contract and side in ('LONG', 'SHORT'):
            trigger_counts[(contract, side)] += 1
        else:
            unknown_trigger_count += 1

    results = []
    for p in active:
        contract = _contract(p)
        side = _side(p)
        matching = trigger_counts.get((contract, side), 0)
        pending = sum(1 for o in orders if _contract(o) == contract)
        issues = []
        if not contract or side == 'AMBIGUOUS':
            issues.append('Incomplete position identity or conflicting direction')
        if not matching:
            issues.append('No direction-matched open protection trigger identified')
        else:
            issues.append('Candidate trigger(s) present; SL vs TP, coverage size, trigger validity and position linkage NOT verified')
        if pending:
            issues.append('Open regular orders may change future exposure')
        results.append({
            'contract': contract or 'UNKNOWN', 'position_side': side or 'UNKNOWN',
            'open_orders_for_contract': pending,
            'candidate_direction_matched_triggers': matching,
            'protective_coverage': 'NOT_VERIFIED', 'issues': issues,
        })
    warnings = []
    if invalid_positions:
        warnings.append('Some position sizes were unreadable; do not trust active count')
    if unknown_trigger_count:
        warnings.append('Some trigger orders could not be classified by contract and direction')
    if active:
        warnings.append('No bot event-to-exchange order mapping supplied; ownership cannot be established')
    if len(active) > 5:
        warnings.append('Observed more than five active exchange positions; investigate manually')
    warnings.append('GET snapshots are non-atomic; states may change immediately after fetching')
    return {
        'status': 'READ_ONLY_SNAPSHOT_REVIEW_REQUIRED',
        'exchange': 'Gate.io USDT Futures',
        'active_positions': len(active),
        'unreadable_position_rows': invalid_positions,
        'open_orders': len(orders),
        'open_trigger_orders': len(triggers),
        'active_contracts': sorted({_contract(p) for p in active if _contract(p)}),
        'positions_review': results,
        'unknown_trigger_count': unknown_trigger_count,
        'bot_order_reconciliation': 'NOT_VERIFIED_NO_BOT_EXCHANGE_ID_MAPPING',
        'protective_sl_tp_coverage': 'NOT_VERIFIED',
        'safe_to_trade': False,
        'live_trading': False,
        'orders_enabled': False,
        'warnings': warnings,
    }


def offline_tests():
    fixture = {
        'positions': [{'contract': 'BTC_USDT', 'size': '0'},
                      {'contract': 'SOL_USDT', 'size': '2'},
                      {'contract': 'ETH_USDT', 'size': '-1'}],
        'open_orders': [{'contract': 'SOL_USDT', 'id': 'fixture'}],
        'trigger_orders': [
            {'initial': {'contract': 'SOL_USDT'}, 'order_type': 'close-long-position',
             'status': 'open', 'trigger': {'price': '10', 'rule': 2}},
            {'initial': {'contract': 'ETH_USDT'}, 'order_type': 'close-short-position',
             'status': 'open', 'trigger': {'price': '100', 'rule': 1}}],
    }
    result = summarize(fixture)
    assert result['active_positions'] == 2
    assert result['open_orders'] == 1
    assert result['open_trigger_orders'] == 2
    assert result['active_contracts'] == ['ETH_USDT', 'SOL_USDT']
    assert all(r['candidate_direction_matched_triggers'] == 1 for r in result['positions_review'])
    assert all(r['protective_coverage'] == 'NOT_VERIFIED' for r in result['positions_review'])
    assert result['safe_to_trade'] is False and result['orders_enabled'] is False
    assert len(signed_headers('/futures/usdt/orders', 'status=open', 'test', 'test')['SIGN']) == 128
    empty = summarize({'positions': [], 'open_orders': [], 'trigger_orders': []})
    assert empty['safe_to_trade'] is False
    try:
        summarize({'positions': [], 'open_orders': []})
    except ValueError:
        pass
    else:
        raise AssertionError('Missing snapshots must fail closed')
    return 'PASSED'


def main():
    print(json.dumps({'offline_tests': offline_tests(), 'real_order_submission': 'BLOCKED'}, indent=2))
    key = os.environ.get('GATE_API_KEY', '').strip()
    secret = os.environ.get('GATE_API_SECRET', '').strip()
    if not (key and secret):
        print(json.dumps({'authenticated_snapshot': 'NOT_RUN',
                          'reason': 'Read-only credentials not configured in this process',
                          'live_trading': False, 'orders_enabled': False}, indent=2))
        return
    try:
        data = {name: fetch_readonly(path, query, key, secret)
                for name, path, query in ENDPOINTS}
        print(json.dumps(summarize(data), indent=2))
    except (RuntimeError, ValueError, TypeError) as exc:
        print(json.dumps({'authenticated_snapshot': 'FAILED', 'reason': str(exc),
                          'live_trading': False, 'orders_enabled': False}, indent=2))


if __name__ == '__main__':
    main()
