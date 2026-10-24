"""LuxAlgo Gate.io ONE-TRADE protection reconciliation (READ-ONLY).

Connects authenticated Gate.io GET trigger-orders retrieval with the precise
SL/TP identity audit. This module CANNOT submit, cancel, or close any order.
Its output NEVER authorizes live trading. Exchange order IDs are required;
without them, no verification is possible.

Run: py gate_single_trade_protection_integrated.py
The default self-test is offline and uses synthetic fixtures only.
"""
from __future__ import annotations
import json
from gate_single_trade_transport_dev import authenticated_get
from gate_protection_audit_dev import audit_protection, _fixture


def check_exchange_protection(*, contract, side, sl_id, tp_id, sl_price, tp_price,
                              get_fn=authenticated_get):
    """Fetch real open trigger orders, then match IDs, triggers and contract.

    get_fn may be replaced only by a trusted fake for *offline tests*.
    Returns fail-closed on unavailable credentials/API or malformed responses.
    An observation is not execution authorization or fill guarantee.
    """
    blocked = {'status': 'NOT_VERIFIED', 'sl_verified': False,
               'tp_verified': False, 'safe_to_trade': False,
               'live_trading': False, 'orders_enabled': False}
    if not sl_id or not tp_id or str(sl_id) == str(tp_id):
        return {**blocked, 'reason': 'Distinct real exchange SL/TP order IDs required'}
    try:
        response = get_fn('/futures/usdt/price_orders', {'status': 'open'})
        if not isinstance(response, dict) or response.get('status') != 'READ_ONLY_RESPONSE':
            return {**blocked, 'reason': 'Authenticated exchange response unavailable'}
        orders = response.get('data')
        if not isinstance(orders, list):
            return {**blocked, 'reason': 'Unexpected exchange response format'}
        result = audit_protection(orders, contract=contract, side=side,
                                  sl_id=sl_id, tp_id=tp_id,
                                  sl_price=sl_price, tp_price=tp_price)
        # NOTE: observation of open orders is never permission to enter a trade.
        result['safe_to_trade'] = False
        result['orders_enabled'] = False
        result['live_trading'] = False
        return result
    except Exception as exc:
        # Don't expose authenticated URLs or service errors that could include secrets.
        return {**blocked, 'reason': 'Exchange read failed: ' + type(exc).__name__}


def self_test():
    orders = [_fixture(101, 'SL', 'LONG', '79000', 2),
              _fixture(102, 'TP', 'LONG', '82000', 1)]
    def fake_get(endpoint, params):
        assert endpoint == '/futures/usdt/price_orders'
        assert params == {'status': 'open'}
        return {'status': 'READ_ONLY_RESPONSE', 'data': orders}
    args = dict(contract='BTC_USDT', side='LONG', sl_id=101, tp_id=102,
                sl_price='79000', tp_price='82000')
    matched = check_exchange_protection(**args, get_fn=fake_get)
    assert matched['sl_verified'] and matched['tp_verified']
    assert not matched['safe_to_trade']
    absent = check_exchange_protection(**{**args, 'tp_id': 999}, get_fn=fake_get)
    assert absent['status'] == 'NOT_VERIFIED'
    missing_ids = check_exchange_protection(**{**args, 'sl_id': ''}, get_fn=fake_get)
    assert missing_ids['status'] == 'NOT_VERIFIED'
    bad_api = check_exchange_protection(**args, get_fn=lambda *_: {'status':'NOT_VERIFIED'})
    assert bad_api['status'] == 'NOT_VERIFIED'
    return {'integrated_offline_checks': 'PASSED',
            'exact_order_id_matching': 'PASSED',
            'missing_sl_tp': 'BLOCKED', 'unavailable_exchange': 'BLOCKED',
            'authenticated_exchange_verification': 'NOT_PERFORMED',
            'real_order_submission': 'BLOCKED', 'live_trading': False}


if __name__ == '__main__':
    print(json.dumps(self_test(), indent=2))
