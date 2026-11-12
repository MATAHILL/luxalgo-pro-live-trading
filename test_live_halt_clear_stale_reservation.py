"""Regression tests for the CLEAR HALT stale-reservation fix.

The bug: clear_halt() refused whenever ANY ledger row was not
CLOSED_VERIFIED. A row left in RESERVED (written by risk_reserve() and never
advanced, because no order was ever sent) therefore blocked the operator
forever, even with a completely flat Gate.io account. The dashboard showed
only "HALT not cleared: LiveSafetyError", which reads like an open position.

The fix must keep the safety property: real exchange exposure still blocks.
These tests lock in both halves.
"""
import unittest
from unittest.mock import patch

import live_execution_coordinator as coord
import live_futures_engine as engine


class FakeCursor:
    """Minimal cursor: records SQL, replays scripted fetch results."""

    def __init__(self, store):
        self.store = store
        self.rowcount = 0
        self._result = None

    def execute(self, sql, params=None):
        self.store["sql"].append(" ".join(sql.split()))
        self._result = None
        self.rowcount = 0
        if "COUNT(*)::int AS unresolved" in sql:
            self._result = {"unresolved": self.store["unresolved_after"]}
        elif "FROM luxalgo_prod_execution_ledger" in sql and "SELECT" in sql:
            self._result = list(self.store["open_rows"])
        elif "UPDATE luxalgo_prod_execution_ledger" in sql:
            self.rowcount = 1
            self.store["released"].append(params[1] if params else None)
        elif "UPDATE luxalgo_prod_execution_control" in sql:
            self.rowcount = 1
            self.store["control_updated"] = True

    def fetchone(self):
        return self._result

    def fetchall(self):
        return self._result or []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeCon:
    def __init__(self, store):
        self.store = store

    def cursor(self):
        return FakeCursor(self.store)

    def execute(self, *a, **k):
        return None


def make_store(open_rows, unresolved_after=0):
    return {"sql": [], "open_rows": open_rows, "released": [],
            "unresolved_after": unresolved_after, "control_updated": False}


def clean_status():
    return {"reconciliation": {"requires_halt": False},
            "untracked_open_orders": 0, "untracked_open_triggers": 0,
            "open_positions": 0, "exchange_open_orders": 0,
            "exchange_open_triggers": 0}


def exposed_status():
    return {"reconciliation": {"requires_halt": True},
            "untracked_open_orders": 0, "untracked_open_triggers": 0,
            "open_positions": 1, "exchange_open_orders": 0,
            "exchange_open_triggers": 0}


STALE_ROW = {"event_id": "a" * 20, "contract": "JCT_USDT", "state": "RESERVED",
             "entry_order_id": None, "contracts": 1}
SENT_ROW = {"event_id": "b" * 20, "contract": "BTC_USDT", "state": "PROTECTED",
            "entry_order_id": "123456789", "contracts": 1}


