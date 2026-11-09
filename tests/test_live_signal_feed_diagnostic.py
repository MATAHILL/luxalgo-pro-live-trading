"""Read-only webhook health is diagnostic, never permission to place an order."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import app as backend
from live_unlock_preflight import diagnose_signal_feed


NOW = datetime(2026, 11, 9, 17, 30, tzinfo=timezone.utc)


class SignalFeedDiagnosticTests(unittest.TestCase):
    def test_recent_qualified_signal_is_not_live_certification(self):
        result = diagnose_signal_feed(
            latest_received_at=NOW - timedelta(seconds=20),
            last_qualified_at=NOW - timedelta(seconds=20),
            eligible_count=79, now=NOW)
        self.assertEqual(result["status"], "RECENT_QUALIFIED_SIGNAL")
        self.assertFalse(result["orders_enabled_by_diagnostic"])
        self.assertTrue(result["diagnostic_only"])

    def test_stale_qualified_signal_is_an_investigation_not_a_trade(self):
        result = diagnose_signal_feed(
            latest_received_at=NOW - timedelta(days=6),
            last_qualified_at=NOW - timedelta(days=6),
            eligible_count=79, now=NOW)
        self.assertEqual(result["status"], "STALE_QUALIFIED_SIGNAL")
        self.assertTrue(result["investigate_webhook"])
        self.assertFalse(result["orders_enabled_by_diagnostic"])

    def test_legacy_signal_without_market_identity_not_counted(self):
        result = diagnose_signal_feed(
            latest_received_at=NOW - timedelta(minutes=5),
            last_qualified_at=None, eligible_count=0, now=NOW)
        self.assertEqual(result["status"], "NO_QUALIFYING_SIGNAL")

    def test_missing_or_future_data_stays_unverified(self):
        for count in (None, -1, True):
            with self.subTest(count=count):
                result = diagnose_signal_feed(
                    latest_received_at=NOW, last_qualified_at=NOW,
                    eligible_count=count, now=NOW)
                self.assertEqual(result["status"], "UNVERIFIED")
        result = diagnose_signal_feed(
            latest_received_at=NOW + timedelta(minutes=5),
            last_qualified_at=NOW, eligible_count=1, now=NOW)
        self.assertEqual(result["status"], "UNVERIFIED_CLOCK")

    def test_production_endpoint_does_not_write_to_exchange(self):
        snapshot = {"source": "GATE_IO_LIVE_FUTURES",
                    "account": {"position_mode": "dual"},
                    "positions": [], "orders": [], "triggers": [],
                    "fetched_at": int(NOW.timestamp())}
        state = {"safety_gates": ["SOURCE_LOCKED", "ENVIRONMENT_LOCKED"],
                 "halt_active": True, "certified": False,
                 "operator_requested_on": False}
        with patch.object(backend, "_terminal_permitted", return_value=True), \
             patch.object(backend, "_db_connect") as db, \
             patch("production_bot_control.state", return_value=state), \
             patch("live_futures_engine.GateLiveClient") as client_type:
            cur = db.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
            cur.fetchone.side_effect = [
                {"unresolved": 0},
                {"latest_received_at": NOW - timedelta(days=6),
                 "last_qualified_at": NOW - timedelta(days=6),
                 "eligible_count": 79},
            ]
            client = client_type.return_value
            client.snapshot.return_value = snapshot
            response = backend.app.test_client().get("/terminal/live-execution/unlock-readiness")
            self.assertEqual(response.status_code, 200)
            output = response.get_json()
            self.assertEqual(output["signal_feed"]["status"], "STALE_QUALIFIED_SIGNAL")
            self.assertFalse(output["orders_enabled_by_this_check"])
            self.assertFalse(output["signal_feed"]["orders_enabled_by_diagnostic"])
            client.submit_entry.assert_not_called()
            client.submit_trigger.assert_not_called()


if __name__ == "__main__":
    unittest.main()
