import unittest
from live_position_sizing import propose_live_contract_size, SizingBlocked

BASE=dict(side="LONG",entry="100",stop="99",equity_usdt="1000",
          available_usdt="500",multiplier="0.01",order_size_min="1",
          order_size_max="10000",open_positions=2,
          already_reserved_stop_risk_usdt="3")
class LiveSizingTests(unittest.TestCase):
    def test_returns_integer_contract_not_a_trade(self):
        r=propose_live_contract_size(**BASE)
        self.assertEqual(r["status"],"READ_ONLY_PROPOSAL")
        self.assertEqual(r["orders_submitted"],0)
        self.assertEqual(r["size_contracts"],500)
        self.assertEqual(r["underlying_quantity"],"5.00")
        self.assertEqual(r["planned_stop_loss_risk_usdt"],"5.00")
        self.assertEqual(r["estimated_initial_margin_usdt"],"100.00")
    def test_short_quantity_is_negative_and_uses_stop_distance(self):
        r=propose_live_contract_size(**{**BASE,"side":"SHORT","stop":"101"})
        self.assertEqual(r["size_contracts"],-500)
    def test_hard_block_at_five_positions(self):
        with self.assertRaisesRegex(SizingBlocked,"POSITION_CAPACITY"):
            propose_live_contract_size(**{**BASE,"open_positions":5})
    def test_hard_block_when_minimum_more_than_budgets(self):
        with self.assertRaisesRegex(SizingBlocked,"MINIMUM_CONTRACT"):
            propose_live_contract_size(**{**BASE,"order_size_min":"501"})
    def test_hard_block_on_trading_risk_above_half_percent(self):
        with self.assertRaisesRegex(SizingBlocked,"RISK_EXCEEDS"):
            propose_live_contract_size(**{**BASE,"risk_fraction":"0.03"})
    def test_hard_block_on_aggregate_risk(self):
        with self.assertRaisesRegex(SizingBlocked,"AGGREGATE"):
            propose_live_contract_size(**{**BASE,"already_reserved_stop_risk_usdt":"25"})
    def test_hard_block_on_nonfinite_or_inverted_stop(self):
        for kw in ({"entry":"NaN"},{"stop":"0"},{"stop":"100"},{"stop":"101"}):
            with self.subTest(kw=kw),self.assertRaises(SizingBlocked):
                propose_live_contract_size(**{**BASE,**kw})
    def test_cannot_exceed_per_contract_exchange_max(self):
        r=propose_live_contract_size(**{**BASE,"order_size_max":"3"})
        self.assertEqual(r["size_contracts"],3)
    def test_insufficient_available_margin_fails_closed(self):
        with self.assertRaisesRegex(SizingBlocked,"MINIMUM_CONTRACT"):
            propose_live_contract_size(**{**BASE,"available_usdt":"0.01"})
    def test_bool_and_floats_never_authorize_order(self):
        with self.assertRaises(SizingBlocked):
            propose_live_contract_size(**{**BASE,"equity_usdt":True})
        self.assertEqual(propose_live_contract_size(**BASE)["orders_submitted"],0)
if __name__=="__main__":unittest.main()
