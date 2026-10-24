"""Gate.io ONE-WAY Futures single-trade adapter; DEVELOPMENT, NO EXCHANGE WRITES.

Builds exact-shape entry and protective close payloads from independently validated
levels. A real system must first verify account one-way mode, current contract
metadata, fills and exchange-created protective orders, and must implement a
persisted kill-switch/idempotency control before permitting POST.
"""
from __future__ import annotations
from decimal import Decimal, InvalidOperation
import json
import re

LIVE_TRADING_ENABLED = False
ORDERS_ENABLED = False

class TradeBlocked(ValueError):
    pass

def positive(x, label):
    try:
        d = Decimal(str(x))
    except (InvalidOperation, ValueError):
        raise TradeBlocked(f'{label}: invalid number') from None
    if not d.is_finite() or d <= 0:
        raise TradeBlocked(f'{label}: must be positive and finite')
    return d

def create_oneway_plan(*, event_id, contract, side, contracts, entry, stop_loss,
                       take_profit, exchange_mode_verified=False, open_positions=None):
    """Prepare non-executable ONE-WAY payloads. Never authorizes an exchange write."""
    if exchange_mode_verified is not True:
        raise TradeBlocked('Account ONE-WAY mode must be verified from exchange')
    if type(open_positions) is not int or open_positions != 0:
        raise TradeBlocked('Exactly zero exchange positions required')
    if not isinstance(event_id, str) or not re.fullmatch('[A-Za-z0-9-]{1,80}', event_id):
        raise TradeBlocked('Invalid unique signal ID')
    if not isinstance(contract, str) or not re.fullmatch('[A-Z0-9]{2,24}_USDT', contract):
        raise TradeBlocked('Invalid USDT contract')
    if side not in ('LONG', 'SHORT'):
        raise TradeBlocked('Direction must be LONG or SHORT')
    if type(contracts) is not int or contracts < 1:
        raise TradeBlocked('Contract quantity must be a positive integer')
    ent, sl, tp = (positive(entry,'entry'),positive(stop_loss,'stop loss'),positive(take_profit,'take profit'))
    if not ((sl < ent < tp) if side == 'LONG' else (tp < ent < sl)):
        raise TradeBlocked('Wrong LONG/SHORT SL/TP order')
    signed_size = contracts if side == 'LONG' else -contracts
    # Order identifiers are references only; durable uniqueness must be implemented in DB.
    # 'text' is deliberately omitted to avoid claiming validated idempotency.
    entry_payload = {
        'contract': contract, 'size': signed_size, 'price': '0', 'tif': 'ioc',
        'tpsl_tp_trigger_price': str(tp), 'tpsl_sl_trigger_price': str(sl),
    }
    # Gate.io price_orders ONE-WAY full-position exits: size=0, close=true,
    # no hedge-mode auto_size. Trade-side trigger rules are direction-specific.
    def protective(level, rule):
        return {
            'initial': {'contract': contract, 'size': 0, 'price': '0',
                        'tif': 'ioc', 'close': True, 'reduce_only': True},
            'trigger': {'strategy_type': 0, 'price_type': 1,
                        'price': str(level), 'rule': rule, 'expiration': 86400},
        }
    return {
        'status': 'BLUEPRINT_ONLY', 'event_id': event_id, 'position_mode': 'ONE-WAY',
        'entry_route': 'POST /api/v4/futures/usdt/orders', 'entry_payload': entry_payload,
        'stop_route': 'POST /api/v4/futures/usdt/price_orders',
        'stop_payload': protective(sl, 2 if side == 'LONG' else 1),
        'take_profit_payload': protective(tp, 1 if side == 'LONG' else 2),
        'execution_allowed': False, 'exchange_sl_tp_verified': False,
        'warnings': [
            'Never submit both attached entry TP/SL and separate triggers without a deduplication strategy',
            'Live contract precision, account mode, leverage and margin must be verified',
            'Actual fill and protective close linkage must be reconciled after execution',
            'No persistence, emergency close, or exchange-side end-to-end validation yet',
        ],
    }

def submit_live_order(*args, **kwargs):
    raise RuntimeError('REAL ORDER BLOCKED: verified protection lifecycle not integrated')

def test():
    common = dict(event_id='SIGNAL-123',contract='BTC_USDT',contracts=2,entry='80000',
                  exchange_mode_verified=True,open_positions=0)
    a=create_oneway_plan(side='LONG',stop_loss='79700',take_profit='80600',**common)
    b=create_oneway_plan(side='SHORT',stop_loss='80300',take_profit='79400',**common)
    assert a['entry_payload']['size']==2 and b['entry_payload']['size']==-2
    assert a['stop_payload']['trigger']['rule']==2 and a['take_profit_payload']['trigger']['rule']==1
    assert b['stop_payload']['trigger']['rule']==1 and b['take_profit_payload']['trigger']['rule']==2
    for obj in (a,b):
        assert obj['stop_payload']['initial']['close'] is True
        assert obj['take_profit_payload']['initial']['size']==0
        assert 'auto_size' not in obj['stop_payload']['initial']
        assert not obj['execution_allowed']
    blocked=0
    for extra in ({'exchange_mode_verified':False},{'open_positions':1},{'contracts':0},
                  {'stop_loss':'81000','take_profit':'82000'}):
        try:
            create_oneway_plan(**(dict(common,side='LONG',stop_loss='79700',take_profit='80600')|extra))
        except TradeBlocked:
            blocked+=1
    assert blocked==4
    try: submit_live_order()
    except RuntimeError: pass
    else: raise AssertionError('Real POST unexpectedly permitted')
    return {'oneway_long_short_payloads':'PASSED', 'oneway_full_close_rules':'PASSED',
            'mode_and_position_guards':'PASSED', 'invalid_levels':'BLOCKED',
            'real_order_submission':'BLOCKED', 'exchange_protection':'NOT_VERIFIED',
            'live_trading':False}

if __name__=='__main__':
    print(json.dumps(test(),indent=2))
