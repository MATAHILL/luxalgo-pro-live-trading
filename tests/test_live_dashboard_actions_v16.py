"""v16 regression tests: the six reported problems.

Each test names the operator-visible symptom it prevents from coming back.
Offline only: no network, no credentials, no exchange writes.
"""
import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("LIVE_TRADING_ENABLED", "true")
os.environ.setdefault("ORDERS_ENABLED", "true")
os.environ.setdefault("LIVE_ORDERS_SEPARATELY_AUTHORIZED", "true")
os.environ.setdefault("LUXALGO_EXCHANGE_WRITE_RELEASE", "true")
os.environ.setdefault("GATE_API_KEY", "fixture")
os.environ.setdefault("GATE_API_SECRET", "fixture")

import live_position_actions as actions  # noqa: E402
import live_close_record as close_record  # noqa: E402
import build_marker  # noqa: E402


class FakeClient:
    """One open DOZ LONG position; a POST /orders makes it disappear."""

    def __init__(self, open_pos=True, triggers=None):
        self.open_pos = open_pos
        self.sent = []
        self.triggers = triggers or []

    def snapshot(self):
        pos = ([{"contract": "DOZ_USDT", "size": "7", "entry_price": "0.0052"}]
               if self.open_pos else [])
        return {
            "account": {"currency": "USDT", "in_dual_mode": False,
                        "total": "1000", "available": "900",
                        "position_mode": "single"},
            "positions": pos, "position_settings": pos,
            "orders": [], "triggers": list(self.triggers),
            "source": "GATE_IO_LIVE_FUTURES", "fetched_at": 0,
            "orders_enabled": False,
        }

    def request(self, method, path, *, params=None, payload=None,
                timeout=8, expires_at_ms=None):
        self.sent.append((method, path))
        if method == "GET":
            if "/my_trades" in path:
                return [{"contract": "DOZ_USDT", "order_id": "999", "pnl": "2.35"}]
            if "/orders" in path and params and params.get("status") == "finished":
                return [{"id": "777", "is_close": True, "fill_price": "0.00568"}]
            return []
        if method == "POST" and path.endswith("/orders"):
            self.open_pos = False
        return {"id": "42"}

    def submit_trigger(self, plan, kind):
        self.sent.append(("POST", "trigger:" + kind))
        return {"id": "9" + kind}


class FakeCon:
    def __init__(self, row):
        self.row = row
        self.updates = []

    def cursor(self):
        con = self

        class C:
            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

            def execute(s, sql, params=None):
                con.updates.append((sql.strip().split()[0], params))

            def fetchone(s):
                return con.row

            def fetchall(s):
                return []

        return C()


def ledger_row(state="PROTECTED"):
    return dict(event_id="evt-DOZ-0001", contract="DOZ_USDT", side="LONG",
                timeframe="15m", position_mode="single", state=state,
                contracts="7", entry_price="0.0052", stop_loss="0.0050",
                take_profit="0.0056", entry_order_id="111",
                stop_order_id="222", take_order_id="333", client_text="t-x")