class StaleReservationReleaseTests(unittest.TestCase):
    def _clear(self, store, status, **kw):
        with patch.object(coord, "install"), \
             patch.object(coord, "inspect", return_value=status):
            return coord.clear_halt(FakeCon(store), client=object(), **kw)

    def test_stale_reservation_blocks_by_default_and_names_the_row(self):
        store = make_store([STALE_ROW], unresolved_after=1)
        with self.assertRaises(engine.LiveSafetyError) as ctx:
            self._clear(store, clean_status())
        message = str(ctx.exception)
        # The operator must be told WHICH row, not just "LiveSafetyError".
        self.assertIn("a" * 20, message)
        self.assertIn("JCT_USDT", message)
        self.assertIn("RESERVED", message)
        self.assertFalse(store["control_updated"])

    def test_stale_reservation_released_when_exchange_is_flat(self):
        store = make_store([STALE_ROW], unresolved_after=0)
        result = self._clear(store, clean_status(),
                             release_stale_reservations=True)
        self.assertFalse(result["halted"])
        self.assertEqual(result["stale_reservations_released"], ["a" * 20])
        self.assertTrue(store["control_updated"])
        self.assertIn("a" * 20, store["released"])

    def test_release_refused_when_exchange_reports_a_position(self):
        store = make_store([STALE_ROW], unresolved_after=1)
        with self.assertRaises(engine.LiveSafetyError):
            self._clear(store, exposed_status(),
                        release_stale_reservations=True)
        self.assertFalse(store["control_updated"])
        self.assertEqual(store["released"], [])

    def test_release_refused_for_a_row_that_may_have_been_sent(self):
        # PROTECTED with an entry order id: it reached the exchange. Asking only
        # for the stale-reservation release must never close it, even on a flat
        # account. The operator has to request the flat-account release
        # explicitly, and even then this path is refused here.
        store = make_store([SENT_ROW], unresolved_after=1)
        with self.assertRaises(engine.LiveSafetyError) as ctx:
            self._clear(store, clean_status(),
                        release_stale_reservations=True)
        self.assertIn("reached the exchange", str(ctx.exception))
        self.assertIn("flat-account release", str(ctx.exception))
        self.assertFalse(store["control_updated"])
        self.assertEqual(store["released"], [])

    def test_sent_row_is_never_closed_when_the_exchange_reports_exposure(self):
        # THE safety property: a row that reached the exchange is never closed
        # while the exchange shows a position. Checked with BOTH flags on.
        store = make_store([SENT_ROW], unresolved_after=1)
        with self.assertRaises(engine.LiveSafetyError):
            self._clear(store, exposed_status(),
                        release_stale_reservations=True,
                        release_flat_reconciled=True)
        self.assertFalse(store["control_updated"])
        self.assertEqual(store["released"], [])

    def test_sent_row_is_closed_only_on_a_flat_account_with_explicit_flag(self):
        # The AKE_USDT case: the trade finished, Gate.io is flat, so only the
        # bot's own record is unfinished. Closing it must be explicit.
        store = make_store([SENT_ROW], unresolved_after=0)
        result = self._clear(store, clean_status(),
                             release_flat_reconciled=True)
        self.assertFalse(result["halted"])
        self.assertEqual(result["flat_account_reconciled"], ["b" * 20])
        self.assertEqual(result["stale_reservations_released"], [])
        self.assertTrue(store["control_updated"])
        self.assertIn("b" * 20, store["released"])

    def test_sent_row_still_refused_when_untracked_order_exists(self):
        store = make_store([SENT_ROW], unresolved_after=1)
        status = clean_status()
        status["untracked_open_orders"] = 1
        with self.assertRaises(engine.LiveSafetyError):
            self._clear(store, status, release_flat_reconciled=True)
        self.assertFalse(store["control_updated"])
        self.assertEqual(store["released"], [])

    def test_sent_row_still_refused_when_exchange_unreadable(self):
        store = make_store([SENT_ROW], unresolved_after=1)
        with patch.object(coord, "install"), \
             patch.object(coord, "inspect",
                          side_effect=engine.LiveSafetyError("GET failed")):
            with self.assertRaises(engine.LiveSafetyError):
                coord.clear_halt(FakeCon(store), client=object(),
                                 release_flat_reconciled=True)
        self.assertFalse(store["control_updated"])
        self.assertEqual(store["released"], [])

    def test_non_boolean_flat_flag_is_rejected(self):
        store = make_store([SENT_ROW], unresolved_after=1)
        with self.assertRaises(engine.LiveSafetyError):
            self._clear(store, clean_status(), release_flat_reconciled="yes")
        self.assertFalse(store["control_updated"])

    def test_release_refused_when_untracked_orders_exist(self):
        store = make_store([STALE_ROW], unresolved_after=1)
        status = clean_status()
        status["untracked_open_orders"] = 1
        with self.assertRaises(engine.LiveSafetyError):
            self._clear(store, status, release_stale_reservations=True)
        self.assertFalse(store["control_updated"])

    def test_release_refused_when_untracked_triggers_exist(self):
        store = make_store([STALE_ROW], unresolved_after=1)
        status = clean_status()
        status["untracked_open_triggers"] = 1
        with self.assertRaises(engine.LiveSafetyError):
            self._clear(store, status, release_stale_reservations=True)
        self.assertFalse(store["control_updated"])

    def test_clean_ledger_clears_without_release_flag(self):
        store = make_store([], unresolved_after=0)
        result = self._clear(store, clean_status())
        self.assertFalse(result["halted"])
        self.assertEqual(result["stale_reservations_released"], [])
        self.assertTrue(store["control_updated"])

    def test_non_boolean_release_flag_is_rejected(self):
        store = make_store([STALE_ROW], unresolved_after=1)
        with self.assertRaises(engine.LiveSafetyError):
            self._clear(store, clean_status(),
                        release_stale_reservations="yes")
        self.assertFalse(store["control_updated"])


