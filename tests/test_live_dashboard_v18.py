"""v18 tests: the seven dashboard development prompts.

Each test names the prompt it covers. Everything here is offline: no exchange
write, no live order, no state change. Where an exchange read is needed, a fake
transport returns a fixed Gate.io-shaped payload.
"""
import json
import os
import unittest
from contextlib import nullcontext
from decimal import Decimal
from unittest.mock import Mock, patch

os.environ.setdefault("TERMINAL_PASSWORD", "test-passphrase-allowing-strong-auth")
os.environ.setdefault("TERMINAL_USER", "admin")

import app as backend
from live_analytics import (aggregate_fills, compute_analytics, liquidity_summary,
                            signal_execution_state)


def _client():
    c = backend.app.test_client()
    with c.session_transaction() as s:
        s["authenticated"] = True
        s["user"] = "admin"
        s["csrf_token"] = "safe"
    return c


def _con(rows):
    """A psycopg-shaped connection returning fixed ledger rows."""
    con = Mock()
    cur = Mock()
    cur.fetchall.return_value = rows
    con.cursor.return_value.__enter__ = Mock(return_value=cur)
    con.cursor.return_value.__exit__ = Mock(return_value=False)
    return con


def _row(**kw):
    base = dict(event_id="e1", contract="DOZ_USDT", side="LONG", state="CLOSED_VERIFIED",
                contracts=Decimal("1000"), entry_price=Decimal("0.0052"),
                stop_loss=Decimal("0.0050"), take_profit=Decimal("0.0058"),
                entry_order_id="111", detail={}, created_at=None, updated_at=None)
    base.update(kw)
    return base


class Prompt2LiquidityBoxes(unittest.TestCase):
    """Prompt 2: six summary boxes show correct values, no placeholders."""

    def test_realized_pnl_and_win_rate_are_computed(self):
        rows = [_row(event_id="a", detail={"net_realized_pnl_usdt": "2.35"}),
                _row(event_id="b", detail={"net_realized_pnl_usdt": "-1.10"})]
        a = compute_analytics(_con(rows), client=None)
        self.assertEqual(a["total_closed_trades"], 2)
        self.assertEqual(a["winning_trades"], 1)
        self.assertEqual(a["losing_trades"], 1)
        self.assertEqual(a["win_rate_pct"], 50.0)
        self.assertAlmostEqual(a["total_realized_pnl_usdt"], 1.25, places=6)

    def test_duplicate_rows_are_counted_once(self):
        # The same event_id twice must not double count.
        rows = [_row(event_id="dup", detail={"net_realized_pnl_usdt": "5"}),
                _row(event_id="dup", detail={"net_realized_pnl_usdt": "5"})]
        a = compute_analytics(_con(rows), client=None)
        self.assertEqual(a["total_closed_trades"], 1)
        self.assertAlmostEqual(a["total_realized_pnl_usdt"], 5.0, places=6)

    def test_unknown_pnl_is_none_not_zero(self):
        rows = [_row(event_id="x", detail={})]
        a = compute_analytics(_con(rows), client=None)
        self.assertIsNone(a["total_realized_pnl_usdt"])
        self.assertEqual(a["realized_pnl_trades_unknown"], 1)

    def test_liquidity_summary_exposes_all_six(self):
        rows = [_row(event_id="a", detail={"net_realized_pnl_usdt": "3"})]
        s = liquidity_summary(_con(rows), client=None, snapshot={"account": {}, "positions": []})
        for key in ("equity_usdt", "available_usdt", "balance_usdt",
                    "total_realized_pnl_usdt", "win_rate_pct", "unrealized_pnl_usdt"):
            self.assertIn(key, s)
        self.assertAlmostEqual(s["total_realized_pnl_usdt"], 3.0, places=6)

    def test_route_registered_and_guarded(self):
        rules = {str(r) for r in backend.app.url_map.iter_rules()}
        self.assertIn("/terminal/futures-liquidity-summary", rules)
        anon = backend.app.test_client()
        self.assertIn(anon.get("/terminal/futures-liquidity-summary").status_code, (302, 401, 403))


class Prompt3OldSectionRemoved(unittest.TestCase):
    """Prompt 3: the old OPEN POSITIONS · GATE.IO FUTURES section is gone."""

    def test_old_heading_absent(self):
        html = backend.PAGE
        self.assertNotIn('OPEN POSITIONS <span class="gloss-cyan">· GATE.IO FUTURES</span>', html)

    def test_new_heading_present(self):
        html = backend.PAGE
        self.assertIn("Open Position", html)
        self.assertIn("Gate.io Futures", html)


