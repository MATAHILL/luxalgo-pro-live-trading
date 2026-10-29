"""Offline post-ACK fault injections. All Gate.io calls are mocked; zero real orders."""
import unittest
from unittest.mock import MagicMock, Mock, patch

import live_execution_orchestrator as orchestrator


PLAN = {
    "event_id": "tv-BTC-5m-postack-db-fault",
    "client_text": "t-lxp-postack-db-fault",
    "contract": "BTC_USDT",
    "side": "LONG",
    "contracts": 1,
    "entry": "80000",
    "sl": "79600",
    "tp": "80800",
    "position_mode": "single",
}


class PostAckDatabaseFailureTests(unittest.TestCase):
    def _run_fault(self, halt_fails):
        con = MagicMock()
        con.__enter__.return_value = con
        client = Mock()
        snapshot = {"account": {}, "positions": [], "orders": [], "triggers": []}
        client.snapshot.return_value = snapshot
        client.submit_entry.return_value = {"id": 123}

        def persist(_open_connection, _event_id, state, **_kwargs):
            if state == "ENTRY_CONFIRMED":
                raise OSError("mocked database journal failure AFTER exchange entry")
            return state

        with patch.object(orchestrator, "EXCHANGE_WRITE_RELEASE", True), \
             patch.object(orchestrator, "install"), \
             patch.object(orchestrator, "control", return_value={"halted": False}), \
             patch.object(orchestrator, "read_rows", return_value=[]), \
             patch.object(orchestrator, "risk_reserve"), \
             patch.object(orchestrator, "assert_existing_protected_exposure"), \
             patch.object(orchestrator, "verified_account_equity", return_value=(1000, 1000)), \
             patch("production_bot_control.require_operator_on"), \
             patch.object(orchestrator, "persist_state", side_effect=persist), \
             patch.object(orchestrator, "require_full_entry_readback",
                          return_value=("123", snapshot)), \
             patch.object(orchestrator, "emergency_persist_halt",
                          side_effect=OSError("mocked halt DB outage") if halt_fails else None) as emergency_halt, \
             patch.object(orchestrator, "emergency_close_once") as emergency_close:
            result = orchestrator.execute_confirmed_signal(lambda: con, PLAN, client)
            self.assertEqual(client.submit_entry.call_count, 1)
            client.submit_trigger.assert_not_called()
            emergency_close.assert_not_called()
            self.assertEqual(emergency_halt.call_count, 1)
            self.assertTrue(result["exchange_position_may_be_open"])
            self.assertTrue(result["requires_manual_exchange_review"])
            self.assertFalse(result["automatic_retry"])
            return result

    def test_post_ack_journal_failure_triggers_persistent_halt(self):
        result = self._run_fault(halt_fails=False)
        self.assertEqual(result["state"], "HALTED_UNKNOWN")
        self.assertTrue(result["halt_persisted"])
        self.assertTrue(result["entry_fill_readback_verified"])
        self.assertEqual(result["emergency_close"]["reason"],
                         "ONE_SHOT_CLOSE_NOT_CERTIFIED")

    def test_post_ack_journal_and_halt_failure_requires_manual_recovery(self):
        result = self._run_fault(halt_fails=True)
        self.assertEqual(result["state"], "HALT_PERSISTENCE_UNVERIFIED")
        self.assertFalse(result["halt_persisted"])
        self.assertEqual(result["emergency_close"]["reason"],
                         "DURABLE_HALT_UNAVAILABLE_NO_CLOSE")


if __name__ == "__main__":
    unittest.main()
