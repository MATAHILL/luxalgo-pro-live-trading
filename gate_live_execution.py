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


# Phase 3: ISOLATED ORDER LIFECYCLE FIXTURE. No network calls, exchange writes,
# credential access or live-order activation. Never connect this state machine
# to a real API without separate authorization, persistent idempotency storage,
# and exchange-side protection verification.
class SimulatedOrderLedger:
    """In-memory, non-executable exercise for retries and unknown acknowledgments."""

    def __init__(self):
        self._records = {}

    def prepare(self, event_id, protected_preview):
        event_id = str(event_id).strip()
        if not event_id or len(event_id) > 128:
            raise ValueError('Invalid event ID')
        if (not isinstance(protected_preview, dict) or
                protected_preview.get('status') != 'PREVIEW_ONLY' or
                protected_preview.get('orders_enabled') is not False or
                protected_preview.get('safe_to_trade') is not False):
            return {'status': 'BLOCKED', 'reason': 'Non-executable preflight is required',
                    'orders_enabled': False}
        if event_id in self._records:
            return {'status': 'BLOCKED', 'reason': 'Duplicate event ID',
                    'orders_enabled': False}
        self._records[event_id] = 'PREPARED'
        return {'status': 'SIMULATED_PREPARED', 'event_id': event_id,
                'orders_enabled': False}

    def transition(self, event_id, event):
        state = self._records.get(event_id)
        if state is None:
            return {'status': 'BLOCKED', 'reason': 'Unknown event ID', 'orders_enabled': False}
        transitions = {
            ('PREPARED', 'MOCK_ACK'): 'SIMULATED_ACK',
            ('PREPARED', 'MOCK_TIMEOUT'): 'ACK_UNKNOWN',
            ('SIMULATED_ACK', 'MOCK_PROTECTION_MISSING'): 'REVIEW_REQUIRED',
            ('SIMULATED_ACK', 'MOCK_PROTECTION_PRESENT'): 'SIMULATED_PROTECTED',
            ('ACK_UNKNOWN', 'MOCK_RECONCILE'): 'REVIEW_REQUIRED',
            ('SIMULATED_PROTECTED', 'MOCK_CLOSE'): 'SIMULATED_CLOSED',
        }
        next_state = transitions.get((state, event))
        if next_state is None:
            return {'status': 'BLOCKED', 'reason': 'Invalid transition or replay',
                    'state': state, 'orders_enabled': False}
        self._records[event_id] = next_state
        return {'status': next_state, 'event_id': event_id,
                'exchange_protection_verified': False, 'orders_enabled': False}

    def snapshot(self):
        return {'records': dict(self._records), 'live_trading': False,
                'orders_enabled': False}


def phase3_offline_lifecycle_tests():
    signal = {'event_id': 'PHASE3-LONG', 'symbol': 'BTCUSDT', 'side': 'LONG',
              'entry': '80000', 'sl': '79700', 'tp1': '80600', 'tp2': '80900'}
    market = {'contract': 'BTC_USDT', 'last_price': '80010',
              'quanto_multiplier': '0.0001', 'order_size_min': 1, 'order_size_max': 1000000}
    account = {'equity_usdt': '300', 'open_position_count': 0}
    # Fixture values are intentionally fabricated: they DO NOT verify exchange protection.
    fixture = {'status': 'READ_ONLY_SNAPSHOT', 'active_positions': 0,
               'bot_order_reconciliation': 'VERIFIED',
               'protective_sl_tp_coverage': 'VERIFIED'}
    preview = preview_protected_order_intent(
        signal, market, account, fixture,
        expected_snapshot_age_seconds=1, current_market_age_seconds=1)
    assert preview['status'] == 'PREVIEW_ONLY'
    ledger = SimulatedOrderLedger()
    assert ledger.prepare('PHASE3-LONG', preview)['status'] == 'SIMULATED_PREPARED'
    assert ledger.prepare('PHASE3-LONG', preview)['status'] == 'BLOCKED'
    assert ledger.transition('PHASE3-LONG', 'MOCK_TIMEOUT')['status'] == 'ACK_UNKNOWN'
    assert ledger.transition('PHASE3-LONG', 'MOCK_ACK')['status'] == 'BLOCKED'
    assert ledger.transition('PHASE3-LONG', 'MOCK_RECONCILE')['status'] == 'REVIEW_REQUIRED'
    assert ledger.transition('PHASE3-LONG', 'MOCK_CLOSE')['status'] == 'BLOCKED'
    assert ledger.prepare('PHASE3-SECOND', preview)['status'] == 'SIMULATED_PREPARED'
    assert ledger.transition('PHASE3-SECOND', 'MOCK_ACK')['status'] == 'SIMULATED_ACK'
    assert ledger.transition('PHASE3-SECOND', 'MOCK_PROTECTION_MISSING')['status'] == 'REVIEW_REQUIRED'
    assert ledger.prepare('PHASE3-THIRD', preview)['status'] == 'SIMULATED_PREPARED'
    assert ledger.transition('PHASE3-THIRD', 'MOCK_ACK')['status'] == 'SIMULATED_ACK'
    assert ledger.transition('PHASE3-THIRD', 'MOCK_PROTECTION_PRESENT')['status'] == 'SIMULATED_PROTECTED'
    assert ledger.transition('PHASE3-THIRD', 'MOCK_CLOSE')['status'] == 'SIMULATED_CLOSED'
    assert ledger.snapshot()['orders_enabled'] is False
    for fn in (submit_live_order, close_live_position, reverse_live_position):
        try:
            fn()
        except RuntimeError:
            pass
        else:
            raise AssertionError('Real exchange operation unexpectedly permitted')
    return {'phase3_fixture_tests': 'PASSED',
            'duplicate_event': 'BLOCKED', 'unknown_ack_retry': 'BLOCKED',
            'unknown_ack_state': 'REVIEW_REQUIRED',
            'unprotected_mock_order': 'REVIEW_REQUIRED',
            'mock_protected_lifecycle': 'SIMULATED_CLOSED',
            'actual_exchange_protection_verified': False,
            'real_orders': 'BLOCKED'}