class Prompt4PositionCard(unittest.TestCase):
    """Prompt 4: renamed card, per-card refresh, glossy P&L, 3 per row."""

    def test_card_renamed_and_refresh_present(self):
        html = backend.PAGE
        self.assertIn("position-card-refresh", html)
        self.assertIn("/terminal/position/refresh", html)
        self.assertIn("Reverse Position", html)
        self.assertIn("Market Close", html)
        self.assertIn("Set TP/SL", html)

    def test_three_per_row_css(self):
        html = backend.PAGE
        self.assertIn("grid-template-columns:repeat(3,minmax(0,1fr))", html)

    def test_glossy_pnl_classes(self):
        html = backend.PAGE
        self.assertIn("gloss-pnl-profit", html)
        self.assertIn("gloss-pnl-loss", html)
        self.assertIn("gloss-pnl-neutral", html)

    def test_refresh_rejects_bad_contract(self):
        r = _client().get("/terminal/position/refresh?contract=NOT_A_CONTRACT")
        self.assertEqual(r.status_code, 400)

    def test_refresh_route_registered(self):
        rules = {str(r) for r in backend.app.url_map.iter_rules()}
        self.assertIn("/terminal/position/refresh", rules)


class Prompt5OrderTable(unittest.TestCase):
    """Prompt 5: order table shows orders, triggers and positions; errors are explicit."""

    def test_panel_present(self):
        html = backend.PAGE
        self.assertIn("LIVE FUTURES ORDER TABLE", html)
        self.assertIn("orderTableBody", html)
        self.assertIn("PROTECTIVE TRIGGERS (SL/TP)", html)

    def test_route_registered(self):
        rules = {str(r) for r in backend.app.url_map.iter_rules()}
        self.assertIn("/terminal/exchange-orders", rules)

    def test_missing_credentials_is_error_not_empty_success(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GATE_API_KEY", None)
            os.environ.pop("GATE_API_SECRET", None)
            r = _client().get("/terminal/exchange-orders")
        self.assertEqual(r.status_code, 503)
        self.assertIn("error", r.json)
        self.assertNotEqual(r.status_code, 200)

    def test_orders_and_triggers_are_returned(self):
        orders = [{"contract": "DOGE_USDT", "id_string": "9001", "size": "100",
                   "left": "100", "price": "0.0648", "status": "open",
                   "create_time": "1763000000"}]
        triggers = [{"initial": {"contract": "DOGE_USDT", "size": "100"},
                     "order_type": "close-long-position", "id_string": "9002",
                     "trigger": {"price": "0.0630", "rule": 2}, "status": "open",
                     "create_time": "1763000001"}]
        fake = Mock()
        fake.request.side_effect = lambda m, p, **kw: (
            orders if p.endswith("/orders") else triggers)
        with patch("live_futures_engine.GateLiveClient", return_value=fake), \
             patch.object(backend, "_gate_read_only_positions", return_value=[]):
            r = _client().get("/terminal/exchange-orders")
        self.assertEqual(r.status_code, 200)
        d = r.json
        self.assertEqual(d["order_count"], 1)
        self.assertEqual(d["trigger_count"], 1)
        self.assertEqual(d["protective_trigger_count"], 1)
        self.assertEqual(d["orders"][0]["order_id"], "9001")
        self.assertTrue(d["triggers"][0]["protective"])


class Prompt6SignalsAndAnalytics(unittest.TestCase):
    """Prompt 6: truthful signal states; analytics repaired."""

    def test_filled_signal_is_not_labelled_not_executed(self):
        state, label, detail = signal_execution_state(
            {"state": "PROTECTED", "entry_order_id": "555"})
        self.assertEqual(state, "EXECUTED")
        self.assertEqual(label, "EXECUTED")
        self.assertIn("555", detail)

    def test_no_ledger_row_is_pending(self):
        state, label, _ = signal_execution_state(None)
        self.assertEqual(state, "PENDING")
        self.assertEqual(label, "PENDING")

    def test_refusal_is_rejected_with_reason(self):
        state, label, detail = signal_execution_state(
            None, "Persistent live execution HALT", "ORDER_SENDER")
        self.assertEqual(state, "REJECTED")
        self.assertIn("Persistent live execution HALT", detail)

    def test_partial_protection_is_partially_filled(self):
        state, label, _ = signal_execution_state({"state": "PROTECTION_PENDING"})
        self.assertEqual(state, "PARTIALLY_FILLED")

    def test_analytics_route_registered(self):
        rules = {str(r) for r in backend.app.url_map.iter_rules()}
        self.assertIn("/terminal/performance-analytics", rules)

    def test_analytics_panel_has_all_metrics(self):
        html = backend.PAGE
        for probe in ("perfClosed", "perfWins", "perfLosses", "perfWinRate",
                      "perfLosingRate", "perfCloseRate", "perfRealized", "perfUnrealized"):
            self.assertIn(probe, html)

    def test_close_rate_uses_executed_open(self):
        rows = [_row(event_id="c1", detail={"net_realized_pnl_usdt": "1"}),
                _row(event_id="o1", state="PROTECTED", detail={})]
        a = compute_analytics(_con(rows), client=None)
        self.assertEqual(a["total_closed_trades"], 1)
        self.assertEqual(a["open_executed_trades"], 1)
        self.assertEqual(a["total_close_rate_pct"], 50.0)


class Prompt7ExitRecord(unittest.TestCase):
    """Prompt 7: exit fill, quantity, exit time, fees and net P&L."""

    def test_partial_closes_are_summed_and_deduped(self):
        fills = [
            {"id": "t1", "order_id": "999", "pnl": "1.00", "fee": "-0.05",
             "size": "500", "price": "0.0056", "create_time": "1763000000"},
            {"id": "t2", "order_id": "999", "pnl": "1.35", "fee": "-0.05",
             "size": "500", "price": "0.00576", "create_time": "1763000100"},
            # A re-read of the same window must not double count.
            {"id": "t1", "order_id": "999", "pnl": "1.00", "fee": "-0.05",
             "size": "500", "price": "0.0056", "create_time": "1763000000"},
        ]
        agg = aggregate_fills(fills, entry_order_id="111")
        self.assertEqual(agg["fill_count"], 2)
        self.assertAlmostEqual(float(agg["pnl"]), 2.35, places=6)
        self.assertAlmostEqual(float(agg["fees"]), -0.10, places=6)
        self.assertAlmostEqual(float(agg["net"]), 2.25, places=6)
        self.assertAlmostEqual(float(agg["qty"]), 1000.0, places=6)
        self.assertEqual(agg["last_ts"], 1763000100)

    def test_entry_fill_is_excluded(self):
        fills = [{"id": "e", "order_id": "111", "pnl": "0", "fee": "-0.02",
                  "size": "1000", "price": "0.0052", "create_time": "1762999000"},
                 {"id": "c", "order_id": "999", "pnl": "2.00", "fee": "-0.10",
                  "size": "1000", "price": "0.0057", "create_time": "1763000000"}]
        agg = aggregate_fills(fills, entry_order_id="111")
        self.assertEqual(agg["fill_count"], 1)
        self.assertAlmostEqual(float(agg["net"]), 1.90, places=6)

    def test_no_fills_is_none(self):
        self.assertIsNone(aggregate_fills([], entry_order_id="111"))
        self.assertIsNone(aggregate_fills(None))

    def test_completed_trades_exposes_exit_fields(self):
        from live_close_record import read_completed_trades
        row = _row(detail={"net_realized_pnl_usdt": "2.35", "fees_usdt": "-0.20",
                           "exit_fill_price": "0.00568", "exit_quantity": "1000",
                           "exit_timestamp": 1763000000, "closing_fill_count": 2,
                           "realized_pnl_source": "GATE_MY_TRADES"})
        out = read_completed_trades(_con([row]))
        self.assertEqual(len(out), 1)
        t = out[0]
        self.assertEqual(t["exit_price"], "0.00568")
        self.assertEqual(t["exit_quantity"], "1000")
        self.assertAlmostEqual(t["net_realized_pnl_usdt"], 2.35, places=6)
        self.assertAlmostEqual(t["fees_usdt"], -0.20, places=6)
        self.assertIsNotNone(t["exit_timestamp"])

    def test_table_has_exit_columns(self):
        html = backend.PAGE
        for probe in ("EXIT FILL", "EXIT QTY", "EXIT TIME (IST)", "FEES",
                      "NET REALIZED P&amp;L"):
            self.assertIn(probe, html)


class SafetyUnchanged(unittest.TestCase):
    """No gate was relaxed and no write path was added by these changes."""

    def test_new_routes_are_read_only(self):
        import inspect
        for fn in (backend.terminal_futures_liquidity_summary,
                   backend.terminal_performance_analytics,
                   backend.terminal_exchange_orders,
                   backend.terminal_position_refresh):
            src = inspect.getsource(fn)
            for forbidden in ("submit_entry(", "cancel_trigger(", "turn_on(",
                              "certify(", "clear_halt(", "halt("):
                self.assertNotIn(forbidden, src, fn.__name__)

    def test_analytics_module_never_writes(self):
        import inspect
        import live_analytics
        src = inspect.getsource(live_analytics)
        for forbidden in ("INSERT ", "UPDATE ", "DELETE ", "submit_entry"):
            self.assertNotIn(forbidden, src)


if __name__ == "__main__":
    unittest.main()