class DiagnoseHaltTests(unittest.TestCase):
    def _diagnose(self, store, status):
        with patch.object(coord, "install"), \
             patch.object(coord, "inspect", return_value=status):
            return coord.diagnose_halt(FakeCon(store), client=object())

    def test_diagnose_separates_stale_from_unrecoverable(self):
        # With a genuinely exposed account, a sent row is NOT offered for
        # release: it stays in the manual-review bucket.
        store = make_store([STALE_ROW, SENT_ROW])
        out = self._diagnose(store, exposed_status())
        self.assertTrue(out["read_only"])
        self.assertEqual(out["unresolved_ledger_rows"], 2)
        self.assertEqual([r["event_id"] for r in out["stale_reservation_rows"]],
                         ["a" * 20])
        self.assertEqual([r["event_id"] for r in out["unrecoverable_rows"]],
                         ["b" * 20])
        self.assertEqual(out["flat_reconciled_rows"], [])
        self.assertFalse(out["exchange_flat"])
        self.assertFalse(out["can_clear_with_stale_release"])
        self.assertFalse(out["can_clear_with_flat_release"])
        self.assertFalse(out["can_clear"])

    def test_diagnose_marks_sent_row_flat_when_account_is_flat(self):
        # The AKE_USDT case: flat account, finished trade, unfinished record.
        store = make_store([SENT_ROW])
        out = self._diagnose(store, clean_status())
        self.assertTrue(out["exchange_flat"])
        self.assertEqual([r["event_id"] for r in out["flat_reconciled_rows"]],
                         ["b" * 20])
        self.assertEqual(out["unrecoverable_rows"], [])
        self.assertTrue(out["can_clear_with_flat_release"])

    def test_diagnose_offers_release_for_stale_only(self):
        store = make_store([STALE_ROW])
        out = self._diagnose(store, clean_status())
        self.assertTrue(out["can_clear_with_stale_release"])
        self.assertFalse(out["can_clear"])

    def test_diagnose_never_mutates(self):
        store = make_store([STALE_ROW])
        self._diagnose(store, clean_status())
        self.assertFalse(store["control_updated"])
        self.assertEqual(store["released"], [])
        self.assertFalse(any("UPDATE" in s for s in store["sql"]))

    def test_diagnose_reports_unreadable_exchange_as_unknown(self):
        store = make_store([STALE_ROW])
        with patch.object(coord, "install"), \
             patch.object(coord, "inspect",
                          side_effect=engine.LiveSafetyError("GET failed")):
            out = coord.diagnose_halt(FakeCon(store), client=object())
        self.assertFalse(out["exchange_reachable"])
        self.assertFalse(out["can_clear_with_stale_release"])
        self.assertFalse(out["can_clear"])
        self.assertTrue(any("EXCHANGE_UNREADABLE" in r
                            for r in out["blocking_reasons"]))

    def test_diagnose_reports_exposure_blocking(self):
        store = make_store([STALE_ROW])
        out = self._diagnose(store, exposed_status())
        self.assertFalse(out["can_clear_with_stale_release"])
        self.assertIn("EXCHANGE_OR_LEDGER_RECONCILIATION_ANOMALY",
                      out["blocking_reasons"])


class HaltDiagnoseRouteTests(unittest.TestCase):
    def test_route_is_registered(self):
        import app as backend
        rules = {r.rule for r in backend.app.url_map.iter_rules()}
        self.assertIn("/api/live/halt-diagnose", rules)

    def test_route_requires_authentication(self):
        import os
        import app as backend
        with patch.dict(os.environ, {"TERMINAL_PASSWORD": "x" * 28,
                                     "TERMINAL_USER": "admin"}):
            client = backend.app.test_client()
            self.assertEqual(
                client.get("/api/live/halt-diagnose").status_code, 401)

    def test_halt_clear_route_accepts_the_flat_reconciled_flag(self):
        # The emergency button sends both flags. The route must forward them
        # rather than silently dropping the new one.
        import inspect
        import app as backend
        source = inspect.getsource(backend.api_live_halt_clear)
        self.assertIn("release_flat_reconciled", source)
        self.assertIn("release_stale_reservations", source)

    def test_halt_clear_route_rejects_a_non_boolean_flat_flag(self):
        import os
        import app as backend
        with patch.dict(os.environ, {"TERMINAL_PASSWORD": "x" * 28,
                                     "TERMINAL_USER": "admin"}):
            client = backend.app.test_client()
            # Unauthenticated first: the guard must win before any parsing.
            self.assertEqual(
                client.post("/api/live/halt-clear",
                            json={"release_flat_reconciled": "yes"}).status_code,
                401)


class EmergencyButtonTests(unittest.TestCase):
    """The one-press emergency control must diagnose before it clears."""

    @classmethod
    def setUpClass(cls):
        import production_cutover_view as view
        cls.html = view.render() if hasattr(view, "render") else None
        if cls.html is None:
            import inspect
            cls.html = inspect.getsource(view)

    def test_emergency_button_is_rendered(self):
        self.assertIn('id="productionEmergencyClear"', self.html)
        self.assertIn("EMERGENCY: CLEAR HALT NOW", self.html)

    def test_emergency_button_calls_the_read_only_diagnostic_first(self):
        self.assertIn("/api/live/halt-diagnose", self.html)
        # The confirm control starts hidden: nothing clears on the first press.
        self.assertIn('id="productionEmergencyConfirm"', self.html)
        self.assertIn("hidden>YES", self.html)

    def test_emergency_confirm_sends_both_release_flags(self):
        self.assertIn("release_stale_reservations:true", self.html)
        self.assertIn("release_flat_reconciled:true", self.html)

    def test_emergency_confirm_is_only_shown_when_server_says_safe(self):
        self.assertIn("can_clear_with_flat_release", self.html)
        self.assertIn("can_clear_with_stale_release", self.html)

    def test_emergency_panel_reports_what_was_cleared(self):
        self.assertIn("What was cleared", self.html)

    def test_emergency_panel_states_the_safety_property(self):
        self.assertIn("never clears while a position", self.html)


if __name__ == "__main__":
    unittest.main()