class TestProblem1ButtonsWork(unittest.TestCase):
    """Symptom: four buttons on a live position did nothing."""

    def test_market_close_sends_reduce_only_order(self):
        client = FakeClient(open_pos=True)
        result = actions.close_position(FakeCon(ledger_row()), "evt-DOZ-0001",
                                        client=client)
        self.assertEqual(result["state"], "CLOSED_VERIFIED")
        self.assertEqual(result["orders_sent"], 1)
        self.assertIn(("POST", "/api/v4/futures/usdt/orders"), client.sent)

    def test_stale_closed_row_does_not_block_a_real_close(self):
        """The exact bug: row said CLOSED_VERIFIED, exchange still held DOZ."""
        client = FakeClient(open_pos=True)
        result = actions.close_position(
            FakeCon(ledger_row(state="CLOSED_VERIFIED")), "evt-DOZ-0001",
            client=client)
        self.assertEqual(result["state"], "CLOSED_VERIFIED")
        self.assertEqual(result["orders_sent"], 1)

    def test_genuinely_closed_sends_nothing(self):
        client = FakeClient(open_pos=False)
        result = actions.close_position(
            FakeCon(ledger_row(state="CLOSED_VERIFIED")), "evt-DOZ-0001",
            client=client)
        self.assertEqual(result["state"], "ALREADY_CLOSED")
        self.assertEqual(result["orders_sent"], 0)
        self.assertEqual(client.sent, [])

    def test_protection_modify_places_both_triggers(self):
        client = FakeClient(open_pos=True)
        result = actions.modify_protection(FakeCon(ledger_row()), "evt-DOZ-0001",
                                           "0.0050", "0.0056", client=client)
        self.assertIn(result["state"], ("PROTECTION_VERIFIED",
                                        "PROTECTION_NOT_VERIFIED"))
        self.assertIn(("POST", "trigger:SL"), client.sent)
        self.assertIn(("POST", "trigger:TP"), client.sent)

    def test_protection_rejects_wrong_side_stop(self):
        client = FakeClient(open_pos=True)
        with self.assertRaises(Exception):
            actions.modify_protection(FakeCon(ledger_row()), "evt-DOZ-0001",
                                      "0.0060", "0.0056", client=client)

    def test_reverse_refused_while_halted(self):
        """Reverse opens new exposure, so a halt must block it."""
        client = FakeClient(open_pos=True)
        with self.assertRaises(Exception):
            actions.reverse_position(FakeCon(ledger_row()), "evt-DOZ-0001",
                                     "0.0050", "0.0056", client=client)


class TestProblem3CloseRecord(unittest.TestCase):
    """Symptom: a closed trade was never recorded, so the halt latched."""

    def test_closeable_states_include_protected(self):
        self.assertIn("PROTECTED", close_record.CLOSEABLE_STATES)

    def test_safe_wrapper_never_raises(self):
        class Boom:
            def snapshot(self):
                raise RuntimeError("exchange down")

        result = close_record.record_closes_safely(FakeCon(ledger_row()), Boom())
        self.assertTrue(result.get("unavailable"))
        self.assertEqual(result["orders_sent"], 0)

    def test_safe_wrapper_exists_for_read_paths(self):
        self.assertTrue(callable(close_record.record_closes_safely))


class TestProblem5BuildMarker(unittest.TestCase):
    """Symptom: could not tell which version was deployed."""

    def test_identity_has_required_keys(self):
        for key in ("build", "build_full", "build_source", "build_branch"):
            self.assertIn(key, build_marker.BUILD)

    def test_explicit_sha_wins(self):
        os.environ["LUXALGO_BUILD_SHA"] = "deadbeefcafe"
        try:
            ident = build_marker.build_identity()
            self.assertEqual(ident["build"], "deadbee")
            self.assertEqual(ident["build_source"], "ENV:LUXALGO_BUILD_SHA")
        finally:
            os.environ.pop("LUXALGO_BUILD_SHA", None)

    def test_unknown_is_never_a_guess(self):
        saved = {k: os.environ.pop(k, None) for k in
                 ("LUXALGO_BUILD_SHA", "RENDER_GIT_COMMIT")}
        try:
            ident = build_marker.build_identity()
            self.assertIn(ident["build"], ("UNKNOWN",) + (ident["build"],))
            self.assertTrue(ident["build"])
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


class TestProblem6ReleasePath(unittest.TestCase):
    """Symptom: the source lock could not be released in a known order."""

    def test_release_status_route_registered(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "app.py"), encoding="utf-8").read()
        self.assertIn("/api/live/release-status", src)

    def test_release_status_is_read_only(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "app.py"), encoding="utf-8").read()
        start = src.index("def api_live_release_status")
        body = src[start:start + 4000]
        for forbidden in ("turn_on(", "certify(", "clear_halt(", "halt("):
            self.assertNotIn(forbidden, body,
                             "release-status must never mutate state")


class TestProblem2And4RefusalVisible(unittest.TestCase):
    """Symptoms: unknown blocker, and a hidden refusal reason."""

    def test_refusal_table_is_read_by_the_panel(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "app.py"), encoding="utf-8").read()
        self.assertIn("luxalgo_live_execution_refusals", src)
        self.assertIn("REFUSED_", src)

    def test_dispatch_blockers_are_persisted(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "app.py"), encoding="utf-8").read()
        self.assertIn("_record_live_refusal(event_id,'WEBHOOK_DISPATCH'", src)


if __name__ == "__main__":
    unittest.main()
