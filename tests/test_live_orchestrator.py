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
      "sl":"79000","tp":"82000","estimated_risk_usdt":"0.2","timeframe":"5m",
      "position_mode":"single"}

def mock_durable_claim(plan):
    return dict(claimed=True,state="CLAIMED",event_id=plan["event_id"],
        contract=plan["contract"],
        close_client_text="t-em-"+plan["client_text"][2:25],
        approval_reference="SIMULATED_CASE_NOT_LIVE_APPROVAL")


def simulate_claimed_close(client,plan):
    # Mock only: never connects to a production DB or exchange.
    with (patch("live_execution_orchestrator.require_safe_emergency_close_inventory",
                return_value={"verified": True}),
          patch("live_emergency_one_shot_claim.consume_one_shot_recovery_claim",
                return_value=True),
          patch("live_emergency_one_shot_claim.finalize_one_shot_recovery_claim",
                return_value=True)):
        return emergency_close_once(client,plan,
            durable_claim=mock_durable_claim(plan),open_connection=lambda:None)


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
        close_readback={"id":789,"contract":"BTC_USDT",
            "text":"t-em-"+PLAN["client_text"][2:25],"status":"finished",
            "is_close":True,"is_reduce_only":True}
        c.request.side_effect=[{"id":789},close_readback,
                               {"id":789},close_readback]
        c.snapshot.return_value={"positions":[],"orders":[],
          "source":"GATE_IO_LIVE_FUTURES",
          "triggers":[{"initial":{"contract":"BTC_USDT"}}]}
        result=simulate_claimed_close(c,PLAN)
        self.assertFalse(result["closed_verified"])
        c.snapshot.return_value={"positions":[],"orders":[],
          "source":"GATE_IO_LIVE_FUTURES","triggers":[]}
        good=simulate_claimed_close(c,PLAN)
        self.assertTrue(good["closed_verified"])

    def test_emergency_close_unverified_readback_never_claims_success(self):
        c=Mock()
        c.request.side_effect=[{"id":789},{"id":789,"contract":"BTC_USDT",
            "text":"WRONG_SOURCE","status":"finished","is_close":True,
            "is_reduce_only":True}]
        r=simulate_claimed_close(c,PLAN)
        self.assertFalse(r["closed_verified"])
        self.assertTrue(r["requires_manual_exchange_review"])
        c.snapshot.assert_not_called()

    def test_valid_claim_but_not_atomically_consumed_never_posts(self):
        c=Mock()
        with patch("live_emergency_one_shot_claim.consume_one_shot_recovery_claim",
                   side_effect=LiveSafetyError("DUPLICATE_CLAIM")):
            r=emergency_close_once(c,PLAN,
                durable_claim=mock_durable_claim(PLAN),open_connection=lambda:None)
        self.assertFalse(r["closed_verified"])
        self.assertEqual(r["orders_sent"],0)
        c.request.assert_not_called()

    def test_verified_exchange_flat_but_db_finalize_fails_never_certifies(self):
        c=Mock()
        c.request.side_effect=[{"id":789},{"id":789,"contract":"BTC_USDT",
            "text":"t-em-"+PLAN["client_text"][2:25],
            "status":"finished","is_close":True,"is_reduce_only":True}]
        c.snapshot.return_value={"positions":[],"orders":[],
            "source":"GATE_IO_LIVE_FUTURES","triggers":[]}
        with (patch("live_execution_orchestrator.require_safe_emergency_close_inventory",
                    return_value={"verified": True}),
              patch("live_emergency_one_shot_claim.consume_one_shot_recovery_claim",
                    return_value=True),
              patch("live_emergency_one_shot_claim.finalize_one_shot_recovery_claim",
                    side_effect=LiveSafetyError("DB_NOT_PERSISTED"))):
            r=emergency_close_once(c,PLAN,
                durable_claim=mock_durable_claim(PLAN),open_connection=lambda:None)
        self.assertFalse(r["closed_verified"])
        self.assertTrue(r["requires_manual_exchange_review"])
        self.assertEqual(c.request.call_count,2)

    def test_emergency_close_denies_unclaimed_exchange_post(self):
        c=Mock()
        for claim in (None,{},dict(mock_durable_claim(PLAN),event_id="OTHER")):
            with self.subTest(claim=claim):
                result=emergency_close_once(c,PLAN,durable_claim=claim)
                self.assertFalse(result["closed_verified"])
                self.assertEqual(result["orders_sent"],0)
        c.request.assert_not_called()

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
                  "sl":stop,"tp":take,"position_mode":"single"}
            rows.append({"contract":symbol,"side":"LONG","state":"PROTECTED",
                         "entry_price":entry,"stop_loss":stop,"take_profit":take,
                         "stop_order_id":str(k),"take_order_id":str(k+1),
                         "position_mode":"single","contracts":1})
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

