"""All Gate.io calls mocked. These tests MUST make zero real-money writes."""
import io
import os
import time
import unittest
from decimal import Decimal
from unittest.mock import patch
from live_futures_engine import (
    GateLiveClient, LiveSafetyError, AckUnknown, EXCHANGE_WRITE_RELEASE,
    build_plan, build_entry_order, build_protective_trigger,
    evaluate_protection, reconcile_ledger, client_text, MIGRATION_SQL,
)
NOW=1800000000
BASE={"event_id":"tradingview-BTC-5m-1234","contract":"BTC_USDT",
      "side":"LONG","timeframe":"5m","levels_status":"VALID",
      "entry":"80000","sl":"79200","tp":"81600","leverage":"5",
      "position_mode":"single",
      "received_epoch":NOW-12}
CONTRACT={"name":"BTC_USDT","quanto_multiplier":"0.0001",
          "order_size_min":1,"order_size_max":100000}

def planned(signal=None,positions=None,metadata=None):
    return build_plan(signal or BASE,metadata or CONTRACT,"1000","1000",
                      positions or [],now=NOW)

class LiveFuturesSafetyTests(unittest.TestCase):
    def test_entry_payload_and_risk_budgets(self):
        plan=planned()
        self.assertEqual(plan["contracts"],62) # floor(5 USDT / 0.08 per contract)
        self.assertEqual(Decimal(plan["estimated_risk_usdt"]),Decimal("4.9600"))
        order=build_entry_order(plan)
        self.assertEqual(order["size"],62)
        self.assertEqual(order["price"],"0")
        self.assertEqual(order["tif"],"ioc")
        self.assertEqual(order["text"],client_text(BASE["event_id"]))
        self.assertFalse(plan["verified_live_order"])
        self.assertFalse(EXCHANGE_WRITE_RELEASE)

    def test_short_and_directional_triggers(self):
        x=dict(BASE,side="SHORT",sl="80800",tp="78400")
        plan=planned(x)
        self.assertEqual(build_entry_order(plan)["size"],-62)
        self.assertEqual(build_protective_trigger(plan,"SL")["trigger"]["rule"],1)
        self.assertEqual(build_protective_trigger(plan,"TP")["trigger"]["rule"],2)
        self.assertTrue(build_protective_trigger(plan,"SL")["initial"]["reduce_only"])

    def test_invalid_stop_direction_rejected(self):
        with self.assertRaises(LiveSafetyError):
            planned(dict(BASE,sl="81000"))

    def test_stale_or_missing_level_rejected(self):
        for data in (dict(BASE,received_epoch=NOW-61),
                     dict(BASE,levels_status="MISSING"),
                     dict(BASE,event_id="invalid id")):
            with self.subTest(data=data),self.assertRaises(LiveSafetyError):
                planned(data)

    def test_deny_fifth_plus_position(self):
        p=[{"contract":f"TEST{i}_USDT","size":"2"} for i in range(5)]
        with self.assertRaises(LiveSafetyError): planned(positions=p)

    def test_duplicate_contract_rejected(self):
        with self.assertRaises(LiveSafetyError):
            planned(positions=[{"contract":"BTC_USDT","size":"1"}])

    def test_missing_multiplier_and_margin_fail_closed(self):
        with self.assertRaises(LiveSafetyError):
            planned(metadata={"name":"BTC_USDT","order_size_min":1,"order_size_max":100})
        with self.assertRaises(LiveSafetyError):
            build_plan(BASE,CONTRACT,"1","0.001",[],now=NOW)

    def test_aggregate_risk_cap(self):
        with self.assertRaises(LiveSafetyError):
            build_plan(BASE,CONTRACT,"1000","1000",[],
                       reserved_risk=Decimal("25"),now=NOW)

    def test_native_triggers_require_both_verified(self):
        plan=planned()
        def trigger(kind,identity):
            payload=build_protective_trigger(plan,kind)
            return dict(payload,id=identity,status="open")
        book=[trigger("SL",101),trigger("TP",102)]
        result=evaluate_protection(plan,book)
        self.assertTrue(result["verified"])
        self.assertEqual(result["stop_order_id"],"101")
        self.assertFalse(evaluate_protection(plan,book[:1])["verified"])
        bad=dict(book[0],status="finished")
        self.assertFalse(evaluate_protection(plan,[bad,book[1]])["verified"])

    def test_source_lock_denies_all_post_even_with_flags(self):
        client=GateLiveClient("fakekey","fakesecret",transport=lambda *a,**k: (_ for _ in ()).throw(AssertionError("called")))
        with patch.dict(os.environ,{"LIVE_TRADING_ENABLED":"true",
                                    "ORDERS_ENABLED":"true",
                                    "LIVE_ORDERS_SEPARATELY_AUTHORIZED":"true"}):
            with self.assertRaisesRegex(LiveSafetyError,"SOURCE_LOCKED"):
                client.submit_entry(planned())
            with self.assertRaisesRegex(LiveSafetyError,"SOURCE_LOCKED"):
                client.submit_trigger(planned(),"SL")

    def test_only_live_allowlisted_get(self):
        client=GateLiveClient("x","y")
        for path in ("/api/v4/futures/usdt/orders/not-an-id",
                     "/api/v4/spot/accounts",
                     "/api/v4/futures/usdt/positions/ANY/close"):
            with self.subTest(path=path),self.assertRaises(LiveSafetyError):
                client.request("GET",path)

    def test_reconciliation_does_not_invent_protection(self):
        plan=planned()
        row={"event_id":BASE["event_id"],"contract":"BTC_USDT","side":"LONG",
             "entry_price":plan["entry"],"stop_loss":plan["sl"],"take_profit":plan["tp"],"position_mode":"single"}
        bare=reconcile_ledger([row],[{"contract":"BTC_USDT","size":"2"}],[])
        self.assertTrue(bare["requires_halt"])
        self.assertEqual(bare["results"][0]["state"],"HALT_UNPROTECTED")
        unknown=reconcile_ledger([], [{"contract":"SOL_USDT","size":"1"}],[])
        self.assertEqual(unknown["results"][0]["state"],"HALT_UNKNOWN_EXPOSURE")

    def test_schema_has_atomic_unique_id_and_initial_halt(self):
        self.assertIn("client_text TEXT NOT NULL UNIQUE",MIGRATION_SQL)
        self.assertIn("PRIMARY KEY",MIGRATION_SQL)
        self.assertIn("TRUE,'NOT_VERIFIED_FOR_REAL_MONEY'",MIGRATION_SQL)

if __name__=="__main__":
    unittest.main()
