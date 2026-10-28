"""Owner-approved BTC minimum-lot time-bounded preflight: simulated only."""
import unittest
from copy import deepcopy
from datetime import datetime,timezone,timedelta
from limited_acceptance_preflight import assess_one_order_scope

NOW=datetime(2026,10,29,4,35,tzinfo=timezone.utc)
def scenario():
    return dict(exchange="GATE_IO_LIVE_USDT_FUTURES",contract="BTC_USDT",
        side="LONG",contracts=1,exchange_min_contracts=1,position_mode="dual",
        account_readback_verified=True,metadata_readback_verified=True,
        protective_trigger_capability_verified=True,
        account_snapshot_utc=NOW-timedelta(seconds=10),
        contract_delisting=False,
        existing_positions=0,existing_orders=0,existing_triggers=0,
        unknown_exchange_ack=False,
        signal_level_status="VALID",order_type="MARKET",
        signal_side="LONG",signal_contract="BTC_USDT",
        timeframe="5m",tv_bar_id="TVBAR_5m_00005977339",
        signal_received_utc=NOW-timedelta(seconds=15),
        leverage=5,max_entry_slippage_ratio="0.001",
        exchange_required_margin_usdt="1.64",
        equity_usdt="170",available_usdt="170",
        quanto_multiplier="0.0001",mark_price="81000",
        entry="81000",stop_loss="80700",take_profit="81600",
        taker_fee_rate="0.00075")

class ScopeTests(unittest.TestCase):
    def rejected(self,**changes):
        s=scenario();s.update(changes)
        result=assess_one_order_scope(s,NOW)
        self.assertEqual(result["status"],"REJECT_NO_ORDER",result)
        self.assertEqual(result["exchange_orders_sent"],0)
        self.assertFalse(result["unrestricted_bot_release"])
        return result
    def test_only_one_precise_case_dry_preflight_passes(self):
        result=assess_one_order_scope(scenario(),NOW)
        self.assertEqual(result["status"],"DRY_PREFLIGHT_PASS_NOT_ORDER_AUTHORITY")
        self.assertEqual(result["exchange_orders_sent"],0)
        self.assertFalse(result["unrestricted_bot_release"])
        self.assertLessEqual(float(result["modeled_loss_usdt"]),0.10)
    def test_authorized_window_not_started(self):
        self.assertEqual(assess_one_order_scope(scenario(),
            datetime(2026,10,29,4,29,59,tzinfo=timezone.utc))["status"],"REJECT_NO_ORDER")
    def test_authorized_window_expired(self):
        self.assertEqual(assess_one_order_scope(scenario(),
            datetime(2026,10,29,4,45,tzinfo=timezone.utc))["status"],"REJECT_NO_ORDER")
    def test_only_btc_long_and_one_minimum_contract(self):
        for x in (dict(side="SHORT"),dict(contract="ETH_USDT"),
                  dict(contracts=2),dict(exchange_min_contracts=2),
                  dict(position_mode="single")):
            with self.subTest(x=x):self.rejected(**x)
    def test_caps_strict(self):
        for x in (dict(leverage="5.01"),dict(max_entry_slippage_ratio="0.0011"),
                  dict(exchange_required_margin_usdt="1.71"),
                  dict(mark_price="84000"),dict(entry="84500",stop_loss="84200",take_profit="85000"),
                  dict(stop_loss="79500"),dict(available_usdt="1.0")):
            with self.subTest(x=x):self.rejected(**x)
    def test_incomplete_exchange_readbacks_rejected(self):
        for x in (dict(account_readback_verified=False),
                  dict(metadata_readback_verified=False),
                  dict(protective_trigger_capability_verified=False),
                  dict(account_snapshot_utc=NOW-timedelta(seconds=31)),
                  dict(existing_positions=1),dict(existing_orders=1),
                  dict(existing_triggers=1),dict(contract_delisting=None),
                  dict(unknown_exchange_ack=True)):
            with self.subTest(x=x):self.rejected(**x)
    def test_no_eligible_luxalgo_btc_long(self):
        for x in (dict(signal_level_status="INVALID"),dict(order_type=None),
                  dict(signal_side="SHORT"),dict(signal_contract="BNB_USDT"),
                  dict(tv_bar_id=None),dict(signal_received_utc=NOW-timedelta(seconds=121)),
                  dict(timeframe="1m"),dict(stop_loss="82000"),
                  dict(take_profit="80500")):
            with self.subTest(x=x):self.rejected(**x)
    def test_malformed_and_negative_values_fail_closed(self):
        for x in (dict(mark_price="NaN"),dict(quanto_multiplier=None),
                  dict(equity_usdt=-10),dict(contracts=True),
                  dict(taker_fee_rate="not-a-fee"),dict(exchange_required_margin_usdt=None),
                  dict(account_snapshot_utc=None)):
            with self.subTest(x=x):self.rejected(**x)

if __name__=="__main__":unittest.main()
