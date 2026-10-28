"""Mocked production execution lifecycle tests; never calls Gate.io POST."""
import unittest
from unittest.mock import patch, Mock
from live_futures_engine import LiveSafetyError, client_text
from live_execution_orchestrator import (
    execute_confirmed_signal, full_close_oneway_order,
    require_full_entry_readback, emergency_close_once, restart_reconcile,
    assert_existing_protected_exposure)
from live_execution_coordinator import status_fallback

PLAN={"event_id":"tv-BTCUSDT-5m-20261028","client_text":client_text("tv-BTCUSDT-5m-20261028"),
      "contract":"BTC_USDT","side":"LONG","contracts":2,"entry":"80000",
      "sl":"79000","tp":"82000","estimated_risk_usdt":"0.2","timeframe":"5m"}

class MockLiveFlow(unittest.TestCase):
    def test_workflow_blocks_before_database_or_network(self):
        with self.assertRaisesRegex(LiveSafetyError,"SOURCE_LOCKED"):
            execute_confirmed_signal(lambda:None,PLAN)

    def test_emergency_close_only_reduces(self):
        data=full_close_oneway_order(PLAN)
        self.assertTrue(data["close"])
        self.assertTrue(data["reduce_only"])
        self.assertEqual(data["size"],0)
        self.assertEqual(data["tif"],"ioc")

    def test_filled_entry_requires_order_and_position_readback(self):
        c=Mock()
        c.request.return_value={"id":123,"text":PLAN["client_text"],
                                "contract":"BTC_USDT","size":2,"left":0,
                                "status":"finished","fill_price":"80001"}
        c.snapshot.return_value={"positions":[{"contract":"BTC_USDT","size":"2"}]}
        result=require_full_entry_readback(c,PLAN,{"id":123})
        self.assertEqual(result[0],"123")
        self.assertEqual(c.request.call_count,1)

    def test_partial_fill_never_verifies(self):
        c=Mock()
        c.request.return_value={"id":123,"text":PLAN["client_text"],
                                "contract":"BTC_USDT","size":2,"left":1,
                                "status":"finished","fill_price":"80001"}
        from live_futures_engine import AckUnknown
        with self.assertRaises(AckUnknown):
            require_full_entry_readback(c,PLAN,{"id":123})

    def test_emergency_close_requires_flat_plus_no_orphan_triggers(self):
        c=Mock()
        c.request.return_value={"id":789}
        c.snapshot.return_value={"positions":[],
          "triggers":[{"initial":{"contract":"BTC_USDT"}}]}
        result=emergency_close_once(c,PLAN)
        self.assertFalse(result["closed_verified"])
        c.snapshot.return_value={"positions":[],"triggers":[]}
        good=emergency_close_once(c,PLAN)
        self.assertTrue(good["closed_verified"])

    def test_live_status_never_calls_unknown_positions_zero(self):
        status=status_fallback("EXCHANGE_OR_DB_UNAVAILABLE")
        self.assertIsNone(status["open_positions"])
        self.assertTrue(status["halted"])
        self.assertFalse(status["orders_enabled"])

    def test_multiple_existing_protected_positions_are_allowed(self):
        from live_futures_engine import build_protective_trigger
        rows=[];positions=[];triggers=[]
        for symbol,entry,stop,take in [
                ("BTC_USDT","80000","79000","82000"),
                ("ETH_USDT","4000","3900","4200"),
                ("SOL_USDT","200","190","220"),
                ("XRP_USDT","2","1.9","2.2")]:
            k=len(rows)*2+501
            plan={"contract":symbol,"side":"LONG","entry":entry,
                  "sl":stop,"tp":take}
            rows.append({"contract":symbol,"side":"LONG","state":"PROTECTED",
                         "entry_price":entry,"stop_loss":stop,"take_profit":take,
                         "stop_order_id":str(k),"take_order_id":str(k+1)})
            positions.append({"contract":symbol,"size":"1"})
            triggers.append(dict(build_protective_trigger(plan,"SL"),
                                 id=k,status="open"))
            triggers.append(dict(build_protective_trigger(plan,"TP"),
                                 id=k+1,status="open"))
        snap={"positions":positions,"orders":[],"triggers":triggers}
        self.assertEqual(assert_existing_protected_exposure(rows,snap)["protected_open_positions"],4)
        snap["triggers"]=triggers[:-1]
        with self.assertRaises(LiveSafetyError):
            assert_existing_protected_exposure(rows,snap)

    def test_restart_never_resends_entry(self):
        fake={"reconciliation":{"requires_halt":True},
              "untracked_open_orders":0}
        with patch("live_execution_coordinator.reconcile_and_halt_if_needed",
                   return_value=fake):
            c=Mock()
            c.__enter__=Mock(return_value=c)
            c.__exit__=Mock(return_value=False)
            r=restart_reconcile(lambda:c,Mock())
            self.assertEqual(r["orders_resubmitted"],0)
            self.assertEqual(r["status"],"HALTED_REVIEW_REQUIRED")

if __name__=="__main__":
    unittest.main()
