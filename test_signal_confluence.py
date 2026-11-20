"""Offline tests for the XRP-only confluence gate. No network, no exchange."""
import math
import unittest
from datetime import datetime, timezone, timedelta

import signal_confluence as sc
import testnet_coin_scope as scope

NOW = datetime(2026, 11, 16, 12, 0, 0, tzinfo=timezone.utc)


def make_bars(n=120, start=1.0, step=0.001, tf="15m", end_at=NOW, vol=100.0):
    """Deterministic trending bars ending on the last COMPLETED candle."""
    secs = sc.TF_SECONDS[tf]
    last_open = int(end_at.timestamp()) - secs
    bars = []
    price = start
    for i in range(n):
        t = last_open - (n - 1 - i) * secs
        o = price
        c = price + step
        h = max(o, c) + 0.0004
        l = min(o, c) - 0.0004
        bars.append({"t": t, "o": o, "h": h, "l": l, "c": c, "v": vol})
        price = c
    return bars


def cfg(**kw):
    base = dict(min_score=0, min_rr=0.0, min_stop_atr=0.0, max_stop_atr=99.0,
                min_atr_pct=0.0, max_atr_pct=99.0, cooldown_minutes=0,
                require_htf=False, max_chase_atr=99.0, min_volume_ratio=0.0,
                max_candle_age=99)
    base.update(kw)
    return sc.ConfluenceConfig(**base)


class SymbolScopeTests(unittest.TestCase):
    def test_btc_blocked_even_if_allowlisted(self):
        c = sc.ConfluenceConfig(allowlist=frozenset({"XRPUSDT", "BTCUSDT"}))
        ok, reason = sc.symbol_permitted("BTCUSDT", c)
        self.assertFalse(ok)
        self.assertEqual(reason, "BTC_BLOCKED")

    def test_btc_prefixed_variants_blocked(self):
        for sym in ("BTCUSDT", "BTCUSDT.P", "GATEIO:BTCUSDT.P", "BTCDOMUSDT"):
            ok, reason = sc.symbol_permitted(sym)
            self.assertFalse(ok, sym)
            self.assertEqual(reason, "BTC_BLOCKED", sym)

    def test_xrp_allowed(self):
        for sym in ("XRPUSDT", "GATEIO:XRPUSDT.P", "BINANCE:XRPUSDT.P"):
            ok, _ = sc.symbol_permitted(sym)
            self.assertTrue(ok, sym)

    def test_other_alts_not_allowed(self):
        for sym in ("ETHUSDT", "SOLUSDT", "DOGEUSDT"):
            ok, reason = sc.symbol_permitted(sym)
            self.assertFalse(ok, sym)
            self.assertEqual(reason, "SYMBOL_NOT_ALLOWED", sym)

    def test_coin_scope_module_agrees(self):
        self.assertTrue(scope.selected_webhook_symbol("XRPUSDT"))
        self.assertTrue(scope.selected_webhook_symbol("GATEIO:XRPUSDT.P"))
        self.assertFalse(scope.selected_webhook_symbol("BTCUSDT"))
        self.assertFalse(scope.selected_webhook_symbol("ETHUSDT"))
        self.assertTrue(scope.selected_testnet_contract("XRP_USDT"))
        self.assertFalse(scope.selected_testnet_contract("BTC_USDT"))


