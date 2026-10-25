"""Gate.io futures integration DEVELOPMENT ONLY.

No API secrets, exchange order endpoints, POST/PATCH/DELETE requests, or
other account-changing operations are implemented here. Safe to run offline.
Review in the live-execution-dev branch only.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_DOWN
import json
import re
from urllib.request import Request, urlopen

LIVE_TRADING_ENABLED = False
ORDERS_ENABLED = False
MAX_OPEN_POSITIONS = 5
RISK_PER_TRADE_PERCENT = Decimal('0.5')
DEFAULT_LEVERAGE = 5
MAX_LEVERAGE = 5


def check_live_execution():
    return {
        'status': 'BLOCKED', 'live_trading': False, 'orders_enabled': False,
        'exchange': 'Gate.io',
        'reason': 'Live order execution and exchange-side protection are not implemented',
    }


def submit_live_order(*args, **kwargs):
    raise RuntimeError('LIVE ORDER BLOCKED: exchange order submission is not implemented')


def reverse_live_position(*args, **kwargs):
    raise RuntimeError('REVERSE BLOCKED: position reconciliation not implemented')


def close_live_position(*args, **kwargs):
    raise RuntimeError('CLOSE BLOCKED: account-safe close not implemented')


def check_gate_public_connection():
    url = 'https://api.gateio.ws/api/v4/futures/usdt/contracts/BTC_USDT'
    try:
        req = Request(url, headers={'Accept': 'application/json'})
        with urlopen(req, timeout=10) as response:
            data = json.load(response)
        if data.get('name') != 'BTC_USDT':
            raise ValueError('Unexpected contract returned')
        return {
            'status': 'CONNECTED', 'exchange': 'Gate.io', 'market': 'BTC_USDT',
            'connection': 'PUBLIC_READ_ONLY', 'live_trading': False, 'orders_enabled': False,
        }
    except Exception as exc:
        return {'status': 'FAILED', 'reason': str(exc), 'live_trading': False, 'orders_enabled': False}


def _decimal(value, name):
    if isinstance(value, bool):
        raise ValueError(f'{name}: invalid boolean')
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f'{name}: not a number') from None
    if not number.is_finite() or number <= 0:
        raise ValueError(f'{name}: must be positive and finite')
    return number


def prepare_dry_run(signal, market, account):
    """OFFLINE validation and sizing, no exchange interaction.

    Required signal: event_id, symbol, side, entry, sl, tp1, tp2.
    Required market: contract, quanto_multiplier, order_size_min,
       order_size_max, last_price; these MUST be obtained from fresh exchange
       metadata for any real future implementation.
    Required account: equity_usdt, open_position_count.
    This function is NOT an executable trade authorization or quote.
    """
    try:
        event_id = str(signal['event_id']).strip()
        symbol = str(signal['symbol']).upper().replace('/', '').replace('_', '')
        side = str(signal['side']).upper()
        if not event_id or len(event_id) > 128:
            raise ValueError('Invalid signal event ID')
        if not re.fullmatch(r'[A-Z0-9]{2,28}USDT', symbol):
            raise ValueError('Invalid USDT futures symbol')
        if side not in ('LONG', 'SHORT'):
            raise ValueError('Direction must be LONG or SHORT')
        expected_contract = symbol[:-4] + '_USDT'
        if market['contract'] != expected_contract:
            raise ValueError('Signal and contract mismatch')
        entry = _decimal(signal['entry'], 'entry')
        stop = _decimal(signal['sl'], 'stop loss')
        tp1 = _decimal(signal['tp1'], 'TP1')
        tp2 = _decimal(signal['tp2'], 'TP2')
        last = _decimal(market['last_price'], 'last price')
        multiplier = _decimal(market['quanto_multiplier'], 'contract multiplier')
        min_size = int(_decimal(market['order_size_min'], 'minimum contracts'))
        max_size = int(_decimal(market['order_size_max'], 'maximum contracts'))
        equity = _decimal(account['equity_usdt'], 'equity')
        positions = account['open_position_count']
        if isinstance(positions, bool) or not isinstance(positions, int) or positions < 0:
            raise ValueError('Invalid position count')
        if positions >= MAX_OPEN_POSITIONS:
            raise ValueError('Maximum open-position limit reached')
        if min_size < 1 or max_size < min_size:
            raise ValueError('Invalid exchange contract size limits')
        if side == 'LONG' and not (stop < entry < tp1 <= tp2):
            raise ValueError('LONG levels must satisfy SL < Entry < TP1 <= TP2')
        if side == 'SHORT' and not (tp2 <= tp1 < entry < stop):
            raise ValueError('SHORT levels must satisfy TP2 <= TP1 < Entry < SL')
        # Signal entry should not be treated as an exchange fill/mark price.
        if abs(last-entry)/entry > Decimal('0.01'):
            raise ValueError('Market price differs from signal entry by over 1%')
        risk_budget = equity * RISK_PER_TRADE_PERCENT / Decimal(100)
        unit_stop_risk = abs(entry-stop) * multiplier
        size = int((risk_budget / unit_stop_risk).to_integral_value(rounding=ROUND_DOWN))
        if size < min_size:
            raise ValueError('Risk budget too small for minimum contract quantity')
        if size > max_size:
            size = max_size
        notional = Decimal(size) * multiplier * entry
        margin = notional / Decimal(DEFAULT_LEVERAGE)
        if margin > equity:
            raise ValueError('Insufficient indicative equity for margin')
        estimated_stop_risk = Decimal(size) * unit_stop_risk
        if estimated_stop_risk > risk_budget:
            raise ValueError('Calculated risk exceeds budget')
        rr1 = abs(tp1-entry)/abs(entry-stop)
        rr2 = abs(tp2-entry)/abs(entry-stop)
        return {
            'status': 'VALID_DRY_RUN_NOT_EXECUTABLE',
            'event_id': event_id, 'contract': expected_contract, 'side': side,
            'entry_reference': str(entry), 'stop_loss': str(stop),
            'take_profit_1': str(tp1), 'take_profit_2': str(tp2),
            'leverage': DEFAULT_LEVERAGE, 'contracts': size,
            'indicative_notional_usdt': str(notional.quantize(Decimal('0.01'))),
            'indicative_margin_usdt': str(margin.quantize(Decimal('0.01'))),
            'risk_budget_usdt': str(risk_budget.quantize(Decimal('0.01'))),
            'estimated_stop_risk_usdt': str(estimated_stop_risk.quantize(Decimal('0.01'))),
            'rr_tp1': str(rr1.quantize(Decimal('0.01'))), 'rr_tp2': str(rr2.quantize(Decimal('0.01'))),
            'exchange_protective_orders_verified': False,
            'positions_reconciled': False,
            'live_trading': False, 'orders_enabled': False,
            'warnings': ['Does not account for fees, slippage, funding, market gaps, or changing margin tiers',
                         'No authenticated position/account reconciliation',
                         'No exchange-native SL/TP orders have been placed or checked'],
        }
    except (KeyError, ValueError, TypeError, OverflowError) as exc:
        return {
            'status': 'BLOCKED', 'reason': str(exc), 'live_trading': False,
            'orders_enabled': False,
        }


def audit_exchange_readonly():
    """Run authenticated GET-only diagnostics if trusted environment credentials exist.

    This is an observation, NEVER an execution authorization. No exchange writes.
    Failure or missing credentials always means NOT VERIFIED.
    """
    import os
    try:
        from gate_readonly_reconcile import ENDPOINTS, fetch_readonly, summarize
        key = os.environ.get('GATE_API_KEY', '').strip()
        secret = os.environ.get('GATE_API_SECRET', '').strip()
        if not key or not secret:
            return {
                'status': 'NOT_VERIFIED',
                'reason': 'Read-only API credentials unavailable in this process',
                'safe_to_trade': False,
                'live_trading': False,
                'orders_enabled': False,
            }
        snapshots = {name: fetch_readonly(path, query, key, secret)
                     for name, path, query in ENDPOINTS}
        result = summarize(snapshots)
        # Deliberately do not forward any authorization to submit_live_order().
        result['safe_to_trade'] = False
        result['live_trading'] = False
        result['orders_enabled'] = False
        return result
    except (ImportError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        # Avoid logging underlying API responses or credentials.
        return {'status': 'NOT_VERIFIED', 'reason': 'Reconciliation check failed: ' + type(exc).__name__,
                'safe_to_trade': False, 'live_trading': False, 'orders_enabled': False}



# Stage: protected-order intent PREVIEW only. No Gate.io order endpoints exist.
def preview_protected_order_intent(signal, market, account, snapshot, *,
                                   expected_snapshot_age_seconds=None,
                                   current_market_age_seconds=None):
    """Fail-closed preflight using real-format inputs, WITHOUT an execution path.

    The exchange snapshot must be authenticated separately, then carefully
    validated. Even when it is valid this function ALWAYS returns non-executable.
    Neither checking a snapshot nor displaying an intent proves exchange-side
    stop-loss or take-profit protection exists.
    """
    preview = prepare_dry_run(signal, market, account)
    if preview.get('status') != 'VALID_DRY_RUN_NOT_EXECUTABLE':
        return {'status': 'BLOCKED', 'reason': preview.get('reason', 'Preflight failed'),
                'safe_to_trade': False, 'orders_enabled': False}

    if not isinstance(snapshot, dict) or snapshot.get('status') != 'READ_ONLY_SNAPSHOT':
        return {'status': 'BLOCKED', 'reason': 'Authenticated read-only snapshot is required',
                'safe_to_trade': False, 'orders_enabled': False}

    # Do not trust a client-supplied position count over the exchange report.
    try:
        exchange_positions = snapshot['active_positions']
        if (type(exchange_positions) is not int or exchange_positions < 0 or
                exchange_positions != account['open_position_count']):
            raise ValueError('Account/exchange position-count mismatch')
        if exchange_positions >= MAX_OPEN_POSITIONS:
            raise ValueError('Five-position limit reached on exchange')
        if expected_snapshot_age_seconds is None or current_market_age_seconds is None:
            raise ValueError('Snapshot and market freshness must be supplied')
        age = float(expected_snapshot_age_seconds)
        market_age = float(current_market_age_seconds)
        if not (0 <= age <= 15 and 0 <= market_age <= 15):
            raise ValueError('Exchange snapshot or market metadata is stale')
        if snapshot.get('bot_order_reconciliation') not in ('MATCHED', 'VERIFIED'):
            raise ValueError('Bot-to-exchange order identity is not reconciled')
        if snapshot.get('protective_sl_tp_coverage') != 'VERIFIED':
            raise ValueError('Exchange-native SL/TP protection is not verified')
    except (KeyError, ValueError, TypeError, OverflowError) as exc:
        return {'status': 'BLOCKED', 'reason': str(exc),
                'safe_to_trade': False, 'orders_enabled': False}

    # Even if every prerequisite is reported true, real submission is disabled.
    # A real execution engine requires independently reviewed exchange-native
    # create/cancel/replace semantics, idempotency, and protection verification.
    return {'status': 'PREVIEW_ONLY', 'intent': preview,
            'reason': 'Preflight data checked; exchange execution is not implemented',
            'safe_to_trade': False, 'live_trading': False, 'orders_enabled': False}


def offline_protection_test():
    """Fixture tests: safe blocked behavior and no network/API side effects."""
    signal = {'event_id':'TEST-002','symbol':'BTCUSDT','side':'LONG',
              'entry':'80000','sl':'79700','tp1':'80600','tp2':'80900'}
    market = {'contract':'BTC_USDT','last_price':'80010',
              'quanto_multiplier':'0.0001','order_size_min':1,'order_size_max':1000000}
    account = {'equity_usdt':'300','open_position_count':0}
    empty = {'status':'READ_ONLY_SNAPSHOT', 'active_positions':0,
             'bot_order_reconciliation':'NOT_IMPLEMENTED',
             'protective_sl_tp_coverage':'NOT_VERIFIED'}
    not_ready = preview_protected_order_intent(signal,market,account,empty,
                                                 expected_snapshot_age_seconds=2,
                                                 current_market_age_seconds=2)
    assert not_ready['status']=='BLOCKED'
    assert not_ready['safe_to_trade'] is False
    asserted = dict(empty,bot_order_reconciliation='VERIFIED',
                    protective_sl_tp_coverage='VERIFIED')
    simulated = preview_protected_order_intent(signal,market,account,asserted,
                                                 expected_snapshot_age_seconds=2,
                                                 current_market_age_seconds=2)
    assert simulated['status']=='PREVIEW_ONLY' and not simulated['orders_enabled']
    mismatch = preview_protected_order_intent(signal,market,account,
                                                 dict(asserted,active_positions=1),
                                                 expected_snapshot_age_seconds=2,
                                                 current_market_age_seconds=2)
    assert mismatch['status']=='BLOCKED'
    stale = preview_protected_order_intent(signal,market,account,asserted,
                                             expected_snapshot_age_seconds=20,
                                             current_market_age_seconds=2)
    assert stale['status']=='BLOCKED'
    for blocked_function in (submit_live_order,reverse_live_position,close_live_position):
        try: blocked_function()
        except RuntimeError: pass
        else: raise AssertionError('Exchange write was unexpectedly allowed')
    return {'fixture_tests':'PASSED', 'not_verified':'BLOCKED',
            'snapshot_mismatch':'BLOCKED','stale_data':'BLOCKED',
            'validated_intent':'PREVIEW_ONLY', 'real_orders':'BLOCKED'}

def offline_self_test():
    signal = {'event_id': 'OFFLINE-TEST-001', 'symbol': 'BTCUSDT', 'side': 'LONG',
              'entry': '80000', 'sl': '79700', 'tp1': '80600', 'tp2': '80900'}
    market = {'contract': 'BTC_USDT', 'last_price': '80010',
              'quanto_multiplier': '0.0001', 'order_size_min': 1, 'order_size_max': 1000000}
    account = {'equity_usdt': '300', 'open_position_count': 0}
    valid = prepare_dry_run(signal, market, account)
    assert valid['status'] == 'VALID_DRY_RUN_NOT_EXECUTABLE', valid
    assert valid['estimated_stop_risk_usdt'] == '1.50'
    bad = prepare_dry_run({**signal, 'sl': '81000'}, market, account)
    assert bad['status'] == 'BLOCKED'
    max_positions = prepare_dry_run(signal, market, {**account, 'open_position_count': 5})
    assert max_positions['status'] == 'BLOCKED'
    try:
        submit_live_order()
    except RuntimeError:
        pass
    else:
        raise AssertionError('Unexpectedly allowed real order')
    return {
        'offline_tests': 'PASSED', 'valid_dry_run': valid,
        'invalid_levels': bad, 'position_limit': max_positions,
        'real_order_submission': 'BLOCKED',
    }




# Development-only integration with the tested persistent one-trade controller.
# Deliberately NO exchange order submission, no credential access, and no
# protective-order claims. This API only reserves an offline intent after
# risk validation; DO NOT call it with the production database path.
def stage_one_trade_offline(signal, market, account, ledger_path):
    """Validate sizing and durably reserve ONE offline test intent.

    ledger_path must point to a dedicated DEVELOPMENT sqlite file. Calling this
    function twice never places an order; the second intent is blocked.
    """
    from gate_one_trade_lifecycle_DEV_WINDOWS_FIXED import SingleTradeLedger, TradeRequest

    plan = prepare_dry_run(signal, market, account)
    if plan.get('status') != 'VALID_DRY_RUN_NOT_EXECUTABLE':
        return {'status': 'BLOCKED', 'reason': plan.get('reason', 'Risk validation failed'),
                'live_trading': False, 'orders_enabled': False}

    request = TradeRequest(
        event_id=plan['event_id'], contract=plan['contract'], side=plan['side'],
        entry=plan['entry_reference'], stop=plan['stop_loss'],
        take=plan['take_profit_1'],
    )
    ledger = SingleTradeLedger(str(ledger_path))
    outcome = ledger.reserve(request)
    return {
        'status': 'OFFLINE_RESERVED' if outcome == 'RESERVED' else 'BLOCKED',
        'reason': 'OFFLINE ONLY - no Gate.io order submitted' if outcome == 'RESERVED' else outcome,
        'trade_state': ledger.status(),
        'preview': plan if outcome == 'RESERVED' else None,
        'exchange_entry_submitted': False,
        'exchange_protective_orders_verified': False,
        'live_trading': False,
        'orders_enabled': False,
    }


def lifecycle_integration_self_test():
    """Offline-only tests; temporary database connections fully closed on Windows."""
    from tempfile import TemporaryDirectory
    from pathlib import Path
    signal = {'event_id': 'OFFLINE-LIFECYCLE-1', 'symbol': 'BTCUSDT', 'side': 'LONG',
              'entry': '80000', 'sl': '79700', 'tp1': '80600', 'tp2': '80900'}
    market = {'contract': 'BTC_USDT', 'last_price': '80010',
              'quanto_multiplier': '0.0001', 'order_size_min': 1, 'order_size_max': 1000000}
    account = {'equity_usdt': '300', 'open_position_count': 0}
    with TemporaryDirectory() as folder:
        path = Path(folder) / 'offline_ledger.sqlite'
        first = stage_one_trade_offline(signal, market, account, path)
        assert first['status'] == 'OFFLINE_RESERVED', first
        assert first['exchange_entry_submitted'] is False
        assert first['exchange_protective_orders_verified'] is False
        second = stage_one_trade_offline(signal, market, account, path)
        assert second['status'] == 'BLOCKED', second
        invalid = stage_one_trade_offline({**signal, 'sl': '81000'}, market, account,
                                         Path(folder) / 'invalid.sqlite')
        assert invalid['status'] == 'BLOCKED', invalid
        assert not (Path(folder) / 'invalid.sqlite').exists()
        from gate_one_trade_lifecycle_DEV_WINDOWS_FIXED import SingleTradeLedger
        assert SingleTradeLedger(str(path)).status()['state'] == 'RESERVED'
        assert LIVE_TRADING_ENABLED is False and ORDERS_ENABLED is False
    return {'risk_validation': 'PASSED', 'persistent_reservation': 'PASSED',
            'duplicate_trade_block': 'PASSED', 'invalid_signal_block': 'PASSED',
            'windows_sqlite_cleanup': 'PASSED', 'real_gate_orders': 'BLOCKED',
            'live_ready': False}




# --- Gate.io API v4 protocol preparation (review-only, no exchange writes) ---
# Official Gate API v4 futures documentation defines a market order as
# price="0", tif="ioc"; signed contract quantity is positive for LONG,
# negative for SHORT. Native attached TP/SL trigger fields exist but must
# be verified against real exchange position + conditional order state.
# This code never submits anything to Gate.io.
import hashlib as _hashlib
import hmac as _hmac
import time as _time

GATE_API_V4_ROOT = "/api/v4"
GATE_FUTURES_ENTRY_PATH = "/api/v4/futures/usdt/orders"


def gate_v4_signature(method, path, query, body, timestamp, secret):
    """Sign one API v4 request; pure function, no network or credential storage."""
    if method not in ('GET', 'POST') or not path.startswith('/api/v4/'):
        raise ValueError('Unsupported Gate.io request shape')
    if not isinstance(body, str) or not isinstance(secret, str) or not secret:
        raise ValueError('Invalid request body or secret')
    digest = _hashlib.sha512(body.encode('utf-8')).hexdigest()
    message = f'{method}\n{path}\n{query}\n{digest}\n{timestamp}'
    return _hmac.new(secret.encode('utf-8'), message.encode('utf-8'), _hashlib.sha512).hexdigest()


def gate_entry_payload_review(plan, *, user_text):
    """Prepare documented Gate market entry WITH attached TP/SL request fields.

    WARNING: a request asking for TP/SL does not mean protections exist.
    Unverified atomicity, SL/TP trigger handling, fill status or margin mode
    prevents live authorization. This does NOT make a network request.
    """
    if not isinstance(plan, dict) or plan.get('status') != 'VALID_DRY_RUN_NOT_EXECUTABLE':
        raise ValueError('Only validated risk previews may be reviewed')
    if not re.fullmatch(r't-[A-Za-z0-9_-]{1,25}', str(user_text)):
        raise ValueError('Order text must be unique and Gate-compatible')
    contract = str(plan['contract'])
    if not re.fullmatch(r'[A-Z0-9]{2,28}_USDT', contract):
        raise ValueError('Invalid futures contract')
    size = int(plan['contracts'])
    if size <= 0 or plan['side'] not in ('LONG', 'SHORT'):
        raise ValueError('Invalid order quantity or side')
    if int(plan['leverage']) > MAX_LEVERAGE:
        raise ValueError('Configured leverage limit exceeded')
    entry = _decimal(plan['entry_reference'], 'entry')
    stop = _decimal(plan['stop_loss'], 'stop')
    take = _decimal(plan['take_profit_1'], 'take')
    if plan['side'] == 'LONG' and not (stop < entry < take):
        raise ValueError('Invalid LONG protection levels')
    if plan['side'] == 'SHORT' and not (take < entry < stop):
        raise ValueError('Invalid SHORT protection levels')
    body = {
        'contract': contract,
        'size': str(size if plan['side'] == 'LONG' else -size),
        'price': '0', 'tif': 'ioc', 'text': user_text,
        'tpsl_tp_trigger_price': str(take),
        'tpsl_sl_trigger_price': str(stop),
    }
    return {
        'status': 'REVIEW_ONLY_NOT_SUBMITTED',
        'method': 'POST', 'path': GATE_FUTURES_ENTRY_PATH,
        'body': body, 'exchange_order_id': None,
        'entry_submitted': False,
        'protection_placed': False,
        'protection_verified': False,
        'live_ready': False,
        'reason': 'Order POST is locked; attached SL/TP semantics, fills, margin, and exchange protection not yet verified',
    }


def gate_authenticated_get_only(api_key, api_secret, path, *, query='', timeout=8):
    """Authenticated read-only GET of strict allowlisted futures endpoints.

    Credentials are passed in memory only. Do not log key/secret/headers.
    Returns parsed JSON; caller must validate response freshness/identity.
    """
    import urllib.parse
    allowed = (
        '/api/v4/futures/usdt/accounts',
        '/api/v4/futures/usdt/positions',
        '/api/v4/futures/usdt/orders',
        '/api/v4/futures/usdt/price_orders',
    )
    if path not in allowed:
        raise ValueError('GET endpoint is not allowlisted')
    if not api_key or not api_secret or not isinstance(query, str):
        raise ValueError('Missing credentials or invalid query')
    stamp = str(int(_time.time()))
    sign = gate_v4_signature('GET', path, query, '', stamp, api_secret)
    url = 'https://api.gateio.ws' + path + (('?' + query) if query else '')
    req = Request(url, method='GET', headers={
        'KEY': api_key, 'Timestamp': stamp, 'SIGN': sign,
        'Accept': 'application/json',
    })
    with urlopen(req, timeout=timeout) as response:
        return json.load(response)


def gate_api_protocol_self_test():
    """Offline tests only, using no credentials or network."""
    import unittest.mock as _mock
    plan = prepare_dry_run(
        {'event_id':'PROTOCOL-TEST', 'symbol':'BTCUSDT', 'side':'SHORT',
         'entry':'80000', 'sl':'80300', 'tp1':'79400', 'tp2':'79100'},
        {'contract':'BTC_USDT', 'last_price':'80010',
         'quanto_multiplier':'0.0001', 'order_size_min':1, 'order_size_max':1000000},
        {'equity_usdt':'300', 'open_position_count':0})
    assert plan['status'] == 'VALID_DRY_RUN_NOT_EXECUTABLE', plan
    body = gate_entry_payload_review(plan, user_text='t-test-protocol-1')
    assert int(body['body']['size']) < 0
    assert body['body']['price'] == '0' and body['body']['tif'] == 'ioc'
    assert body['body']['tpsl_sl_trigger_price'] == '80300'
    assert body['entry_submitted'] is False and body['protection_verified'] is False
    v1 = gate_v4_signature('POST', GATE_FUTURES_ENTRY_PATH, '', '{}', '12345', 'sample-key')
    v2 = gate_v4_signature('POST', GATE_FUTURES_ENTRY_PATH, '', '{}', '12345', 'sample-key')
    assert len(v1) == 128 and v1 == v2
    with _mock.patch('urllib.request.urlopen', side_effect=AssertionError('network should not run')):
        try:
            gate_authenticated_get_only('key', 'secret', '/api/v4/futures/usdt/orders/bad')
        except ValueError:
            pass
        else:
            raise AssertionError('Unknown endpoint was allowed')
    assert LIVE_TRADING_ENABLED is False and ORDERS_ENABLED is False
    return {'signed_api_request_format':'PASSED', 'gate_market_entry_payload':'PASSED',
            'short_order_direction':'PASSED', 'get_endpoint_allowlist':'PASSED',
            'network_writes':'BLOCKED', 'real_gate_orders':'BLOCKED', 'live_ready':False}



# --- Exchange-response protection audit (OFFLINE review, no network writes) ---
def audit_protection_snapshots(plan, entry_order, stop_order, take_order, position):
    """Validate four exchange-shaped records; never authorize order submission.

    Inputs MUST be independently retrieved from Gate.io using authenticated GETs.
    This only checks identities, directions, active trigger levels and exposure.
    It is NOT proof the exchange will fill stops or prevent slippage.
    """
    def blocked(reason):
        return {'status': 'NOT_VERIFIED', 'reason': reason,
                'exchange_protective_orders_verified': False,
                'safe_to_trade': False, 'live_ready': False}

    if not isinstance(plan, dict) or plan.get('status') != 'VALID_DRY_RUN_NOT_EXECUTABLE':
        return blocked('Missing validated risk plan')
    if not all(isinstance(x, dict) for x in (entry_order, stop_order, take_order, position)):
        return blocked('Missing exchange response')
    contract = plan['contract']
    side = plan['side']
    if any(x.get('contract') != contract for x in (entry_order, position)):
        return blocked('Entry/position contract mismatch')
    if not entry_order.get('id'):
        return blocked('Missing exchange entry order ID')
    if entry_order.get('status') != 'finished' or str(entry_order.get('left', '')) != '0':
        return blocked('Entry fill status not fully reconciled')
    try:
        pos_size = Decimal(str(position['size']))
        min_size = Decimal(str(plan['contracts']))
        if not pos_size.is_finite() or abs(pos_size) != min_size:
            return blocked('Position size mismatch')
        if (side == 'LONG' and pos_size <= 0) or (side == 'SHORT' and pos_size >= 0):
            return blocked('Position direction mismatch')
        expected_type = 'close-long-position' if side == 'LONG' else 'close-short-position'
        trigger_rule = (2, 1) if side == 'LONG' else (1, 2)
        for name, order, level, rule in (
            ('SL', stop_order, plan['stop_loss'], trigger_rule[0]),
            ('TP', take_order, plan['take_profit_1'], trigger_rule[1]),
        ):
            if not order.get('id') or order.get('status') != 'open':
                return blocked(name + ' trigger missing or not open')
            if order.get('order_type') != expected_type:
                return blocked(name + ' is not a full position-close trigger')
            initial, trigger = order.get('initial'), order.get('trigger')
            if not isinstance(initial, dict) or not isinstance(trigger, dict):
                return blocked(name + ' trigger payload incomplete')
            if initial.get('contract') != contract or str(initial.get('size')) != '0':
                return blocked(name + ' contract/full-close mismatch')
            if initial.get('close') is not True or str(initial.get('price')) != '0':
                return blocked(name + ' does not request full market close')
            if int(trigger['rule']) != rule or int(trigger['strategy_type']) != 0:
                return blocked(name + ' trigger direction/strategy mismatch')
            if Decimal(str(trigger['price'])) != Decimal(str(level)):
                return blocked(name + ' trigger price mismatch')
        if str(stop_order['id']) == str(take_order['id']):
            return blocked('SL and TP reference the same trigger')
    except (ValueError, TypeError, KeyError, ArithmeticError):
        return blocked('Invalid position or trigger response data')
    return {'status': 'OFFLINE_SNAPSHOT_MATCH',
            'reason': 'Snapshot fields match; freshness, provenance, live execution and recovery NOT verified',
            'exchange_protective_orders_verified': False,
            'safe_to_trade': False, 'live_ready': False}


def protection_snapshot_self_test():
    plan = prepare_dry_run(
        {'event_id':'AUDIT-TEST','symbol':'BTCUSDT','side':'LONG',
         'entry':'80000','sl':'79700','tp1':'80600','tp2':'80900'},
        {'contract':'BTC_USDT','last_price':'80010','quanto_multiplier':'0.0001',
         'order_size_min':1,'order_size_max':1000000},
        {'equity_usdt':'300','open_position_count':0})
    entry = {'id':123,'contract':'BTC_USDT','status':'finished','left':'0'}
    position = {'contract':'BTC_USDT','size':str(plan['contracts'])}
    def trigger(oid, price, rule):
        return {'id':oid,'status':'open','order_type':'close-long-position',
                'initial':{'contract':'BTC_USDT','size':0,'close':True,'price':'0'},
                'trigger':{'strategy_type':0,'rule':rule,'price':price}}
    sl = trigger(11,'79700',2)
    tp = trigger(12,'80600',1)
    good = audit_protection_snapshots(plan, entry, sl, tp, position)
    assert good['status'] == 'OFFLINE_SNAPSHOT_MATCH' and not good['live_ready']
    assert audit_protection_snapshots(plan,entry,dict(sl,status='finished'),tp,position)['status']=='NOT_VERIFIED'
    assert audit_protection_snapshots(plan,entry,sl,tp,dict(position,size='-50'))['status']=='NOT_VERIFIED'
    assert audit_protection_snapshots(plan,entry,dict(sl,trigger={**sl['trigger'],'price':'79701'}),tp,position)['status']=='NOT_VERIFIED'
    assert audit_protection_snapshots(plan,entry,sl,dict(tp,id=11),position)['status']=='NOT_VERIFIED'
    return {'matching_fixture':'PASSED_NOT_LIVE_PROOF', 'inactive_sl':'BLOCKED',
            'wrong_position':'BLOCKED', 'wrong_stop_price':'BLOCKED',
            'duplicate_trigger_id':'BLOCKED', 'network_writes':'BLOCKED', 'live_ready':False}



# --- Authenticated Gate.io order-ID reconciliation (READ-ONLY) ---
# No live execution capability is added. This is an audit of EXISTING exchange orders.
def gate_get_order_record(api_key, api_secret, kind, order_id, *, timeout=8):
    """GET one order by immutable exchange ID. No account-changing methods."""
    import urllib.error
    if kind not in ('orders', 'price_orders'):
        raise ValueError('Unsupported Gate order type')
    order_id = str(order_id)
    if not re.fullmatch(r'[1-9][0-9]{0,19}', order_id):
        raise ValueError('Invalid exchange order ID')
    if not api_key or not api_secret:
        raise ValueError('Missing API credentials')
    path = '/api/v4/futures/usdt/' + kind + '/' + order_id
    stamp = str(int(_time.time()))
    signature = gate_v4_signature('GET', path, '', '', stamp, api_secret)
    req = Request('https://api.gateio.ws' + path, method='GET', headers={
        'KEY': api_key, 'Timestamp': stamp, 'SIGN': signature,
        'Accept': 'application/json',
    })
    try:
        with urlopen(req, timeout=timeout) as response:
            record = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError('Gate read-only HTTP status: ' + str(exc.code)) from None
    except (urllib.error.URLError, TimeoutError):
        raise RuntimeError('Gate read-only network failure') from None
    if not isinstance(record, dict) or str(record.get('id')) != order_id:
        raise RuntimeError('Gate order lookup returned mismatched identity')
    return record


def gate_get_position_record(api_key, api_secret, contract, *, timeout=8):
    """GET one futures position, requiring exact contract identity."""
    import urllib.error
    if not re.fullmatch(r'[A-Z0-9]{2,28}_USDT', str(contract)):
        raise ValueError('Invalid contract')
    if not api_key or not api_secret:
        raise ValueError('Missing API credentials')
    path = '/api/v4/futures/usdt/positions/' + contract
    stamp = str(int(_time.time()))
    signature = gate_v4_signature('GET', path, '', '', stamp, api_secret)
    req = Request('https://api.gateio.ws' + path, method='GET', headers={
        'KEY': api_key, 'Timestamp': stamp, 'SIGN': signature,
        'Accept': 'application/json',
    })
    try:
        with urlopen(req, timeout=timeout) as response:
            record = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError('Gate read-only HTTP status: ' + str(exc.code)) from None
    except (urllib.error.URLError, TimeoutError):
        raise RuntimeError('Gate read-only network failure') from None
    if not isinstance(record, dict) or record.get('contract') != contract:
        raise RuntimeError('Gate position lookup returned mismatched contract')
    return record


def reconcile_existing_gate_trade(plan, ids, *, api_key=None, api_secret=None,
                                  order_reader=None, position_reader=None):
    """GET-only audit of already-existing entry, SL, TP and position.

    Explicit verified IDs required: entry_order_id, stop_order_id, take_order_id.
    Missing orders, stale information, mismatches => NOT_VERIFIED. No trading
    permission, no order creation, no position closure or live-ready assertion.
    """
    blocked = lambda reason: {'status': 'NOT_VERIFIED', 'reason': reason,
                               'safe_to_trade': False, 'live_ready': False,
                               'exchange_protective_orders_verified': False}
    if not isinstance(plan, dict) or plan.get('status') != 'VALID_DRY_RUN_NOT_EXECUTABLE':
        return blocked('Validated execution reference missing')
    if not isinstance(ids, dict):
        return blocked('Explicit exchange order IDs required')
    values = [str(ids.get(k, '')) for k in ('entry_order_id', 'stop_order_id', 'take_order_id')]
    if any(not re.fullmatch(r'[1-9][0-9]{0,19}', x) for x in values) or len(set(values)) != 3:
        return blocked('Distinct valid exchange entry, SL and TP IDs required')
    if not api_key or not api_secret:
        if order_reader is None or position_reader is None:
            return blocked('Authenticated Gate credentials or injected test reader required')
    read_order = order_reader or (lambda kind, oid: gate_get_order_record(api_key, api_secret, kind, oid))
    read_position = position_reader or (lambda contract: gate_get_position_record(api_key, api_secret, contract))
    try:
        entry = read_order('orders', values[0])
        stop = read_order('price_orders', values[1])
        take = read_order('price_orders', values[2])
        position = read_position(plan['contract'])
        result = audit_protection_snapshots(plan, entry, stop, take, position)
        if result.get('status') != 'OFFLINE_SNAPSHOT_MATCH':
            return blocked('Exchange order/position snapshot mismatch: ' + result.get('reason','unknown'))
        # Even real read-only snapshots are only a point-in-time observation.
        return {'status': 'READ_ONLY_SNAPSHOT_MATCH',
                'reason': 'Four records matched at query time; no guarantee of protection persistence or execution',
                'exchange_protective_orders_verified': False, 'safe_to_trade': False,
                'live_ready': False, 'network_writes': 'BLOCKED'}
    except (RuntimeError, ValueError, TypeError, KeyError, OSError):
        return blocked('Unable to authenticate or reconcile all requested records')


def gate_readonly_reconciliation_self_test():
    plan = prepare_dry_run(
        {'event_id':'AUDIT-READ-1', 'symbol':'BTCUSDT', 'side':'LONG',
         'entry':'80000','sl':'79700','tp1':'80600','tp2':'80900'},
        {'contract':'BTC_USDT','last_price':'80010','quanto_multiplier':'0.0001',
         'order_size_min':1,'order_size_max':1000000},
        {'equity_usdt':'300','open_position_count':0})
    entry = {'id':101,'contract':'BTC_USDT','status':'finished','left':'0'}
    def trig(oid, price, rule):
        return {'id':oid,'status':'open','order_type':'close-long-position',
                'initial':{'contract':'BTC_USDT','size':0,'close':True,'price':'0'},
                'trigger':{'strategy_type':0,'rule':rule,'price':price}}
    records = {('orders','101'):entry,
               ('price_orders','102'):trig(102,'79700',2),
               ('price_orders','103'):trig(103,'80600',1)}
    reader=lambda kind,oid: records[(kind,oid)]
    pos=lambda contract: {'contract':contract,'size':str(plan['contracts'])}
    ids={'entry_order_id':'101','stop_order_id':'102','take_order_id':'103'}
    result=reconcile_existing_gate_trade(plan,ids,order_reader=reader,position_reader=pos)
    assert result['status']=='READ_ONLY_SNAPSHOT_MATCH' and result['live_ready'] is False
    assert reconcile_existing_gate_trade(plan,{**ids,'take_order_id':'102'},order_reader=reader,position_reader=pos)['status']=='NOT_VERIFIED'
    bad=dict(records); bad[('price_orders','102')]=dict(records[('price_orders','102')],status='inactive')
    assert reconcile_existing_gate_trade(plan,ids,order_reader=lambda k,i:bad[(k,i)],position_reader=pos)['status']=='NOT_VERIFIED'
    try: gate_get_order_record('a','b','orders','../../bad')
    except ValueError: pass
    else: raise AssertionError('Unsafe ID accepted')
    assert not reconcile_existing_gate_trade(plan,ids)['safe_to_trade']
    return {'read_only_record_match':'PASSED_NOT_LIVE_PROOF',
            'explicit_id_requirement':'PASSED', 'inactive_stop_block':'PASSED',
            'unsafe_order_id_block':'PASSED', 'missing_credentials_block':'PASSED',
            'exchange_writes':'BLOCKED','live_ready':False}


# --- One-way execution gateway: real HTTP shape with production dispatch HARD LOCKED ---
# This section is kept in the existing development module, rather than another
# disconnected script. It does not wire the Flask webhook to exchange writes.
from dataclasses import dataclass as _dataclass
from urllib.error import HTTPError as _HTTPError, URLError as _URLError

class ExecutionNotReady(RuntimeError):
    pass

_GATE_ALLOWED_POSTS = frozenset({
    '/api/v4/futures/usdt/orders',
    '/api/v4/futures/usdt/price_orders',
})


def gate_oneway_close_payload_review(contract):
    """Review-only one-way close; never a direction-reversing order."""
    if not re.fullmatch(r'[A-Z0-9]{2,28}_USDT', str(contract)):
        raise ValueError('Invalid USDT contract')
    return {'contract': contract, 'size': 0, 'close': True,
            'reduce_only': True, 'price': '0', 'tif': 'ioc'}


def gate_oneway_trigger_payload_review(plan, kind, *, mark_price):
    """Review-only separate full-position SL or TP for one-way futures.

    Uses Gate.io price-order semantics; never submits. A mark-price comparison
    is a reference check, not a guarantee that latest-trade triggers are valid.
    """
    if plan.get('status') != 'VALID_DRY_RUN_NOT_EXECUTABLE':
        raise ValueError('Plan has not passed sizing validation')
    if kind not in ('SL', 'TP'):
        raise ValueError('Trigger kind must be SL or TP')
    mark = _decimal(mark_price, 'mark price')
    price = _decimal(plan['stop_loss'] if kind == 'SL' else plan['take_profit_1'], 'trigger price')
    side = plan['side']
    if side not in ('LONG', 'SHORT'):
        raise ValueError('Invalid trade side')
    rule = 2 if (side == 'LONG') == (kind == 'SL') else 1
    if (rule == 1 and price <= mark) or (rule == 2 and price >= mark):
        raise ValueError('Trigger is on the wrong side of current price')
    return {
        'initial': {'contract': plan['contract'], 'size': 0, 'close': True,
                    'reduce_only': True, 'price': '0', 'tif': 'ioc'},
        'trigger': {'strategy_type': 0, 'price_type': 1,
                    'price': str(price), 'rule': rule, 'expiration': 86400},
        'order_type': 'close-long-position' if side == 'LONG' else 'close-short-position',
    }


def gate_signed_post_preview(api_key, api_secret, path, payload, *, timestamp='1700000000'):
    """Create signed HTTP request metadata WITHOUT making a request.

    Returns no secret/key/signature in diagnostic output; credentials stay local.
    Caller must not log its return value or submit it while live is blocked.
    """
    if path not in _GATE_ALLOWED_POSTS:
        raise ValueError('Exchange POST endpoint not allowlisted')
    if not api_key or not api_secret or not isinstance(payload, dict):
        raise ValueError('Missing credentials or request payload')
    body = json.dumps(payload, separators=(',', ':'), ensure_ascii=True)
    signature = gate_v4_signature('POST', path, '', body, timestamp, api_secret)
    return {'method':'POST','path':path,'body':body,
            'headers':{'KEY':api_key,'Timestamp':timestamp,'SIGN':signature,
                       'Accept':'application/json','Content-Type':'application/json'}}


def gate_submit_protected_entry(*args, **kwargs):
    """Permanently locked until webhook, durable ledger and recovery are integrated.

    The signing and request-build code above is testable, but MUST NOT be
    mistaken for authorization to send the first real order.
    """
    raise ExecutionNotReady('Live POST dispatch locked: no verified automatic emergency exit and recovery')


def gate_entry_ack_assessment(response):
    """Classify entry acknowledgment; never retry unknown states automatically."""
    if not isinstance(response, dict):
        return {'state':'ACK_UNKNOWN', 'retry':False, 'exchange_order_id':None}
    order_id = str(response.get('id_string') or response.get('id') or '')
    if not re.fullmatch(r'[1-9][0-9]{0,19}', order_id):
        return {'state':'ACK_UNKNOWN', 'retry':False, 'exchange_order_id':None}
    # Acknowledged != filled, and attached TP/SL != confirmed protection.
    return {'state':'ACKNOWLEDGED_UNPROTECTED', 'retry':False,
            'exchange_order_id':order_id, 'filled':False, 'protection_verified':False}


def gate_execution_gateway_self_test():
    """Offline assertions: only request preparation and fail-closed behavior."""
    from unittest.mock import patch
    signal={'event_id':'TEST-GATEWAY','symbol':'BTCUSDT','side':'LONG',
            'entry':'80000','sl':'79700','tp1':'80600','tp2':'80900'}
    market={'contract':'BTC_USDT','last_price':'80010','quanto_multiplier':'0.0001',
            'order_size_min':1,'order_size_max':1000000}
    account={'equity_usdt':'300','open_position_count':0}
    plan=prepare_dry_run(signal, market, account)
    assert plan['status']=='VALID_DRY_RUN_NOT_EXECUTABLE'
    close=gate_oneway_close_payload_review('BTC_USDT')
    assert close['size']==0 and close['close'] and close['reduce_only']
    sl=gate_oneway_trigger_payload_review(plan,'SL',mark_price='80000')
    tp=gate_oneway_trigger_payload_review(plan,'TP',mark_price='80000')
    assert sl['trigger']['rule']==2 and tp['trigger']['rule']==1
    assert sl['initial']['close'] and tp['initial']['reduce_only']
    try: gate_oneway_trigger_payload_review(plan,'SL',mark_price='79600')
    except ValueError: pass
    else: raise AssertionError('Unsafe stop trigger permitted')
    signed=gate_signed_post_preview('test-key','test-secret',GATE_FUTURES_ENTRY_PATH,
                                    gate_entry_payload_review(plan,user_text='t-gateway1')['body'])
    assert signed['headers']['SIGN']==gate_v4_signature('POST',GATE_FUTURES_ENTRY_PATH,'',
                                                       signed['body'],'1700000000','test-secret')
    assert gate_entry_ack_assessment(None)['state']=='ACK_UNKNOWN'
    assert gate_entry_ack_assessment({'id':123})['state']=='ACKNOWLEDGED_UNPROTECTED'
    with patch('urllib.request.urlopen', side_effect=AssertionError('Network call forbidden')):
        try: gate_submit_protected_entry(signed)
        except ExecutionNotReady: pass
        else: raise AssertionError('Live dispatch accidentally available')
    assert not LIVE_TRADING_ENABLED and not ORDERS_ENABLED
    return {'oneway_market_close_shape':'PASSED', 'independent_sl_tp_payloads':'PASSED',
            'signed_post_payload':'PASSED', 'unknown_ack_no_retry':'PASSED',
            'live_http_post':'HARD_BLOCKED', 'webhook_execution':'NOT_CONNECTED',
            'live_ready':False}



# --- Pre-execution gate: only existing signed, read-only snapshots; still NO POST ---
def assess_oneway_execution_prerequisites(plan, snapshot, *, age_seconds, mode, entry_order_ids=()):
    """Pure, fail-closed admission review; NEVER authorizes live exchange writes.

    Account position mode must be externally verified ONE_WAY, with fresh and
    complete read-only snapshots. A zero open-order count is not sufficient to
    prove the absence of exchange exposure; all three snapshots are required.
    """
    result = {'status': 'BLOCKED', 'safe_to_trade': False,
              'orders_enabled': False, 'live_ready': False}
    if not isinstance(plan, dict) or plan.get('status') != 'VALID_DRY_RUN_NOT_EXECUTABLE':
        return dict(result, reason='Risk plan missing or invalid')
    if mode != 'ONE_WAY_VERIFIED':
        return dict(result, reason='One-way position mode not independently verified')
    if not isinstance(snapshot, dict) or snapshot.get('status') not in (
            'READ_ONLY_SNAPSHOT_REVIEW_REQUIRED', 'READ_ONLY_SNAPSHOT'):
        return dict(result, reason='Authenticated Gate account snapshot missing')
    try:
        from math import isfinite
        age = float(age_seconds)
        if not isfinite(age) or age < 0 or age > 10:
            raise ValueError('Stale snapshot')
        for field in ('active_positions', 'open_orders', 'open_trigger_orders',
                      'unreadable_position_rows', 'unknown_trigger_count'):
            v = snapshot[field]
            if type(v) is not int or v != 0:
                raise ValueError(field + ' is not confirmed empty')
        if entry_order_ids:
            raise ValueError('Existing exchange order ID requires investigation')
    except (ValueError, TypeError, KeyError):
        return dict(result, reason='Nonempty, missing, or stale exchange state')
    # NOTE: This is only a candidate check; a second atomic reservation and
    # validated protection / emergency close are needed before live order writes.
    return dict(result, status='PRECHECK_OBSERVED_ONLY',
                reason='No exchange exposure observed; execution NOT authorized')


def oneway_precheck_self_test():
    plan = prepare_dry_run(
        {'event_id':'SAFE-TEST-1','symbol':'BTCUSDT','side':'LONG',
         'entry':'80000','sl':'79700','tp1':'80600','tp2':'80900'},
        {'contract':'BTC_USDT','last_price':'80010','quanto_multiplier':'0.0001',
         'order_size_min':1,'order_size_max':1000000},
        {'equity_usdt':'300','open_position_count':0})
    empty = {'status':'READ_ONLY_SNAPSHOT_REVIEW_REQUIRED',
             'active_positions':0,'open_orders':0,'open_trigger_orders':0,
             'unreadable_position_rows':0,'unknown_trigger_count':0}
    call = lambda snap=empty, **kwargs: assess_oneway_execution_prerequisites(
        plan,snap,age_seconds=kwargs.pop('age_seconds',2),
        mode=kwargs.pop('mode','ONE_WAY_VERIFIED'),**kwargs)
    assert call()['status']=='PRECHECK_OBSERVED_ONLY' and not call()['safe_to_trade']
    assert call(mode='HEDGE')['status']=='BLOCKED'
    assert call(age_seconds=100)['status']=='BLOCKED'
    assert call(dict(empty, open_trigger_orders=1))['status']=='BLOCKED'
    assert call(dict(empty, unreadable_position_rows=1))['status']=='BLOCKED'
    assert call(dict(empty, unknown_trigger_count=1))['status']=='BLOCKED'
    assert call(entry_order_ids=['123'])['status']=='BLOCKED'
    assert not call()['live_ready']
    return {'oneway_mode_guard':'PASSED', 'stale_snapshot_block':'PASSED',
            'existing_orders_block':'PASSED','ambiguous_exchange_rows_block':'PASSED',
            'risk_admission':'OBSERVE_ONLY','live_ready':False}

if __name__ == '__main__':
    print(json.dumps({'risk_tests': offline_self_test(), 'protection_tests': offline_protection_test(),
                      'lifecycle_integration': lifecycle_integration_self_test(),
                      'gate_api_protocol': gate_api_protocol_self_test(),
                      'protection_snapshot_audit': protection_snapshot_self_test(),
                      'gate_readonly_reconciliation': gate_readonly_reconciliation_self_test(),
                       'execution_gateway': gate_execution_gateway_self_test(),
                       'oneway_precheck': oneway_precheck_self_test()}, indent=2))
