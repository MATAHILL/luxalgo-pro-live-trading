"""Mock-only XRP single-order scope tests; NO Gate clients, money or live approvals."""
import unittest
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from xrp_scoped_acceptance_preflight import BoundedOwnerScope, assess_xrp_single_order

NOW=datetime(2026,11,1,6,0,30,tzinfo=timezone.utc)
OWNER=BoundedOwnerScope(contract="XRP_USDT",side="LONG",
    start_utc=NOW-timedelta(seconds=10),end_utc=NOW+timedelta(seconds=20),
    approval_reference="EXAMPLE_REFERENCE_DOES_NOT_AUTHORIZE_ORDERS")

def scenario():
    return dict(exchange="GATE_IO_LIVE_USDT_FUTURES",contract="XRP_USDT",
      side="LONG",contracts=1,exchange_min_contracts="0.1",
      exchange_max_contracts="100", account_authenticated=True,
      xrp_metadata_authenticated=True,native_sl_tp_capability_verified=True,
      emergency_close_recovery_certified=True,
      emergency_halt_reconciled_and_cleared=True,
      operator_authorized_for_one_order=True,no_unknown_exchange_ack=True,
      exchange_snapshot_utc=NOW-timedelta(seconds=10),
      open_positions=0,open_orders=0,open_triggers=0,reserved_orders=0,
      xrp_delisting=False,signal_side="LONG",signal_contract="XRP_USDT",
      signal_levels="VALID",order_type="MARKET",timeframe="5m",
      tv_bar_id="TVBAR_5m_00059773000",signal_received_utc=NOW-timedelta(seconds=5),
      actual_leverage="5",max_entry_slippage_ratio="0.001",
      taker_fee_rate="0.00075",entry="2.5",sl="2.48",tp="2.7",
      mark_price="2.5",quanto_multiplier="1",
      equity_usdt="100",available_usdt="100",required_margin_usdt="0.6")

class OneOrderScopeTests(unittest.TestCase):
    def reject(self, **updates):
        data=scenario();data.update(updates)
        result=assess_xrp_single_order(data,OWNER,NOW)
        self.assertEqual(result["status"],"REJECT_NO_ORDER",result)
        self.assertEqual(result["orders_sent"],0)
        self.assertFalse(result["can_execute"])
        return result

    def test_mock_complete_evidence_only_yields_dry_status(self):
        x=assess_xrp_single_order(scenario(),OWNER,NOW)
        self.assertEqual(x["status"],"DRY_PREFLIGHT_PASS_NOT_ORDER_AUTHORITY")
        self.assertFalse(x["can_execute"])
        self.assertFalse(x["exchange_writes_enabled"])
        self.assertFalse(x["continuous_trading_authorized"])
        self.assertEqual(x["orders_sent"],0)
        self.assertLessEqual(float(x["modeled_loss_usdt"]),0.1)

    def test_old_owner_time_or_unbounded_scope_never_passes(self):
        for at in (OWNER.start_utc-timedelta(seconds=1),OWNER.end_utc,NOW+timedelta(days=2)):
            self.assertEqual(assess_xrp_single_order(scenario(),OWNER,at)["status"],"REJECT_NO_ORDER")
        for scope in (None,replace(OWNER,end_utc=OWNER.start_utc),
                      replace(OWNER,end_utc=OWNER.start_utc+timedelta(hours=2)),
                      replace(OWNER,approval_reference=""),
                      replace(OWNER,approval_reference="TEST_ONLY_fake"),
                      replace(OWNER,max_margin="2.0"),
                      replace(OWNER,max_leverage="6"),
                      replace(OWNER,contract="BTC_USDT")):
            with self.subTest(scope=scope):
                self.assertEqual(assess_xrp_single_order(scenario(),scope,NOW)["status"],"REJECT_NO_ORDER")

    def test_missing_any_one_safety_gate_must_reject(self):
        for field in ("account_authenticated","xrp_metadata_authenticated",
                      "native_sl_tp_capability_verified",
                      "emergency_close_recovery_certified",
                      "emergency_halt_reconciled_and_cleared",
                      "operator_authorized_for_one_order","no_unknown_exchange_ack"):
            with self.subTest(field=field):self.reject(**{field:False})

    def test_bad_lot_limits_signal_or_position_rejected(self):
        cases=[
            dict(contracts=2),dict(contracts="0.1"),
            dict(exchange_min_contracts="0.3"),dict(exchange_min_contracts="1.1"),
            dict(exchange_min_contracts=0),dict(exchange_max_contracts="0.9"),
            dict(signal_side="SHORT"),dict(signal_contract="BTC_USDT"),
            dict(signal_levels="MISSING"),dict(order_type=None),
            dict(tv_bar_id=None),dict(timeframe="1m"),
            dict(signal_received_utc=NOW-timedelta(seconds=61)),
            dict(exchange_snapshot_utc=NOW-timedelta(seconds=31)),
            dict(open_orders=1),dict(open_positions=1),
            dict(open_triggers=1),dict(reserved_orders=1),
            dict(xrp_delisting=None),
        ]
        for case in cases:
            with self.subTest(case=case):self.reject(**case)

    def test_exact_owner_risk_financial_caps_failclosed(self):
        for case in [dict(required_margin_usdt="1.71"),
                     dict(available_usdt="0.1"),dict(entry="9",mark_price="9",tp="10",sl="8.99"),
                     dict(sl="2.0"),dict(tp="2.4"),dict(mark_price="2.75"),
                     dict(actual_leverage="5.1"),
                     dict(max_entry_slippage_ratio="0.00101"),
                     dict(taker_fee_rate="NaN"),dict(equity_usdt="-1"),
                     dict(equity_usdt="1"),dict(contracts=True)]:
            with self.subTest(case=case):self.reject(**case)

if __name__=="__main__":unittest.main()
