"""Regression: the halt must not fire after every trade, and must self-release.

The bug
-------
A trade closing normally (native TP/SL hit, or a manual close) leaves its
sibling protective trigger resting on Gate.io for a few seconds while the
exchange cancels it asynchronously. The 60-second reconciler saw

    position gone  +  trigger still resting

and called that ``POSITION_MISSING_OR_AMBIGUOUS`` -> ``HALT_RECONCILIATION``
-> latched ``UNPROTECTED_OR_UNKNOWN_EXCHANGE_EXPOSURE``.

Nothing ever re-checked that decision. Even after the account was completely
flat and the ledger row had reached ``CLOSED_VERIFIED``, the halt stayed
latched forever. The bot could trade exactly once, then needed a human to
clear the halt before the next signal could run.

Two defects, both fixed here:

1. ``reconcile_ledger`` treated "position closed, sibling trigger awaiting
   cancellation" as unprotected exposure. With no open position there is
   nothing to protect, and a reduce-only trigger cannot open one.
2. ``reconcile_and_halt_if_needed`` only ever SET the halt. It never released
   a halt it had itself latched once the risk was provably gone.

What must still hold
--------------------
Genuine unprotected exposure must still halt: an open position with missing,
mismatched or wrong-side protection. An operator halt must never be released
automatically. An unfinished ledger row must keep the halt latched.
"""
import unittest

from live_futures_engine import (
    build_protective_trigger, reconcile_ledger, LiveSafetyError,
)
import live_execution_coordinator as coordinator


CONTRACT = "DOGE_USDT"
ENTRY, SL, TP = "0.0648", "0.0655", "0.0634"
MODE = "dual"


def _plan():
    return dict(contract=CONTRACT, side="SHORT", entry=ENTRY, sl=SL, tp=TP,
                position_mode=MODE)


def _trigger(kind, oid, plan=None):
    plan = plan or _plan()
    exp = build_protective_trigger(plan, kind)
    return {"id": int(oid), "id_string": str(oid), "contract": CONTRACT,
            "status": "open", "order_type": exp["order_type"],
            "initial": {"contract": CONTRACT, "size": 0, "price": "0",
                        "reduce_only": True, "is_reduce_only": True,
                        "is_close": True, "tif": "ioc"},
            "trigger": exp["trigger"]}


def _row(state="PROTECTED"):
    return dict(event_id="evt1", client_text="t-lxp-1", contract=CONTRACT,
                side="SHORT", state=state, contracts=100,
                entry_price=ENTRY, stop_loss=SL, take_profit=TP,
                position_mode=MODE, stop_order_id="101", take_order_id="102")


def _position(size="-100"):
    return dict(contract=CONTRACT, size=size, entry_price=ENTRY,
                mark_price="0.06529")


class HaltDoesNotFireAfterEveryTrade(unittest.TestCase):
    """Defect 1: a normal close is not unprotected exposure."""

    def test_position_closed_with_sibling_trigger_resting_does_not_halt(self):
        """The exact state that halted the bot after every single trade."""
        result = reconcile_ledger([_row()], [], [_trigger("SL", 101)])
        self.assertFalse(result["requires_halt"])
        states = [r["state"] for r in result["results"]]
        self.assertIn("CLOSE_PENDING_CLEANUP", states)

    def test_pending_cleanup_is_not_reported_as_closed(self):
        """The row must stay open until the exchange is fully clear, so the
        exit record is never written before the trade has finished."""
        result = reconcile_ledger([_row()], [], [_trigger("SL", 101)])
        self.assertNotIn("CLOSED_READBACK",
                         [r["state"] for r in result["results"]])

    def test_fully_flat_account_reports_closed(self):
        """Once the close recorder has marked the row CLOSED_VERIFIED, a flat
        account is a clean readback, not an anomaly."""
        result = reconcile_ledger([_row(state="CLOSED_VERIFIED")], [], [])
        self.assertFalse(result["requires_halt"])
        self.assertIn("CLOSED_READBACK",
                      [r["state"] for r in result["results"]])

    def test_protected_row_with_nothing_on_exchange_still_halts(self):
        """A PROTECTED row whose position vanished, with no trigger to explain
        it, is genuinely ambiguous. The close recorder must resolve it first;
        reconcile_ledger alone must never assume it closed."""
        result = reconcile_ledger([_row()], [], [])
        self.assertTrue(result["requires_halt"])
        self.assertIn("POSITION_MISSING_OR_AMBIGUOUS",
                      [r["reason"] for r in result["results"]])

    def test_position_vanished_with_nothing_explaining_it_still_halts(self):
        """No position and no trigger is only safe when the row can close.
        A row that cannot close must still halt."""
        result = reconcile_ledger([_row(state="SENDING")], [], [])
        self.assertTrue(result["requires_halt"])

    def test_open_position_without_protection_still_halts(self):
        """Genuine unprotected exposure must never be relaxed."""
        result = reconcile_ledger([_row()], [_position()], [])
        self.assertTrue(result["requires_halt"])
        self.assertIn("HALT_UNPROTECTED",
                      [r["state"] for r in result["results"]])

    def test_open_position_with_wrong_side_protection_still_halts(self):
        bad = _trigger("SL", 101)
        bad["trigger"] = dict(bad["trigger"], price="0.9999")
        result = reconcile_ledger([_row()], [_position()],
                                  [bad, _trigger("TP", 102)])
        self.assertTrue(result["requires_halt"])

    def test_untracked_exchange_position_still_halts(self):
        result = reconcile_ledger([], [_position()], [])
        self.assertTrue(result["requires_halt"])
        self.assertIn("HALT_UNKNOWN_EXPOSURE",
                      [r["state"] for r in result["results"]])