class GateTests(unittest.TestCase):
    def _eval(self, **over):
        bars = over.pop("bars", make_bars())
        htf = over.pop("htf", None)
        # Fall back to a fixed reference price when no candles are supplied.
        ref = bars[-1]["c"] if bars else 1.0
        kw = dict(symbol="XRPUSDT", side="LONG", timeframe="15m",
                  entry=ref, stop=ref - 0.01, take_profit=ref + 0.03,
                  candles=bars, htf_candles=htf, config=cfg(), now=NOW)
        kw.update(over)
        return sc.evaluate(**kw)

    def test_btc_never_passes(self):
        d = self._eval(symbol="BTCUSDT")
        self.assertEqual(d["decision"], "BLOCK")
        self.assertIn("BTC_BLOCKED", d["blockers"])

    def test_geometry_inverted_blocks(self):
        bars = make_bars()
        d = self._eval(bars=bars, stop=bars[-1]["c"] + 0.01,
                       take_profit=bars[-1]["c"] + 0.03)
        self.assertEqual(d["decision"], "BLOCK")
        self.assertIn("GEOMETRY_INVALID", d["blockers"])

    def test_missing_candles_blocks(self):
        d = self._eval(bars=[])
        self.assertEqual(d["decision"], "BLOCK")
        self.assertIn("INSUFFICIENT_CANDLES", d["blockers"])

    def test_stale_candles_block(self):
        old = make_bars(end_at=NOW - timedelta(hours=6))
        d = self._eval(bars=old, config=cfg(max_candle_age=2))
        self.assertEqual(d["decision"], "BLOCK")
        self.assertIn("STALE_CANDLES", d["blockers"])

    def test_rr_below_min_blocks(self):
        bars = make_bars()
        d = self._eval(bars=bars, stop=bars[-1]["c"] - 0.01,
                       take_profit=bars[-1]["c"] + 0.011,
                       config=cfg(min_rr=1.5))
        self.assertEqual(d["decision"], "BLOCK")
        self.assertIn("RR_BELOW_MIN", d["blockers"])

    def test_htf_conflict_blocks_long(self):
        bars = make_bars()
        htf = make_bars(n=120, step=-0.001, tf="1h")
        d = self._eval(bars=bars, htf=htf, config=cfg(require_htf=True))
        self.assertEqual(d["decision"], "BLOCK")
        self.assertIn("HTF_CONFLICT", d["blockers"])

    def test_htf_missing_blocks_when_required(self):
        d = self._eval(config=cfg(require_htf=True))
        self.assertEqual(d["decision"], "BLOCK")
        self.assertIn("HTF_DATA_UNAVAILABLE", d["blockers"])

    def test_cooldown_blocks(self):
        d = self._eval(config=cfg(cooldown_minutes=30),
                       last_entry_epoch=NOW.timestamp() - 300)
        self.assertEqual(d["decision"], "BLOCK")
        self.assertIn("COOLDOWN_ACTIVE", d["blockers"])

    def test_chase_extension_blocks(self):
        bars = make_bars(step=0.02)  # steep run away from the mean
        d = self._eval(bars=bars, config=cfg(max_chase_atr=1.0))
        self.assertEqual(d["decision"], "BLOCK")
        self.assertIn("CHASE_EXTENSION", d["blockers"])

    def test_clean_aligned_signal_passes(self):
        bars = make_bars()
        htf = make_bars(n=120, step=0.002, tf="1h")
        d = self._eval(bars=bars, htf=htf, config=cfg(require_htf=True))
        self.assertEqual(d["decision"], "PASS", d["blockers"])
        self.assertGreater(d["score"], 0)

    def test_score_below_min_blocks(self):
        bars = make_bars()
        d = self._eval(bars=bars, config=cfg(min_score=101))
        self.assertEqual(d["decision"], "BLOCK")
        self.assertIn("SCORE_BELOW_MIN", d["blockers"])

    def test_never_raises_on_garbage(self):
        for bad in (None, "x", 123, {"a": 1}):
            d = sc.evaluate(symbol="XRPUSDT", side="LONG", timeframe="15m",
                            entry=bad, stop=bad, take_profit=bad,
                            candles=bad, config=cfg(), now=NOW)
            self.assertEqual(d["decision"], "BLOCK")

    def test_short_geometry(self):
        bars = make_bars(step=-0.001)
        d = self._eval(bars=bars, side="SHORT", entry=bars[-1]["c"],
                       stop=bars[-1]["c"] + 0.01,
                       take_profit=bars[-1]["c"] - 0.03)
        self.assertEqual(d["decision"], "PASS", d["blockers"])


class CandleNormalizationTests(unittest.TestCase):
    def test_in_progress_bar_dropped(self):
        secs = 900
        now = NOW
        raw = [
            {"t": str(int(now.timestamp()) - 2 * secs), "o": "1", "h": "1.1",
             "l": "0.9", "c": "1.05", "v": "10"},
            {"t": str(int(now.timestamp()) - secs), "o": "1", "h": "1.1",
             "l": "0.9", "c": "1.05", "v": "10"},
            {"t": str(int(now.timestamp())), "o": "1", "h": "1.1",
             "l": "0.9", "c": "1.05", "v": "10"},  # forming
        ]
        bars = sc.normalize_candles(raw, "15m", now)
        self.assertEqual(len(bars), 2)

    def test_bad_rows_skipped(self):
        raw = [{"t": "x"}, {"t": "1", "o": "1", "h": "0.5", "l": "2", "c": "1"}]
        self.assertEqual(sc.normalize_candles(raw, "15m", NOW), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
