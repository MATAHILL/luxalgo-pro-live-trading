"""Offline configuration, sizing and atomic risk regressions; zero orders."""
import os
import unittest
from decimal import Decimal
from unittest.mock import patch, MagicMock
from live_risk_policy import per_entry_risk, policy_status
from live_futures_engine import build_plan, verify_execution_preflight, LiveSafetyError
from live_execution_coordinator import risk_reserve


class RiskPolicyTests(unittest.TestCase):
    def test_invalid_configuration_fails_closed(self):
        for value in ('', 'NaN', 'Infinity', '-1', '0', '3.01', 'true'):
            with self.subTest(value=value), patch.dict(os.environ, LUXALGO_LIVE_RISK_PERCENT=value):
                with self.assertRaises(ValueError):
                    per_entry_risk()

    def test_requested_cap_and_existing_aggregate_are_distinct(self):
        with patch.dict(os.environ, LUXALGO_LIVE_RISK_PERCENT='3'):
            self.assertEqual(per_entry_risk(), Decimal('.03'))
            self.assertEqual(policy_status()['aggregate_risk_percent'], '2.5')

    def test_three_percent_policy_does_not_bypass_atomic_aggregate(self):
        con = MagicMock()
        cur = con.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = dict(halted=False)
        cur.fetchall.return_value = []
        with patch.dict(os.environ, LUXALGO_LIVE_RISK_PERCENT='3'):
            with self.assertRaisesRegex(LiveSafetyError, 'account-wide'):
                risk_reserve(con, dict(contract='BTC_USDT', estimated_risk_usdt='26'), [], '1000')
        self.assertFalse(any('INSERT INTO' in str(c) for c in cur.execute.call_args_list))

    def test_margin_limited_plan_passes_same_price_preflight(self):
        signal = dict(event_id='tv-margin-test-123', contract='BTC_USDT', side='LONG',
                      timeframe='5m', levels_status='VALID', entry='80000', sl='79000',
                      tp='82000', leverage=5, position_mode='single', received_epoch=1000)
        meta = dict(name='BTC_USDT', status='trading', in_delisting=False,
                    order_size_min=1, order_size_max=100000, market_order_size_max=100000,
                    order_price_round='.1', leverage_min='1', leverage_max='100',
                    mark_price='80000', quanto_multiplier='.0001', taker_fee_rate='.00075')
        with patch.dict(os.environ, LUXALGO_LIVE_RISK_PERCENT='3'):
            plan = build_plan(signal, meta, '1000', '16', [], now=1001)
            self.assertEqual(plan['contracts'], 9)
            self.assertEqual((plan['entry'], plan['sl'], plan['tp']), ('80000', '79000', '82000'))
            snapshot = dict(account=dict(currency='USDT', total='1000', unrealised_pnl='0',
                                        available='16', position_mode='single'),
                            position_settings=[dict(contract='BTC_USDT', mode='single', leverage='5')])
            self.assertTrue(verify_execution_preflight(plan, meta, snapshot, now=1001)['verified'])
