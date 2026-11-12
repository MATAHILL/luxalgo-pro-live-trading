"""Regression tests for the three dashboard defects fixed in v15.

1. A position closed on the exchange must be recorded CLOSED_VERIFIED, so the
   bot does not latch EMERGENCY_HALT and the exit record is not empty.
2. The operator close/protection/reverse actions must exist and be reachable.
3. A persisted refusal must be readable, not write-only.
"""
import json
import unittest
from decimal import Decimal
from unittest.mock import patch, MagicMock

import live_close_record as close_record
import live_position_actions as actions
from live_futures_engine import LiveSafetyError


class FakeCursor:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.executed = []
        self.rowcount = 0
        self._fetch = None

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if "SELECT event_id,contract,side,state,contracts" in sql:
            self._fetch = self.rows
        elif "SELECT event_id,contract,side,timeframe,position_mode" in sql:
            self._fetch = self.rows[0] if self.rows else None
        elif "UPDATE luxalgo_prod_execution_ledger" in sql:
            self.rowcount = 1
            self._fetch = None
        else:
            self._fetch = None

    def fetchall(self):
        return self._fetch or []

    def fetchone(self):
        return self._fetch

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeCon:
    def __init__(self, rows=None):
        self.cur = FakeCursor(rows)
        self.commits = 0

    def cursor(self):
        return self.cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeClient:
    """Minimal Gate client: flat account, no orders, no triggers."""

    def __init__(self, positions=None, orders=None, triggers=None,
                 trades=None, finished=None):
        self._positions = positions or []
        self._orders = orders or []
        self._triggers = triggers or []
        self._trades = trades or []
        self._finished = finished or []
        self.posts = []
        self.deletes = []

    def snapshot(self):
        return {"account": {"currency": "USDT", "in_dual_mode": False},
                "positions": self._positions, "orders": self._orders,
                "triggers": self._triggers, "source": "GATE_IO_LIVE_FUTURES",
                "fetched_at": 1}

    def request(self, method, path, *, params=None, payload=None, **kw):
        if method == "GET" and path.endswith("/my_trades"):
            return self._trades
        if method == "GET" and path.endswith("/orders"):
            return self._finished
        if method == "POST":
            self.posts.append((path, payload))
            return {"id": "12345"}
        if method == "DELETE":
            self.deletes.append(path)
            return {}
        return {}

    def submit_trigger(self, plan, kind):
        self.posts.append(("trigger-" + kind, plan))
        return {"id": "999"}


class TestCloseDetection(unittest.TestCase):
    def test_flat_exchange_records_closed_verified(self):
        row = dict(event_id="a" * 20, contract="DOZ_USDT", side="LONG",
                   state="PROTECTED", contracts="10", entry_price="1",
                   stop_loss="0.9", take_profit="1.2",
                   entry_order_id="111", client_text="t-lxp-x")
        con = FakeCon([row])
        client = FakeClient(trades=[{"contract": "DOZ_USDT", "order_id": "222",
                                     "pnl": "1.5"}])
        out = close_record.detect_and_record_closes(con, client)
        self.assertEqual(out["recorded_count"], 1)
        self.assertEqual(out["recorded"][0]["realized_pnl_usdt"], "1.5")
        self.assertEqual(out["orders_sent"], 0)
        updates = [s for s, p in con.cur.executed
                   if "UPDATE luxalgo_prod_execution_ledger" in s]
        self.assertEqual(len(updates), 1)

    def test_open_position_is_never_closed(self):
        row = dict(event_id="b" * 20, contract="DOZ_USDT", side="LONG",
                   state="PROTECTED", contracts="10", entry_price="1",
                   stop_loss="0.9", take_profit="1.2",
                   entry_order_id="111", client_text="t-lxp-y")
        con = FakeCon([row])
        client = FakeClient(positions=[{"contract": "DOZ_USDT", "size": "10"}])
        out = close_record.detect_and_record_closes(con, client)
        self.assertEqual(out["recorded_count"], 0)
        self.assertEqual(out["skipped"][0]["reason"], "STILL_OPEN_OR_PROTECTED")

    def test_resting_trigger_blocks_close(self):
        row = dict(event_id="c" * 20, contract="DOZ_USDT", side="LONG",
                   state="PROTECTED", contracts="10", entry_price="1",
                   stop_loss="0.9", take_profit="1.2",
                   entry_order_id="111", client_text="t-lxp-z")
        con = FakeCon([row])
        client = FakeClient(triggers=[{"initial": {"contract": "DOZ_USDT"}}])
        out = close_record.detect_and_record_closes(con, client)
        self.assertEqual(out["recorded_count"], 0)

    def test_unreadable_pnl_is_none_not_zero(self):
        row = dict(event_id="d" * 20, contract="DOZ_USDT", side="LONG",
                   state="PROTECTED", contracts="10", entry_price="1",
                   stop_loss="0.9", take_profit="1.2",
                   entry_order_id="111", client_text="t-lxp-w")
        con = FakeCon([row])
        client = FakeClient(trades=[])
        out = close_record.detect_and_record_closes(con, client)
        self.assertIsNone(out["recorded"][0]["realized_pnl_usdt"])


