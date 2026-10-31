"""XRP fractional exchange minimums: whole-contract plans only, no real orders."""
import unittest
from decimal import Decimal
from live_futures_engine import (
    build_plan, build_entry_order, LiveSafetyError, EXCHANGE_WRITE_RELEASE,
)

NOW = 1800000000
SIGNAL = {
    "event_id": "tradingview-XRP-5m-20261031",
    "contract": "XRP_USDT",
    "side": "LONG",
    "timeframe": "5m",
    "levels_status": "VALID",
    "entry": "2.5",
    "sl": "2.4",
    "tp": "2.7",
    "leverage": "5",
    "position_mode": "dual",
    "received_epoch": NOW - 10,
}
META = {
    "name": "XRP_USDT",
    "quanto_multiplier": "1",
    "order_size_min": "0.1",
    "order_size_max": "1.5",
    "taker_fee_rate": "0.00075",
    "in_delisting": False,
}


def plan(meta=None, equity="100", available="100"):
    return build_plan(SIGNAL, meta if meta is not None else META,
                      equity, available, [], now=NOW)


class FractionalMinimumWholeLotTests(unittest.TestCase):
    def test_decimal_minimum_allows_one_whole_contract_with_decimal_maximum(self):
        result = plan()
        self.assertEqual(result["contracts"], 1)
        self.assertEqual(build_entry_order(result)["size"], 1)
        self.assertEqual(result["contract"], "XRP_USDT")
        self.assertLessEqual(Decimal(result["estimated_risk_usdt"]), Decimal("0.5"))
        self.assertFalse(result["verified_live_order"])
        self.assertFalse(EXCHANGE_WRITE_RELEASE)

    def test_never_rounds_exchange_maximum_up(self):
        meta = dict(META, order_size_max="4.5")
        result = plan(meta)
        self.assertEqual(result["contracts"], 4)
        self.assertLessEqual(result["contracts"], Decimal("4.5"))

    def test_incompatible_fractional_granularity_fail_closed(self):
        for minimum, maximum in (("0.3", "10"), ("1.1", "10"),
                                 ("0.1", "0.9"), ("0", "10"),
                                 ("0.1", "0.0"), ("NaN", "10")):
            with self.subTest(minimum=minimum, maximum=maximum):
                with self.assertRaises(LiveSafetyError):
                    plan(dict(META, order_size_min=minimum, order_size_max=maximum))

    def test_insufficient_risk_and_margin_never_force_one_lot(self):
        for equity, available in (("1", "100"), ("100", "0.1")):
            with self.subTest(equity=equity, available=available):
                with self.assertRaises(LiveSafetyError):
                    plan(equity=equity, available=available)

    def test_no_test_authorization_implied_from_sizing(self):
        result = plan()
        self.assertFalse(EXCHANGE_WRITE_RELEASE)
        self.assertNotIn("authorization", result)
        self.assertEqual(result["verified_live_order"], False)


if __name__ == "__main__":
    unittest.main()
