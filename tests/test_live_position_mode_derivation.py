"""Regression tests: Gate reports hedge mode as in_dual_mode, not position_mode.

Before this fix every live entry was refused with
"Authenticated live position mode unknown or unsupported" because the code read
account["position_mode"], a field Gate.io's /futures/usdt/accounts never sends.
"""
import unittest

from live_futures_engine import account_position_mode, GateLiveClient, LiveSafetyError


class AccountPositionModeTests(unittest.TestCase):
    def test_dual_mode_true_derives_dual(self):
        self.assertEqual(account_position_mode({"currency": "USDT", "in_dual_mode": True}), "dual")

    def test_dual_mode_false_derives_single(self):
        self.assertEqual(account_position_mode({"currency": "USDT", "in_dual_mode": False}), "single")

    def test_real_gate_payload_without_position_mode_is_usable(self):
        # The exact shape Gate returns: no position_mode key at all.
        account = {"currency": "USDT", "total": "1000", "available": "900",
                   "unrealised_pnl": "0", "in_dual_mode": True}
        self.assertNotIn("position_mode", account)
        self.assertEqual(account_position_mode(account), "dual")

    def test_missing_everything_is_unknown(self):
        self.assertIsNone(account_position_mode({"currency": "USDT"}))

    def test_non_dict_is_unknown(self):
        self.assertIsNone(account_position_mode(None))
        self.assertIsNone(account_position_mode("dual"))

    def test_contradictory_fields_fail_closed(self):
        self.assertIsNone(account_position_mode({"in_dual_mode": True, "position_mode": "single"}))
        self.assertIsNone(account_position_mode({"in_dual_mode": False, "position_mode": "dual"}))

    def test_agreeing_fields_are_accepted(self):
        self.assertEqual(account_position_mode({"in_dual_mode": True, "position_mode": "dual"}), "dual")
        self.assertEqual(account_position_mode({"in_dual_mode": False, "position_mode": "single"}), "single")

    def test_unrecognised_string_fails_closed(self):
        self.assertIsNone(account_position_mode({"position_mode": "hedge"}))
        self.assertIsNone(account_position_mode({"in_dual_mode": True, "position_mode": "hedge"}))

    def test_string_fallback_when_no_boolean(self):
        self.assertEqual(account_position_mode({"position_mode": "DUAL"}), "dual")

    def test_boolean_position_mode_is_not_trusted_as_a_string(self):
        self.assertIsNone(account_position_mode({"position_mode": True}))


class SnapshotNormalisationTests(unittest.TestCase):
    def _client(self, account):
        client = GateLiveClient.__new__(GateLiveClient)
        client.key = "k"
        client.secret = "s"

        def request(method, path, params=None, payload=None, expires_at_ms=None, timeout=10):
            if path.endswith("/accounts"):
                return account
            if path.endswith("/positions"):
                return []
            if path.endswith("/orders"):
                return []
            if path.endswith("/price_orders"):
                return []
            raise AssertionError("unexpected path " + path)

        client.request = request
        return client

    def test_snapshot_exposes_derived_position_mode(self):
        snap = self._client({"currency": "USDT", "in_dual_mode": True}).snapshot()
        self.assertEqual(snap["account"]["position_mode"], "dual")

    def test_snapshot_does_not_mutate_the_exchange_payload(self):
        account = {"currency": "USDT", "in_dual_mode": False}
        self._client(account).snapshot()
        self.assertNotIn("position_mode", account)

    def test_snapshot_unknown_mode_stays_none(self):
        snap = self._client({"currency": "USDT"}).snapshot()
        self.assertIsNone(snap["account"]["position_mode"])

    def test_snapshot_still_rejects_non_usdt_account(self):
        with self.assertRaises(LiveSafetyError):
            self._client({"currency": "BTC", "in_dual_mode": True}).snapshot()


if __name__ == "__main__":
    unittest.main()