class TestPositionActions(unittest.TestCase):
    def _row(self, state="PROTECTED"):
        return dict(event_id="e" * 20, contract="DOZ_USDT", side="LONG",
                    timeframe="15m", position_mode="single", state=state,
                    contracts="10", entry_price="1", stop_loss="0.9",
                    take_profit="1.2", entry_order_id="111",
                    stop_order_id="222", take_order_id="333",
                    client_text="t-lxp-v")

    def test_close_sends_reduce_only(self):
        con = FakeCon([self._row()])
        client = FakeClient(positions=[{"contract": "DOZ_USDT", "size": "10",
                                        "entry_price": "1"}])
        with patch.object(actions, "_live_position",
                          side_effect=[({"contract": "DOZ_USDT", "size": "10",
                                         "entry_price": "1"},
                                        client.snapshot()), (None, client.snapshot())]):
            out = actions.close_position(con, "e" * 20, client)
        self.assertEqual(out["state"], "CLOSED_VERIFIED")
        self.assertEqual(out["orders_sent"], 1)
        sent = [p for path, p in client.posts if path.endswith("/orders")]
        self.assertTrue(sent[0]["reduce_only"])
        self.assertEqual(sent[0]["size"], -10)

    def test_close_refuses_fractional(self):
        con = FakeCon([self._row()])
        client = FakeClient()
        with patch.object(actions, "_live_position",
                          return_value=({"contract": "DOZ_USDT", "size": "1.5"},
                                        client.snapshot())):
            with self.assertRaises(LiveSafetyError):
                actions.close_position(con, "e" * 20, client)

    def test_reverse_blocked_while_halted(self):
        con = FakeCon([self._row()])
        client = FakeClient()
        with patch.object(actions, "_halted", return_value=True):
            with self.assertRaises(LiveSafetyError) as ctx:
                actions.reverse_position(con, "e" * 20, "0.8", "1.3", client)
        self.assertIn("halted", str(ctx.exception))
        self.assertEqual(client.posts, [])

    def test_protection_rejects_wrong_side_stop(self):
        con = FakeCon([self._row()])
        client = FakeClient()
        with patch.object(actions, "_live_position",
                          return_value=({"contract": "DOZ_USDT", "size": "10",
                                         "entry_price": "1"},
                                        client.snapshot())):
            with self.assertRaises(LiveSafetyError):
                # A LONG stop above entry is invalid geometry.
                actions.modify_protection(con, "e" * 20, "1.1", "1.2", client)


class TestRefusalReadback(unittest.TestCase):
    def test_refusal_table_is_read_by_the_panel(self):
        src = open("app.py", encoding="utf-8").read()
        self.assertIn("LEFT JOIN luxalgo_live_execution_refusals", src)
        self.assertIn("refusal_reason", src)

    def test_completed_trades_route_exists(self):
        src = open("app.py", encoding="utf-8").read()
        self.assertIn("@app.get('/api/completed-trades')", src)
        self.assertIn("read_completed_trades", src)

    def test_position_action_routes_exist(self):
        src = open("app.py", encoding="utf-8").read()
        for route in ("/terminal/position/close", "/terminal/position/protection",
                      "/terminal/position/reverse", "/terminal/live-positions"):
            self.assertIn(route, src, route)


if __name__ == "__main__":
    unittest.main()