# Phase 4: Static, untrusted API shape examples. NO HTTP clients for order endpoints.
# Not executable instructions: hedge-mode flags, sizing, price precision, margin mode,
# and protection ordering must be independently established in exchange testnet.
def phase4_protection_blueprint(signal, market, account):
    """Return documentation-informed stop/TP shape PREVIEWS, never trade instructions."""
    sizing = prepare_dry_run(signal, market, account)
    if sizing.get('status') != 'VALID_DRY_RUN_NOT_EXECUTABLE':
        return {'status': 'BLOCKED', 'reason': sizing.get('reason'), 'orders_enabled': False}
    direction = sizing['side']
    contract = sizing['contract']
    # stop below/above market and profit target above/below entry respectively.
    def view(level, kind):
        price = sizing[level]
        # Gate rule 1 => >= trigger, rule 2 => <= trigger.
        rule = (2 if direction == 'LONG' else 1) if kind == 'SL' else (1 if direction == 'LONG' else 2)
        return {
            'purpose': kind,
            'endpoint_reference': 'POST /futures/usdt/price_orders',
            'order_type_reference': 'close-long-position' if direction == 'LONG' else 'close-short-position',
            'initial_reference': {'contract': contract, 'price': '0', 'size': '0'},
            'trigger_reference': {'strategy_type': 0, 'price_type': 1,
                                  'price': price, 'rule': rule},
            'NOT_A_SUBMITTABLE_PAYLOAD': True,
        }
    return {
        'status': 'OFFLINE_BLUEPRINT_ONLY',
        'direction': direction, 'contract': contract,
        'stop_loss_preview': view('stop_loss', 'SL'),
        'tp1_preview': view('take_profit_1', 'TP'),
        'tp2_preview': view('take_profit_2', 'TP'),
        'warnings': [
            'Illustrative Gate.io request-field references, NOT final accepted API payloads',
            'Dual-mode/hedge-mode behavior and margins not verified',
            'TP1/TP2 partial-closing distribution not implemented',
            'Trigger price precision, current mark price, exchange metadata and stale checks not verified',
            'Race conditions, competing full-close triggers, and emergency stop not addressed',
            'No SL/TP protection exists until exchange confirms it on testnet or live exchange',
        ],
        'exchange_protection_verified': False,
        'safe_to_trade': False,
        'live_trading': False,
        'orders_enabled': False,
    }


def phase4_offline_blueprint_tests():
    market = {'contract':'BTC_USDT', 'last_price':'80010',
              'quanto_multiplier':'0.0001', 'order_size_min':1, 'order_size_max':1000000}
    account = {'equity_usdt':'300', 'open_position_count':0}
    long_signal = {'event_id':'P4-LONG', 'symbol':'BTCUSDT', 'side':'LONG',
                   'entry':'80000', 'sl':'79700', 'tp1':'80600', 'tp2':'80900'}
    short_signal = {'event_id':'P4-SHORT', 'symbol':'BTCUSDT', 'side':'SHORT',
                    'entry':'80000', 'sl':'80300', 'tp1':'79400', 'tp2':'79100'}
    long = phase4_protection_blueprint(long_signal,market,account)
    short = phase4_protection_blueprint(short_signal,market,account)
    assert long['status'] == short['status'] == 'OFFLINE_BLUEPRINT_ONLY'
    assert long['stop_loss_preview']['trigger_reference']['rule'] == 2
    assert long['tp1_preview']['trigger_reference']['rule'] == 1
    assert short['stop_loss_preview']['trigger_reference']['rule'] == 1
    assert short['tp1_preview']['trigger_reference']['rule'] == 2
    for blueprint in (long,short):
        assert blueprint['orders_enabled'] is False and blueprint['safe_to_trade'] is False
        assert all(x['NOT_A_SUBMITTABLE_PAYLOAD'] is True for x in
                   (blueprint['stop_loss_preview'],blueprint['tp1_preview'],blueprint['tp2_preview']))
    bad = phase4_protection_blueprint(dict(long_signal,sl='81000'),market,account)
    assert bad['status'] == 'BLOCKED'
    return {'offline_blueprint_tests':'PASSED', 'long_rules':'PASSED',
            'short_rules':'PASSED', 'invalid_levels':'BLOCKED',
            'testnet_execution':'NOT_TESTED', 'real_orders':'BLOCKED'}



