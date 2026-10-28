"""Fault injection: no real exchange orders, no API keys, no database writes."""
import io
import json
import time
import unittest
from unittest.mock import Mock
from live_futures_engine import (
    GateLiveClient,LiveSafetyError,AckUnknown,build_plan,
    build_protective_trigger,evaluate_protection,reconcile_ledger,
)
from live_execution_orchestrator import (
    emergency_close_once,require_full_entry_readback,assert_existing_protected_exposure,
)

PLAN={"event_id":"tv-long-BTCUSDT-5m-1200","client_text":"t-lxp-example123456",
      "contract":"BTC_USDT","side":"LONG","timeframe":"5m",
      "contracts":2,"entry":"80000","sl":"79000","tp":"82000",
      "position_mode":"single"}

class FakeHttp:
    def __init__(self,payload): self.payload=payload
    def __enter__(self): return self
    def __exit__(self,*args): return False
    def read(self,n): return json.dumps(self.payload).encode()

class NonTradingFaultInjections(unittest.TestCase):
    def test_read_only_transport_timeout_fails(self):
        def fail(*a,**kw):raise TimeoutError("induced GET timeout")
        c=GateLiveClient("fakekey","fakesecret",transport=fail)
        with self.assertRaisesRegex(LiveSafetyError,"Authenticated live GET failed"):
            c.snapshot()

    def test_invalid_account_snapshot_fails(self):
        def response(req,timeout=8):
            path=req.full_url.split("?")[0]
            if path.endswith("/accounts"):return FakeHttp({"currency":"WRONG"})
            return FakeHttp([])
        c=GateLiveClient("fakekey","fakesecret",transport=response)
        with self.assertRaises(LiveSafetyError):c.snapshot()

    def test_exchange_incomplete_trigger_pagination_fails(self):
        def response(req,timeout=8):
            path=req.full_url.split("?")[0]
            if path.endswith("/accounts"):return FakeHttp({"currency":"USDT"})
            if path.endswith("/price_orders"):return FakeHttp([{"id":i} for i in range(100)])
            return FakeHttp([])
        c=GateLiveClient("fakekey","fakesecret",transport=response)
        with self.assertRaisesRegex(LiveSafetyError,"pagination required"):
            c.snapshot()

    def test_real_gate_response_aliases_for_stop_target(self):
        book=[]
        for kind,identity in (("SL","100100"),("TP","100200")):
            p=build_protective_trigger(PLAN,kind)
            p["id_string"]=identity;p["id"]=int(identity);p["status"]="open"
            p["initial"].pop("close",None)
            p["initial"].pop("reduce_only",None)
            p["initial"]["is_close"]=True
            book.append(p)
        outcome=evaluate_protection(PLAN,book)
        self.assertTrue(outcome["verified"])
        self.assertEqual(outcome["take_order_id"],"100200")
        book.append(dict(book[1],id_string="100300",id=100300))
        self.assertFalse(evaluate_protection(PLAN,book)["verified"])

    def test_partial_fill_is_not_full_entry(self):
        c=Mock()
        c.request.return_value={"id":101,"contract":"BTC_USDT","text":PLAN["client_text"],
                                "size":2,"left":1,"fill_price":"80000","status":"finished"}
        with self.assertRaises(AckUnknown):
            require_full_entry_readback(c,PLAN,{"id":101})
        c.snapshot.assert_not_called()

    def test_unknown_emergency_close_requires_manual_review(self):
        c=Mock()
        c.request.side_effect=AckUnknown("Simulated timeout")
        status=emergency_close_once(c,PLAN)
        self.assertFalse(status["closed_verified"])
        self.assertTrue(status["requires_manual_exchange_review"])
        self.assertEqual(c.request.call_count,1)

    def test_untracked_position_cannot_safely_open_more(self):
        with self.assertRaises(LiveSafetyError):
            assert_existing_protected_exposure([],
                {"positions":[{"contract":"BTC_USDT","size":"2"}],
                 "triggers":[],"orders":[]})

    def test_unknown_native_trigger_blocks_next_order(self):
        with self.assertRaises(LiveSafetyError):
            assert_existing_protected_exposure([],
                {"positions":[],"triggers":[{"id":100}],"orders":[]})

    def test_reconciler_reports_closed_ledger_position_reappearing(self):
        row={"event_id":"X","contract":"BTC_USDT","side":"LONG","state":"CLOSED_VERIFIED",
             "entry_price":"80000","stop_loss":"79000","take_profit":"82000",
             "position_mode":"single"}
        outcome=reconcile_ledger([row],[{"contract":"BTC_USDT","size":"2"}],[])
        self.assertTrue(outcome["requires_halt"])

    def test_stale_market_signal_rejected_for_dispatch(self):
        s=dict(event_id="tv-BTC-5m-10000001",side="LONG",contract="BTC_USDT",
               timeframe="5m",levels_status="VALID",
               received_epoch=100000,entry="80000",sl="79000",tp="82000",
               position_mode="single")
        meta=dict(name="BTC_USDT",quanto_multiplier="0.0001",
                  order_size_min=1,order_size_max=100000,taker_fee_rate="0.00075")
        with self.assertRaisesRegex(LiveSafetyError,"delayed or future"):
            build_plan(s,meta,1000,1000,[],now=100100)

if __name__=="__main__":
    unittest.main()
