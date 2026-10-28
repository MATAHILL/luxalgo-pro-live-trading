"""Gate.io verified hedge-mode protective orders: no exchange writes."""
import unittest
from live_futures_engine import (
    build_plan,build_protective_trigger,evaluate_protection,LiveSafetyError,
    verified_account_equity,build_entry_order)
from live_execution_orchestrator import full_close_oneway_order
NOW=1800000000
CONTRACT={"name":"BTC_USDT","quanto_multiplier":"0.0001","order_size_min":1,"order_size_max":100000,"taker_fee_rate":"0.00075"
def plan(side="LONG",mode="dual"):
    return build_plan(dict(event_id="tv-exchange-mode-20261028",contract="BTC_USDT",
                side=side,timeframe="5m",levels_status="VALID",position_mode=mode,
                entry="80000",sl="79200" if side=="LONG" else "80800",
                tp="81600" if side=="LONG" else "78400",leverage=5,
                received_epoch=NOW),CONTRACT,70,70,[],now=NOW)

class ModeProtection(unittest.TestCase):
    def test_verified_dual_long_position_sl_tp(self):
        p=plan()
        sl=build_protective_trigger(p,"SL")
        tp=build_protective_trigger(p,"TP")
        self.assertEqual(sl["initial"]["auto_size"],"close_long")
        self.assertNotIn("close",sl["initial"])
        self.assertEqual(sl["trigger"]["rule"],2)
        self.assertEqual(tp["trigger"]["rule"],1)
        orders=[dict(sl,status="open",id=1001),dict(tp,status="open",id=1002)]
        self.assertTrue(evaluate_protection(p,orders)["verified"])
        self.assertEqual(full_close_oneway_order(p)["auto_size"],"close_long")

    def test_verified_dual_short_position_sl_tp(self):
        p=plan("SHORT")
        sl=build_protective_trigger(p,"SL")
        tp=build_protective_trigger(p,"TP")
        self.assertEqual(sl["initial"]["auto_size"],"close_short")
        self.assertEqual(sl["trigger"]["rule"],1)
        self.assertEqual(tp["trigger"]["rule"],2)
        self.assertEqual(full_close_oneway_order(p)["auto_size"],"close_short")

    def test_no_auto_size_means_no_hedge_protection_proof(self):
        p=plan()
        sl=build_protective_trigger(p,"SL")
        tp=build_protective_trigger(p,"TP")
        for x in (sl,tp): x["initial"].pop("auto_size")
        self.assertFalse(evaluate_protection(
            p,[dict(sl,id=1001,status="open"),dict(tp,id=1002,status="open")])["verified"])

    def test_unknown_and_split_position_mode_forbidden(self):
        for mode in ("dual_plus","UNKNOWN",None):
            with self.subTest(mode=mode),self.assertRaises(LiveSafetyError):
                plan(mode=mode)

    def test_zero_minimum_size_not_manipulated(self):
        p=plan()
        self.assertEqual(p["position_mode"],"dual")
        self.assertGreaterEqual(p["contracts"],1)
        self.assertFalse(p["verified_live_order"])

    def test_real_contract_minimum_cannot_bypass_risk_cap(self):
        s=dict(event_id="tv-too-wide-stop-0001",contract="BTC_USDT",
               side="LONG",timeframe="5m",levels_status="VALID",
               position_mode="dual",received_epoch=NOW,leverage=5,
               entry="81502.24",sl="76000",tp="90000")
        with self.assertRaisesRegex(LiveSafetyError,"Insufficient margin/risk"):
            build_plan(s,CONTRACT,"70.00000001","70.00000001",[],now=NOW)

    def test_negative_unrealized_pnl_reduces_risk_equity(self):
        equity,available=verified_account_equity(dict(
            currency="USDT",total="70",unrealised_pnl="-10",available="55",
            position_mode="dual"))
        self.assertEqual(str(equity),"60")
        self.assertEqual(str(available),"55")

    def test_source_locked_order_has_bounded_slippage(self):
        p=plan()
        self.assertEqual(build_entry_order(p)["market_order_slip_ratio"],"0.001")
        p["max_entry_slippage_ratio"]="0.02"
        with self.assertRaisesRegex(LiveSafetyError,"slippage"):
            build_entry_order(p)

if __name__=="__main__":unittest.main()
