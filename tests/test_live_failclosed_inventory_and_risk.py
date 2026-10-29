"""Non-monetary fail-closed acceptance regressions. Zero exchange writes."""
import unittest
from unittest.mock import MagicMock

from live_futures_engine import (
    LiveSafetyError, active_positions, build_protective_trigger,
    evaluate_protection, reconcile_ledger,
)
from live_execution_coordinator import risk_reserve, read_rows


class InventoryAndRiskFailClosedTests(unittest.TestCase):
    def test_missing_and_malformed_exchange_position_size_never_means_flat(self):
        for bad in (None, "", "  ", "garbage", "NaN", "Infinity", True, False):
            with self.subTest(size=repr(bad)), self.assertRaises(LiveSafetyError):
                active_positions([{"contract": "BTC_USDT", "size": bad}])
        with self.assertRaises(LiveSafetyError):
            active_positions([{"contract": "BTC_USDT"}])
        self.assertEqual(active_positions([{"contract": "BTC_USDT", "size": "0"}]), [])
        self.assertEqual(len(active_positions([{"contract": "BTC_USDT", "size": "-1"}])), 1)

    def _fake_reservation(self, rows=None):
        con = MagicMock()
        cursor = con.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {"halted": False}
        cursor.fetchall.return_value = rows if rows is not None else []
        return con, cursor

    def test_atomic_reservation_rejects_over_point_five_percent(self):
        con, cursor = self._fake_reservation()
        plan = {"contract": "BTC_USDT", "estimated_risk_usdt": "5.01"}
        with self.assertRaisesRegex(LiveSafetyError, "per-entry 0.5%"):
            risk_reserve(con, plan, [], "1000")
        self.assertFalse(any("INSERT INTO luxalgo_prod_execution_ledger" in str(c)
                             for c in cursor.execute.call_args_list))

    def test_atomic_reservation_rejects_invalid_or_nonpositive_risk(self):
        for risk in ("0", "-1", "NaN", "Infinity", "garbage", None, True):
            with self.subTest(risk=repr(risk)):
                con, _ = self._fake_reservation()
                with self.assertRaises(LiveSafetyError):
                    risk_reserve(con,
                                 {"contract": "BTC_USDT", "estimated_risk_usdt": risk},
                                 [], "1000")

    def test_corrupt_existing_risk_never_frees_a_position_slot(self):
        for risk in ("NaN", "-1", "Infinity"):
            with self.subTest(risk=risk):
                con, _ = self._fake_reservation(
                    [{"contract": "SOL_USDT", "state": "PROTECTED", "risk_usdt": risk}])
                with self.assertRaises(LiveSafetyError):
                    risk_reserve(con,
                                 {"contract": "BTC_USDT", "estimated_risk_usdt": "1"},
                                 [], "1000")

    def test_pending_journal_cannot_become_protected_by_readback_alone(self):
        plan = {"contract": "BTC_USDT", "side": "LONG", "position_mode": "single",
                "entry": "80000", "sl": "79000", "tp": "82000"}
        native = []
        for kind, order_id in (("SL", 800101), ("TP", 800102)):
            native.append(dict(build_protective_trigger(plan, kind),
                               status="open", id=order_id))
        self.assertTrue(evaluate_protection(plan, native)["verified"])
        row = {"event_id": "test", "contract": "BTC_USDT", "side": "LONG",
               "state": "ACK_UNKNOWN", "position_mode": "single",
               "entry_price": "80000", "stop_loss": "79000", "take_profit": "82000"}
        result = reconcile_ledger([row], [{"contract": "BTC_USDT", "size": "1"}], native)
        self.assertTrue(result["requires_halt"])
        self.assertEqual(result["results"][0]["reason"], "UNVERIFIED_LEDGER_LIFECYCLE_STATE")

    def test_readback_prioritizes_all_active_rows_over_recent_closed_history(self):
        con = MagicMock()
        cursor = con.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = []
        self.assertEqual(read_rows(con), [])
        sql = cursor.execute.call_args.args[0]
        self.assertIn("ORDER BY (state <> 'CLOSED_VERIFIED') DESC", sql)


if __name__ == "__main__":
    unittest.main()
