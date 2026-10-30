"""Fresh contract/account configuration checks, without network or orders."""
import unittest
from live_futures_engine import verify_execution_preflight, LiveSafetyError

class FreshExecutionPreflight(unittest.TestCase):
    def setUp(self):
        self.plan=dict(received_epoch=1000,contract='BTC_USDT',contracts=1,side='LONG',
                       entry='80000',sl='79000',tp='82000',position_mode='single',
                       leverage='5',max_entry_slippage_ratio='0.001',estimated_risk_usdt='0.14')
        self.meta=dict(name='BTC_USDT',status='trading',in_delisting=False,
                       order_size_min=1,order_size_max=100,market_order_size_max=10,
                       order_price_round='0.1',leverage_min='1',leverage_max='100',
                       mark_price='80000',quanto_multiplier='0.0001',taker_fee_rate='0.00075')
        self.snapshot=dict(account=dict(currency='USDT',position_mode='single',
                            total='100',unrealised_pnl='0',available='90'),
                            position_settings=[dict(contract='BTC_USDT',mode='single',leverage='5')])
    def check(self):
        return verify_execution_preflight(self.plan,self.meta,self.snapshot,now=1001)
    def test_verified_settings_pass_without_orders(self):
        self.assertEqual(self.check()['orders_sent'],0)
    def test_actual_leverage_must_match(self):
        for leverage in ('20','3',None,'NaN'):
            self.snapshot['position_settings'][0]['leverage']=leverage
            with self.subTest(leverage=leverage),self.assertRaises(LiveSafetyError):self.check()
    def test_cross_margin_limit_readback(self):
        self.snapshot['position_settings'][0].update(leverage='0',cross_leverage_limit='5')
        self.assertTrue(self.check()['verified'])
        del self.snapshot['position_settings'][0]['cross_leverage_limit']
        with self.assertRaises(LiveSafetyError):self.check()
    def test_missing_contract_settings_rejected(self):
        self.snapshot['position_settings']=[]
        with self.assertRaises(LiveSafetyError):self.check()
    def test_market_lot_limit_and_zero_fallback(self):
        self.meta['market_order_size_max']=0
        self.assertTrue(self.check()['verified'])
        self.meta['market_order_size_max']='0.5'
        with self.assertRaises(LiveSafetyError):self.check()
    def test_no_rounding_of_original_protective_levels(self):
        self.plan['sl']='79000.01'
        with self.assertRaisesRegex(LiveSafetyError,'off tick'):self.check()
        self.assertEqual(self.plan['sl'],'79000.01')
    def test_stale_signal_rejected(self):
        with self.assertRaisesRegex(LiveSafetyError,'expired'):
            verify_execution_preflight(self.plan,self.meta,self.snapshot,now=1061)
    def test_delisting_and_unknown_status_rejected(self):
        self.meta['status']='delisting'
        with self.assertRaises(LiveSafetyError):self.check()
    def test_mode_change_rejected(self):
        self.snapshot['account']['position_mode']='dual'
        with self.assertRaises(LiveSafetyError):self.check()
    def test_margin_fee_buffer_enforced(self):
        self.snapshot['account']['available']='1.6'
        with self.assertRaisesRegex(LiveSafetyError,'margin'):self.check()
    def test_fresh_contract_risk_cannot_exceed_reservation(self):
        self.meta['quanto_multiplier']='0.0002'
        with self.assertRaises(LiveSafetyError):self.check()
