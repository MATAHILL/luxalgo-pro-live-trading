"""Regression tests for the live-trading release gate and API surface.

All exchange calls are mocked; these tests never submit a real order.
They lock in the fix: the source locks are explicit, default-locked
environment releases, the HALT can be cleared only after a clean
reconciliation, certification can be recorded, and the live-trading API
routes exist.
"""
import os
import unittest
from unittest.mock import patch

import app as backend
import live_futures_engine as engine
import live_execution_orchestrator as lifecycle
import production_bot_control as prod


class ReleaseSwitchTests(unittest.TestCase):
    def test_release_switches_default_to_locked(self):
        # Absent configuration must fail closed.
        with patch.dict(os.environ, {}, clear=False):
            for name in ("LUXALGO_EXCHANGE_WRITE_RELEASE",
                         "LUXALGO_AUTOMATIC_WORKER_RELEASE",
                         "LUXALGO_ACCEPTANCE_RELEASE",
                         "LUXALGO_RECOVERY_RELEASE"):
                os.environ.pop(name, None)
                self.assertFalse(engine._env_release(name))

    def test_release_switch_requires_exact_true(self):
        for value, expected in (("true", True), ("TRUE", True),
                                ("True", True), ("false", False),
                                ("1", False), ("yes", False), ("", False)):
            with self.subTest(value=value):
                with patch.dict(os.environ,
                                {"LUXALGO_EXCHANGE_WRITE_RELEASE": value}):
                    self.assertEqual(
                        engine._env_release("LUXALGO_EXCHANGE_WRITE_RELEASE"),
                        expected)

    def test_module_constants_are_booleans(self):
        self.assertIsInstance(engine.EXCHANGE_WRITE_RELEASE, bool)
        self.assertIsInstance(lifecycle.AUTOMATIC_WORKER_RELEASE, bool)


class LiveApiSurfaceTests(unittest.TestCase):
    def test_live_routes_are_registered(self):
        rules = {r.rule for r in backend.app.url_map.iter_rules()}
        for path in ("/api/live/status", "/api/readiness", "/api/trades",
                     "/api/live/arm", "/api/live/disarm",
                     "/api/live/halt-clear", "/api/live/certify"):
            self.assertIn(path, rules)

    def test_live_api_requires_authentication(self):
        with patch.dict(os.environ, {"TERMINAL_PASSWORD": "x" * 28,
                                     "TERMINAL_USER": "admin"}):
            client = backend.app.test_client()
            self.assertEqual(client.get("/api/live/status").status_code, 401)
            self.assertEqual(client.get("/api/trades").status_code, 401)

    def test_arm_requires_csrf(self):
        with patch.dict(os.environ, {"TERMINAL_PASSWORD": "x" * 28,
                                     "TERMINAL_USER": "admin"}):
            client = backend.app.test_client()
            with client.session_transaction() as sess:
                sess["authenticated"] = True
                sess["user"] = "admin"
                sess["csrf_token"] = "csrf"
            self.assertEqual(client.post("/api/live/arm", json={}).status_code, 403)


class HealthReflectsStateTests(unittest.TestCase):
    def test_health_reports_locked_when_state_unavailable(self):
        with patch.object(backend, "_db_connect", side_effect=RuntimeError("no db")):
            body = backend.app.test_client().get("/health").get_json()
        self.assertFalse(body["live_trading"])
        self.assertFalse(body["orders_enabled"])
        self.assertEqual(body["kind"],
                         "signal_receiver_with_readonly_exchange_checks")

    def test_health_reports_live_when_armed(self):
        with patch.object(backend, "_db_connect"), \
             patch("production_bot_control.state",
                   return_value={"live_trading": True, "orders_enabled": True,
                                 "safety_gates": []}):
            body = backend.app.test_client().get("/health").get_json()
        self.assertTrue(body["live_trading"])
        self.assertTrue(body["orders_enabled"])
        self.assertEqual(body["kind"], "live_trading_enabled")


class CertificationGateTests(unittest.TestCase):
    def test_certify_rejects_bad_evidence(self):
        with patch.object(prod, "install"):
            with self.assertRaises(RuntimeError):
                prod.certify(object(), evidence_sha="x",
                             signed_approval_id="ok-id")
            with self.assertRaises(RuntimeError):
                prod.certify(object(), evidence_sha="good-evidence",
                             signed_approval_id="ok-id", valid_hours=0)

    def test_clear_halt_refuses_when_reconciliation_reports_exposure(self):
        from live_execution_coordinator import clear_halt
        with patch.object(prod, "install"), \
             patch("live_execution_coordinator.install"), \
             patch("live_execution_coordinator.inspect",
                   return_value={"reconciliation": {"requires_halt": True},
                                 "untracked_open_orders": 0,
                                 "untracked_open_triggers": 0}):
            with self.assertRaises(engine.LiveSafetyError):
                clear_halt(object(), client=object())


if __name__ == "__main__":
    unittest.main()
