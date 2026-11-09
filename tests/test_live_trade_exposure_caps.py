"""Production exposure ceilings and TradingView exchange ticker normalization.

All tests are pure/mocked: no network, credentials, or live order submission.
"""
import unittest
from decimal import Decimal

from live_futures_engine import (
    build_plan, verify_execution_preflight, LiveSafetyError,
    MAX_POSITION_NOTIONAL_USDT, MAX_POSITION_INITIAL_MARGIN_USDT,
)
from live_signal_identity import normalize_luxalgo_symbol


NOW = 1800000000
SIGNAL = {
    "event_id": "tradingview-BTC-5m-max-exposure",
    "contract": "BTC_USDT",
    "side": "LONG",
    "timeframe": "5m",
    "levels_status": "VALID",
    "entry": "80000",
    "sl": "79999",
    "tp": "80100",
    "leverage": "5",
    "position_mode": "single",
    "received_epoch": NOW - 1,
}
META = {
    "name": "BTC_USDT", "quanto_multiplier": "0.0001",
    "order_size_min": "1", "order_size_max": "100000",
    "taker_fee_rate": "0.00075",
}


class LiveRiskAndSourceTests(unittest.TestCase):
    def test_trade_size_is_bounded_even_with_large_available_balance(self):
        p = build_plan(SIGNAL, META, "100000", "100000", [], now=NOW)
        q = Decimal(p["contracts"])
        one_notional = Decimal(SIGNAL["entry"]) * Decimal(META["quanto_multiplier"])
        fee = Decimal(META["taker_fee_rate"])
        slip = Decimal(p["max_entry_slippage_ratio"])
        self.assertLessEqual(q * one_notional * (1 + slip), MAX_POSITION_NOTIONAL_USDT)
        self.assertLessEqual(
            q * one_notional * (1 + slip) * (Decimal(1) / Decimal(5) + fee),
            MAX_POSITION_INITIAL_MARGIN_USDT)
        self.assertEqual(p["maximum_notional_usdt"], "300")
        self.assertEqual(p["maximum_initial_margin_usdt"], "60")
        self.assertFalse(p["verified_live_order"])

    def test_exchange_minimum_above_owner_exposure_cap_fails_closed(self):
        meta = dict(META, quanto_multiplier="0.01")
        with self.assertRaisesRegex(LiveSafetyError, "Insufficient margin/risk"):
            build_plan(SIGNAL, meta, "100000", "100000", [], now=NOW)

    def test_fresh_readback_rejects_manual_oversize_plan(self):
        # A constructed plan cannot bypass the live preflight risk boundaries.
        p = dict(received_epoch=1000, contract="BTC_USDT", contracts=40,
                 side="LONG", entry="80000", sl="79000", tp="82000",
                 position_mode="single", leverage="5",
                 max_entry_slippage_ratio="0.001", estimated_risk_usdt="100")
        meta = dict(META, status="trading", in_delisting=False,
                    market_order_size_max=100, order_price_round="0.1",
                    leverage_min="1", leverage_max="100", mark_price="80000")
        snap = dict(account=dict(currency="USDT", position_mode="single",
                                 total="100000", unrealised_pnl="0", available="10000"),
                    position_settings=[dict(contract="BTC_USDT", mode="single",
                                            leverage="5")])
        with self.assertRaisesRegex(LiveSafetyError, "notional or margin ceiling"):
            verify_execution_preflight(p, meta, snap, now=1001)

    def test_only_allowlisted_exchange_symbols_are_accepted(self):
        for ticker in ("GATEIO:XRPUSDT.P", "BINANCE:XRPUSDT.P",
                       "XRPUSDT.P", "XRPUSDT", "gateio:xrpusdt.p"):
            with self.subTest(ticker=ticker):
                self.assertEqual(normalize_luxalgo_symbol(ticker), "XRPUSDT")
        for ticker in ("BYBIT:XRPUSDT.P", "GATEIO:XRPBTC.P",
                       "GATEIO:XRPUSDT.B", "GATEIO:BTCUSDT:X",
                       "FAKE:BTCUSDT", None, 23, "BTCUSDT/USDT"):
            with self.subTest(ticker=ticker):
                self.assertIsNone(normalize_luxalgo_symbol(ticker))


if __name__ == "__main__":
    unittest.main()
