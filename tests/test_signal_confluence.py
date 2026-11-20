"""Offline tests for the read-only LuxAlgo confluence quality gate."""
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import signal_confluence as sc


class ConfluenceTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.cfg = sc.ConfluenceConfig()
        now_ts = int(self.now.timestamp())
        self.ltf = self._candles(now_ts, 45, 300)
        self.htf = self._candles(now_ts, 35, 900)

    @staticmethod
    def _candles(now_ts, count, step):
        # Flat, low-range fixtures: tests data quality, not trading direction.
        return [
            {"t": now_ts - (count - i) * step, "o": "100", "h": "100.2",
             "l": "99.8", "c": "100"}
            for i in range(count)
        ]

    def _evaluate(self, **overrides):
        args = dict(
            symbol="XRPUSDT", side="LONG", timeframe="5m",
            entry=Decimal("100"), stop=Decimal("98"), take_profit=Decimal("104"),
            candles=self.ltf, htf_candles=self.htf, config=self.cfg,
            now=self.now, last_entry_epoch=None,
        )
        args.update(overrides)
        return sc.evaluate(**args)

    def test_valid_read_only_quality_pass(self):
        result = self._evaluate()
        self.assertEqual(result["decision"], "PASS")
        self.assertEqual(result["score"], 7)
        self.assertEqual(result["blockers"], [])

    def test_wrong_side_luxalgo_levels_block(self):
        result = self._evaluate(stop=Decimal("101"), take_profit=Decimal("104"))
        self.assertEqual(result["decision"], "BLOCK")
        self.assertIn("LUXALGO_LEVEL_GEOMETRY", result["blockers"])

    def test_stale_lower_timeframe_data_blocks(self):
        stale = self._candles(int((self.now - timedelta(hours=1)).timestamp()), 45, 300)
        result = self._evaluate(candles=stale)
        self.assertIn("LTF_DATA_FRESHNESS", result["blockers"])

    def test_insufficient_higher_timeframe_data_blocks(self):
        result = self._evaluate(htf_candles=self.htf[:10])
        self.assertIn("HTF_CANDLE_COUNT", result["blockers"])

    def test_entry_too_far_from_market_blocks(self):
        result = self._evaluate(entry=Decimal("110"), stop=Decimal("108"),
                                take_profit=Decimal("114"))
        self.assertIn("ENTRY_PRICE_PROXIMITY_AND_RANGE", result["blockers"])

    def test_recent_entry_cooldown_blocks(self):
        result = self._evaluate(last_entry_epoch=self.now.timestamp() - 30)
        self.assertIn("ENTRY_COOLDOWN", result["blockers"])

    def test_invalid_timeframe_blocks(self):
        result = self._evaluate(timeframe="1m")
        self.assertEqual(result["decision"], "BLOCK")

    def test_gate_candle_parser_rejects_bad_ohlc(self):
        self.assertIsNone(sc._candle({"t": 1, "o": "10", "h": "9",
                                      "l": "8", "c": "9"}))


if __name__ == "__main__":
    unittest.main()