# One-trade exchange integration: REAL order payload validation only.
# NO exchange write method is invoked or exposed here.
# Gate.io's one-way and hedge-mode close semantics differ; refusing to guess.
def build_single_trade_gate_payloads(signal, market, account, *, position_mode):
    """Build strictly non-executable example payloads; check against exchange
    metadata and a verified account mode before implementation of actual writes.
    """
    base = prepare_dry_run(signal, market, account)
    if base.get('status') != 'VALID_DRY_RUN_NOT_EXECUTABLE':
        return {'status':'BLOCKED', 'reason':base.get('reason'), 'orders_enabled':False}
    if position_mode not in ('single','dual'):
        return {'status':'BLOCKED', 'reason':'Account position mode must be verified', 'orders_enabled':False}
    side=base['side']
    qty=base['contracts'] if side=='LONG' else -base['contracts']
    contract=base['contract']
    entry={'contract':contract,'size':qty,'price':'0','tif':'ioc',
           'text':'t-luxalgo-one-trade'}
    trigger_direction={'LONG': {'SL':2,'TP':1},'SHORT':{'SL':1,'TP':2}}[side]
    def protection(kind, level):
        initial={'contract':contract,'size':0,'price':'0','tif':'ioc',
                 'reduce_only':True}
        if position_mode=='dual':
            initial['auto_size']='close_long' if side=='LONG' else 'close_short'
        else:
            initial['close']=True
        return {'initial':initial,'trigger':{'strategy_type':0,'price_type':1,
                 'price':base[level],'rule':trigger_direction[kind],
                 'expiration':86400},
                'order_type':'close-long-position' if side=='LONG' else 'close-short-position'}
    return {'status':'PAYLOAD_PREVIEW_ONLY', 'entry':entry,
            'stop_loss':protection('SL','stop_loss'),
            'take_profit':protection('TP','take_profit_1'),
            'position_mode':position_mode,
            'exchange_order_confirmed':False,
            'protective_orders_confirmed':False,
            'entry_fill_confirmed':False,
            'halt_after_close_implemented':False,
            'orders_enabled':False,'live_trading':False,
            'warning':'Never POST these previews. Current code lacks exchange write and recovery orchestration.'}


def single_trade_payload_tests():
    market={'contract':'BTC_USDT','last_price':'80010','quanto_multiplier':'0.0001',
            'order_size_min':1,'order_size_max':1000000}
    account={'equity_usdt':'300','open_position_count':0}
    for direction, sl, tp in [('LONG','79700','80600'),('SHORT','80300','79400')]:
        signal={'event_id':'SINGLE-TEST','symbol':'BTCUSDT','side':direction,
                'entry':'80000','sl':sl,'tp1':tp,
                'tp2':'80900' if direction=='LONG' else '79100'}
        for mode in ('single','dual'):
            result=build_single_trade_gate_payloads(signal,market,account,position_mode=mode)
            assert result['status']=='PAYLOAD_PREVIEW_ONLY',result
            assert result['entry']['size']*(1 if direction=='LONG' else -1)>0
            assert result['stop_loss']['initial'].get('auto_size') == (
                ('close_long' if direction=='LONG' else 'close_short') if mode=='dual' else None)
            assert result['orders_enabled'] is False
    assert build_single_trade_gate_payloads(signal,market,account,
                                            position_mode='unknown')['status']=='BLOCKED'
    return {'one_trade_payload_checks':'PASSED','hedge_and_one_way':'PASSED',
            'real_gate_orders':'BLOCKED','protection_exchange_verified':False}

if __name__ == '__main__':
    print(json.dumps({'risk_tests': offline_self_test(),
                      'protection_tests': offline_protection_test(),
                      'phase3_lifecycle_tests': phase3_offline_lifecycle_tests(),
                      'phase4_blueprint_tests': phase4_offline_blueprint_tests(),
                       'single_trade_payload_tests': single_trade_payload_tests()}, indent=2))
