"""Regression tests for the live-call symbol scope and refusal visibility.

These lock in the two defects that stopped a real LuxAlgo signal from ever
reaching an order:

1. ``signal_confluence.DEFAULT_ALLOWLIST`` was ``"XRPUSDT"``, so every ETH/SOL/
   DOGE alert was vetoed at veto 0 with ``SYMBOL_NOT_ALLOWED`` before scoring,
   before ``build_plan()`` and before any exchange call.
2. The trade-status panel only rendered live positions and closed trades, so a
   signal refused before any order produced no row at all and its refusal
   reason was never visible.

No network, no exchange, no database.
"""
import os
import unittest

import signal_confluence as sc
import testnet_coin_scope as scope


class NoAllowlistTests(unittest.TestCase):
    def test_default_allowlist_is_empty(self):
        """An empty allowlist means 'no restriction', never 'nothing'."""
        self.assertEqual(sc.DEFAULT_ALLOWLIST, "")

    def test_alt_symbols_permitted_by_default(self):
        for sym in ("XRPUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "ADAUSDT"):
            ok, reason = sc.symbol_permitted(sym)
            self.assertTrue(ok, sym)
            self.assertEqual(reason, "OK", sym)

    def test_tradingview_prefixed_symbols_permitted(self):
        for sym in ("GATEIO:ETHUSDT.P", "BINANCE:SOLUSDT.P", "GATEIO:XRPUSDT.P"):
            ok, reason = sc.symbol_permitted(sym)
            self.assertTrue(ok, sym)
            self.assertEqual(reason, "OK", sym)

    def test_btc_still_blocked(self):
        for sym in ("BTCUSDT", "BTCUSDT.P", "GATEIO:BTCUSDT.P", "BTCDOMUSDT"):
            ok, reason = sc.symbol_permitted(sym)
            self.assertFalse(ok, sym)
            self.assertEqual(reason, "BTC_BLOCKED", sym)

    def test_block_list_still_restricts(self):
        """LUXALGO_BLOCKED_SYMBOLS remains the only symbol restriction."""
        old = os.environ.get("LUXALGO_BLOCKED_SYMBOLS")
        try:
            os.environ["LUXALGO_BLOCKED_SYMBOLS"] = "BTCUSDT,ETHUSDT"
            cfg = sc.ConfluenceConfig.from_env()
            self.assertFalse(sc.symbol_permitted("ETHUSDT", cfg)[0])
            self.assertTrue(sc.symbol_permitted("SOLUSDT", cfg)[0])
        finally:
            if old is None:
                os.environ.pop("LUXALGO_BLOCKED_SYMBOLS", None)
            else:
                os.environ["LUXALGO_BLOCKED_SYMBOLS"] = old

    def test_allowlist_env_cannot_reintroduce_a_veto(self):
        """A stale LUXALGO_SYMBOL_ALLOWLIST must not block a symbol.

        The variable is still parsed for backward compatibility, but
        symbol_permitted no longer consults it.
        """
        old = os.environ.get("LUXALGO_SYMBOL_ALLOWLIST")
        try:
            os.environ["LUXALGO_SYMBOL_ALLOWLIST"] = "XRPUSDT"
            cfg = sc.ConfluenceConfig.from_env()
            self.assertTrue(sc.symbol_permitted("ETHUSDT", cfg)[0])
            self.assertTrue(sc.symbol_permitted("SOLUSDT", cfg)[0])
        finally:
            if old is None:
                os.environ.pop("LUXALGO_SYMBOL_ALLOWLIST", None)
            else:
                os.environ["LUXALGO_SYMBOL_ALLOWLIST"] = old

    def test_scope_module_agrees_with_confluence(self):
        for sym in ("XRPUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT"):
            self.assertTrue(scope.selected_webhook_symbol(sym), sym)
            self.assertTrue(sc.symbol_permitted(sym)[0], sym)
        self.assertFalse(scope.selected_webhook_symbol("BTCUSDT"))
        self.assertFalse(sc.symbol_permitted("BTCUSDT")[0])


class RefusalVisibilityTests(unittest.TestCase):
    """The panel must render a refused signal, not only live/closed rows."""

    @classmethod
    def setUpClass(cls):
        with open("app.py", encoding="utf-8") as fh:
            src = fh.read()
        start = src.index("PAGE = '''")
        end = src.index("'''", start + 10)
        cls.page = src[start:end]

    def test_panel_fetches_refusal_feed(self):
        self.assertIn("getJson('/terminal/incoming-signals?production=1')", self.page)

    def test_panel_renders_signal_only_rows(self):
        self.assertIn("for(const s of sigs)", self.page)
        self.assertIn("shown.has(s.event_id)", self.page)
        self.assertIn("whyByEvent[s.event_id]", self.page)

    def test_signal_row_carries_the_reason_column(self):
        self.assertIn("'Signal'", self.page)
        self.assertIn("status-signal", self.page)

    def test_refusal_column_is_last(self):
        body_at = self.page.index('<tbody id="signalStatusBody"')
        head = self.page[self.page.rindex("<thead>", 0, body_at):
                         self.page.rindex("</thead>", 0, body_at)]
        self.assertTrue(head.rstrip().endswith("<th>WHY NOT EXECUTED</th></tr>"))


if __name__ == "__main__":
    unittest.main()
