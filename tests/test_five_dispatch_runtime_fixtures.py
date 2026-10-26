"""Offline tests for the actual five-position submission function.

The app module is never imported, and every exchange/database operation is mocked.
Tests cannot place live orders or change the production service.
"""
import ast
import os
import pathlib
import re
import unittest
from unittest.mock import patch

APP = pathlib.Path(__file__).resolve().parents[1] / "app.py"
tree = ast.parse(APP.read_text(encoding="utf-8"))
fn = next(node for node in tree.body
          if isinstance(node, ast.FunctionDef) and node.name == "_five_submit_order_once")

class GateOrderTransportError(Exception):
    pass

class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.claimed = True
        self.transport_error = None
        self.journal_error = None
        preview = {"order_preview": {"contract": "BTC_USDT", "size": 2,
                   "price": "0", "tif": "ioc", "text": "t-lux-test",
                   "tpsl_sl_trigger_price": "900", "tpsl_tp_trigger_price": "1100"}}
        def preflight(event):
            self.calls.append("preflight")
            return preview
        def plan(event, value):
            self.calls.append("plan")
            return {"state": "READY_FOR_OFFLINE_REVIEW"}
        def claim(event):
            self.calls.append("claim")
            return self.claimed
        def post(payload):
            self.calls.append("post")
            if self.transport_error:
                raise self.transport_error
            return {"exchange_order_id": "1234"}
        def journal(event, oid):
            self.calls.append("ack")
            if self.journal_error:
                raise self.journal_error
        env = {"FIVE_POSITION_EXECUTION_CERTIFIED": True,
               "GateOrderTransportError": GateOrderTransportError,
               "_five_position_entry_preflight": preflight,
               "_five_dispatch_plan": plan,
               "_five_journal_mark_dispatch_uncertain": claim,
               "_gate_post_transport_implementation": post,
               "_five_journal_verify_exchange_order": journal,
               "re": re, "os": os}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), str(APP), "exec"), env)
        self.submit = env["_five_submit_order_once"]
        self.env = env

    def invoke(self):
        with patch.dict(os.environ, {"LIVE_TRADING_ENABLED": "true",
                                     "ORDERS_ENABLED": "true"}):
            return self.submit("event-01")

    def test_certification_kill_switch_prevents_all_actions(self):
        self.env["FIVE_POSITION_EXECUTION_CERTIFIED"] = False
        with self.assertRaisesRegex(GateOrderTransportError, "NOT_CERTIFIED"):
            self.invoke()
        self.assertEqual(self.calls, [])

    def test_order_flags_are_required(self):
        with patch.dict(os.environ, {"LIVE_TRADING_ENABLED": "false",
                                     "ORDERS_ENABLED": "false"}):
            with self.assertRaisesRegex(GateOrderTransportError, "disabled"):
                self.submit("event-01")
        self.assertEqual(self.calls, [])

    def test_dispatch_claim_must_precede_single_post(self):
        result = self.invoke()
        self.assertEqual(self.calls, ["preflight", "plan", "claim", "post", "ack"])
        self.assertEqual(result["state"], "ACKNOWLEDGED_UNPROTECTED")
        self.assertFalse(result["protection_verified"])
        self.assertTrue(result["recovery_required"])

    def test_rejected_claim_cannot_post(self):
        self.claimed = False
        with self.assertRaisesRegex(GateOrderTransportError, "CLAIM_DENIED"):
            self.invoke()
        self.assertEqual(self.calls, ["preflight", "plan", "claim"])

    def test_network_failure_never_retries(self):
        self.transport_error = TimeoutError("simulated exchange timeout")
        with self.assertRaisesRegex(GateOrderTransportError, "ACK_UNKNOWN_DO_NOT_RETRY"):
            self.invoke()
        self.assertEqual(self.calls.count("post"), 1)
        self.assertNotIn("ack", self.calls)

    def test_journal_ack_failure_does_not_resubmit(self):
        self.journal_error = RuntimeError("database unavailable")
        with self.assertRaisesRegex(GateOrderTransportError, "JOURNAL_UNCERTAIN_DO_NOT_RETRY"):
            self.invoke()
        self.assertEqual(self.calls.count("post"), 1)
        self.assertEqual(self.calls.count("ack"), 1)

if __name__ == "__main__":
    unittest.main()
