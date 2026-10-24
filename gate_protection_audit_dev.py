"""Gate.io futures protective-order READ-ONLY diagnostic.

Never places, cancels, or changes exchange orders. Never authorizes live entry.
Run with: python gate_protection_audit_dev.py   (offline fixtures only)
Only compare *known* exchange-native trigger order IDs after authenticated retrieval.
"""
from __future__ import annotations
from decimal import Decimal, InvalidOperation
import json


def _num(value):
    if isinstance(value, bool):
        raise ValueError('boolean price')
    try:
        x = Decimal(str(value))
    except (ValueError, InvalidOperation, TypeError):
        raise ValueError('invalid price') from None
    if not x.is_finite() or x <= 0:
        raise ValueError('invalid positive price')
    return x


def audit_protection(open_trigger_orders, *, contract, side, sl_id, tp_id, sl_price, tp_price):
    """Inspect GET /futures/usdt/price_orders?status=open result.

    Requires exact exchange-issued trigger IDs from protected-entry records.
    It is NOT sufficient to show that some unrelated stop orders exist.
    A match is observational, NOT a proof of future triggering/filling or safe-to-trade.
    """
    result = {'status': 'NOT_VERIFIED', 'sl_verified': False, 'tp_verified': False,
              'live_trading': False, 'orders_enabled': False, 'safe_to_trade': False}
    try:
        if not isinstance(open_trigger_orders, list):
            raise ValueError('Authenticated trigger-order list required')
        if side not in ('LONG', 'SHORT') or not isinstance(contract, str) or not contract.endswith('_USDT'):
            raise ValueError('Invalid contract or side')
        if not sl_id or not tp_id or str(sl_id) == str(tp_id):
            raise ValueError('Two distinct exchange order IDs required')
        stop, take = _num(sl_price), _num(tp_price)
        # The Gate API v4 defines rule 1 as price >= trigger; rule 2 as price <= trigger.
        expected = {'SL': (str(sl_id), stop, 2 if side == 'LONG' else 1),
                    'TP': (str(tp_id), take, 1 if side == 'LONG' else 2)}
        matching_type = 'close-long-position' if side == 'LONG' else 'close-short-position'
        for purpose, (order_id, target, rule) in expected.items():
            found = [x for x in open_trigger_orders if isinstance(x, dict) and
                     str(x.get('id_string') or x.get('id')) == order_id]
            if len(found) != 1:
                raise ValueError(f'{purpose}: exact open order ID missing or repeated')
            order = found[0]
            initial, trigger = order.get('initial'), order.get('trigger')
            if not isinstance(initial, dict) or not isinstance(trigger, dict):
                raise ValueError(f'{purpose}: incomplete order')
            if (order.get('status') != 'open' or initial.get('contract') != contract or
                    order.get('order_type') != matching_type or
                    int(trigger.get('rule', -1)) != rule or _num(trigger.get('price')) != target or
                    str(initial.get('size')) != '0' or str(initial.get('price')) != '0'):
                raise ValueError(f'{purpose}: protective terms mismatch')
            result[purpose.lower() + '_verified'] = True
        result['status'] = 'OBSERVED_BOTH_OPEN_NOT_EXECUTION_AUTHORIZATION'
        return result
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        result['status'] = 'NOT_VERIFIED'
        result['reason'] = str(exc)
        # Any failed verification invalidates coverage as a whole.
        result['sl_verified'] = False
        result['tp_verified'] = False
        return result


def _fixture(order_id, kind, direction, price, rule):
    return {'id': order_id, 'status': 'open',
            'order_type': 'close-long-position' if direction == 'LONG' else 'close-short-position',
            'initial': {'contract': 'BTC_USDT', 'size': 0, 'price': '0'},
            'trigger': {'price': str(price), 'rule': rule}}


def self_test():
    good = [_fixture(101, 'SL', 'LONG', '79000', 2), _fixture(102, 'TP', 'LONG', '82000', 1)]
    args = dict(contract='BTC_USDT', side='LONG', sl_id=101, tp_id=102,
                sl_price='79000', tp_price='82000')
    valid = audit_protection(good, **args)
    assert valid['status'].startswith('OBSERVED_BOTH') and valid['safe_to_trade'] is False
    missing = audit_protection(good[:1], **args)
    assert missing['status'] == 'NOT_VERIFIED'
    wrong = audit_protection([good[0], {**good[1], 'trigger': {'price': '82000', 'rule': 2}}], **args)
    assert wrong['status'] == 'NOT_VERIFIED'
    short = [_fixture(201, 'SL', 'SHORT', '81000', 1), _fixture(202, 'TP', 'SHORT', '78000', 2)]
    short_args = dict(contract='BTC_USDT', side='SHORT', sl_id=201, tp_id=202,
                      sl_price='81000', tp_price='78000')
    assert audit_protection(short, **short_args)['status'].startswith('OBSERVED_BOTH')
    return {'offline_protection_audit':'PASSED','missing_sl_tp':'BLOCKED',
            'incorrect_trigger':'BLOCKED','long_short':'PASSED',
            'exchange_authenticated':'NOT_TESTED','live_trading':False}


if __name__ == '__main__':
    print(json.dumps(self_test(), indent=2))
