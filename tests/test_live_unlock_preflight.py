"""Non-trading regression checks for the initial live-unlock checklist."""
import unittest
from unittest.mock import patch

import app as backend
from live_futures_engine import LiveSafetyError
from live_unlock_preflight import assess_unlock_readiness


def flat_snapshot():
    return {
        "source": "GATE_IO_LIVE_FUTURES",
        "account": {"position_mode": "dual"},
        "positions": [], "orders": [], "triggers": [],
        "fetched_at": 1793370000,
    }


def blocked_state():
    return {
        "safety_gates": ["SOURCE_LOCKED", "ENVIRONMENT_LOCKED", "EMERGENCY_HALT",
                         "ACCEPTANCE_AND_FINAL_AUTHORIZATION_MISSING", "OPERATOR_OFF"],
        "halt_active": True,
        "certified": False,
        "operator_requested_on": False,
    }


class UnlockPreflightTests(unittest.TestCase):
    def test_fresh_flat_account_is_not_an_accepted_live_trade(self):
        data = assess_unlock_readiness(
            blocked_state(), flat_snapshot(), sender_released=False,
            worker_released=False, unresolved_ledger_rows=0)
        self.assertEqual(data["status"], "BLOCKED_UNTIL_INDEPENDENT_ACCEPTANCE")
        self.assertTrue(data["checks"]["authenticated_exchange_readback"])
        self.assertTrue(data["checks"]["flat_exchange_for_limited_acceptance"])
        self.assertTrue(data["ready_for_independent_acceptance_review"])
        self.assertFalse(data["checks"]["independent_live_fill_native_protection_and_recovery"])
        self.assertTrue(data["source_sender_locked"])
        self.assertTrue(data["automatic_worker_locked"])
        self.assertFalse(data["automatic_real_money_trading_certified"])
        self.assertFalse(data["orders_enabled_by_this_check"])
        self.assertEqual(data["orders_submitted"], 0)

    def test_unknown_inventory_and_unknown_ledger_fail_closed(self):
        for snapshot in (None, {}, {"source": "GATE_IO_LIVE_FUTURES",
                                   "account": {"position_mode": "dual"},
                                   "positions": [], "orders": []}):
            with self.subTest(snapshot=snapshot):
                data = assess_unlock_readiness(
                    blocked_state(), snapshot, sender_released=False,
                    worker_released=False, unresolved_ledger_rows=None)
                self.assertFalse(data["ready_for_independent_acceptance_review"])
                self.assertFalse(data["checks"]["authenticated_exchange_readback"])
                self.assertFalse(data["checks"]["no_unresolved_execution_rows"])

    def test_any_manual_exposure_disqualifies_flat_acceptance(self):
        for field in ("positions", "orders", "triggers"):
            with self.subTest(field=field):
                snap = flat_snapshot()
                snap[field] = [{"contract": "BTC_USDT"}]
                data = assess_unlock_readiness(
                    blocked_state(), snap, sender_released=False,
                    worker_released=False, unresolved_ledger_rows=0)
                self.assertFalse(data["checks"]["flat_exchange_for_limited_acceptance"])
                self.assertFalse(data["ready_for_independent_acceptance_review"])

    def test_forged_status_does_not_issue_certificate_or_trade_permission(self):
        state = dict(safety_gates=[], halt_active=False,
                     certified=True, operator_requested_on=True)
        data = assess_unlock_readiness(
            state, flat_snapshot(), sender_released=True,
            worker_released=True, unresolved_ledger_rows=0)
        self.assertFalse(data["automatic_real_money_trading_certified"])
        self.assertFalse(data["checks"]["independent_live_fill_native_protection_and_recovery"])
        self.assertFalse(data["orders_enabled_by_this_check"])

    def test_live_endpoint_uses_get_snapshot_and_never_calls_exchange_post(self):
        with patch.object(backend, "_terminal_permitted", return_value=True), \
             patch.object(backend, "_db_connect") as db, \
             patch("production_bot_control.state", return_value=blocked_state()), \
             patch("live_futures_engine.GateLiveClient") as client_type:
            db.return_value.__enter__.return_value.cursor.return_value.\
                __enter__.return_value.fetchone.return_value = {"unresolved": 0}
            client = client_type.return_value
            client.snapshot.return_value = flat_snapshot()
            response = backend.app.test_client().get("/terminal/live-execution/unlock-readiness")
            self.assertEqual(response.status_code, 200)
            report = response.get_json()
            self.assertEqual(report["phase"], "PRE_RELEASE_READ_ONLY_AUDIT")
            self.assertTrue(report["exchange_snapshot_available"])
            self.assertFalse(report["automatic_real_money_trading_certified"])
            client.snapshot.assert_called_once_with()
            client.request.assert_not_called()
            client.submit_entry.assert_not_called()
            client.submit_trigger.assert_not_called()

    def test_exchange_readback_failure_is_explicit_block_not_flat(self):
        with patch.object(backend, "_terminal_permitted", return_value=True), \
             patch.object(backend, "_db_connect") as db, \
             patch("production_bot_control.state", return_value=blocked_state()), \
             patch("live_futures_engine.GateLiveClient") as client_type:
            db.return_value.__enter__.return_value.cursor.return_value.\
                __enter__.return_value.fetchone.return_value = {"unresolved": 0}
            client_type.return_value.snapshot.side_effect = LiveSafetyError("GET unavailable")
            response = backend.app.test_client().get("/terminal/live-execution/unlock-readiness")
            self.assertEqual(response.status_code, 503)
            report = response.get_json()
            self.assertFalse(report["exchange_snapshot_available"])
            self.assertFalse(report["ready_for_independent_acceptance_review"])
            self.assertFalse(report["orders_enabled_by_this_check"])


if __name__ == "__main__":
    unittest.main()