class _FakeCursor:
    def __init__(self, store):
        self.store = store
        self._result = None
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.rowcount = 0
        if "pg_advisory_xact_lock" in s:
            self._result = None
        elif s.startswith("SELECT halted,reason FROM luxalgo_prod_execution_control"):
            self._result = dict(self.store["control"])
        elif "COUNT(*)::int AS unresolved" in s:
            self._result = {"unresolved": self.store["unresolved"]}
        elif s.startswith("UPDATE luxalgo_prod_execution_control"):
            new_reason, expected = params
            if (self.store["control"]["halted"] is True
                    and self.store["control"]["reason"] == expected):
                self.store["control"] = {"halted": False, "reason": new_reason}
                self.rowcount = 1
            self._result = None
        else:
            self._result = None

    def fetchone(self):
        return self._result


class _FakeCon:
    def __init__(self, store):
        self.store = store

    def cursor(self):
        return _FakeCursor(self.store)


class HaltSelfReleasesOnceRiskIsGone(unittest.TestCase):
    """Defect 2: a halt this module latched must be released when resolved."""

    def _status(self, flat=True, requires_halt=False):
        return {"open_positions": 0 if flat else 1,
                "exchange_open_orders": 0,
                "exchange_open_triggers": 0 if flat else 1,
                "untracked_open_orders": 0,
                "untracked_open_triggers": 0,
                "reconciliation": {"requires_halt": requires_halt}}

    def _store(self, halted=True, reason="UNPROTECTED_OR_UNKNOWN_EXCHANGE_EXPOSURE",
               unresolved=0):
        return {"control": {"halted": halted, "reason": reason},
                "unresolved": unresolved}

    def test_auto_latched_halt_releases_when_account_is_flat(self):
        store = self._store()
        out = coordinator._release_auto_latched_halt(_FakeCon(store), self._status())
        self.assertIsNotNone(out)
        self.assertFalse(store["control"]["halted"])
        self.assertEqual(store["control"]["reason"],
                         "AUTO_RELEASED_EXPOSURE_RESOLVED")

    def test_operator_halt_is_never_auto_released(self):
        store = self._store(reason="OPERATOR_PERSISTENT_HALT")
        out = coordinator._release_auto_latched_halt(_FakeCon(store), self._status())
        self.assertIsNone(out)
        self.assertTrue(store["control"]["halted"])

    def test_acceptance_halt_is_never_auto_released(self):
        store = self._store(reason="SINGLE_ACCEPTANCE_COMPLETE_REVIEW_REQUIRED")
        out = coordinator._release_auto_latched_halt(_FakeCon(store), self._status())
        self.assertIsNone(out)
        self.assertTrue(store["control"]["halted"])

    def test_halt_stays_when_exposure_remains(self):
        store = self._store()
        out = coordinator._release_auto_latched_halt(
            _FakeCon(store), self._status(flat=False))
        self.assertIsNone(out)
        self.assertTrue(store["control"]["halted"])

    def test_halt_stays_when_reconciliation_still_requires_it(self):
        store = self._store()
        out = coordinator._release_auto_latched_halt(
            _FakeCon(store), self._status(flat=True, requires_halt=True))
        self.assertIsNone(out)
        self.assertTrue(store["control"]["halted"])

    def test_halt_stays_while_a_ledger_row_is_unfinished(self):
        store = self._store(unresolved=1)
        out = coordinator._release_auto_latched_halt(_FakeCon(store), self._status())
        self.assertIsNone(out)
        self.assertTrue(store["control"]["halted"])

    def test_unreadable_status_never_releases(self):
        store = self._store()
        for bad in (None, {}, "unreadable"):
            with self.subTest(status=bad):
                out = coordinator._release_auto_latched_halt(_FakeCon(store), bad)
                self.assertIsNone(out)
                self.assertTrue(store["control"]["halted"])

    def test_already_clear_halt_is_a_no_op(self):
        store = self._store(halted=False, reason="fixture")
        out = coordinator._release_auto_latched_halt(_FakeCon(store), self._status())
        self.assertIsNone(out)
        self.assertFalse(store["control"]["halted"])


class ContinuousTrading(unittest.TestCase):
    """The whole point: one trade after another, no halt in between."""

    def test_full_trade_cycle_never_halts(self):
        row = _row()
        sl, tp = _trigger("SL", 101), _trigger("TP", 102)

        # 1. entry filled, protection resting
        self.assertFalse(reconcile_ledger([row], [_position()], [sl, tp])
                         ["requires_halt"])
        # 2. TP hit; Gate has not yet cancelled the SL. This is the state that
        #    used to latch the halt after every single trade.
        self.assertFalse(reconcile_ledger([row], [], [sl])["requires_halt"])
        # 3. Gate cancels the SL; the close recorder marks the row closed and
        #    the account is flat.
        self.assertFalse(reconcile_ledger([_row(state="CLOSED_VERIFIED")], [], [])
                         ["requires_halt"])

    def test_second_trade_allowed_after_first_closes(self):
        history = _row()
        history["event_id"] = "evt1"
        history["state"] = "CLOSED_VERIFIED"
        second = _row()
        second["event_id"] = "evt2"
        second["client_text"] = "t-lxp-2"
        result = reconcile_ledger([history, second], [_position()],
                                  [_trigger("SL", 101), _trigger("TP", 102)])
        self.assertFalse(result["requires_halt"])


if __name__ == "__main__":
    unittest.main()
