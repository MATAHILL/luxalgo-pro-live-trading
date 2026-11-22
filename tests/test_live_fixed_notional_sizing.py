"""Fixed-notional order sizing: switchable, floored, and cap-preserving.

All tests are pure/mocked: no network, credentials, or live order submission.
"""
import os
import unittest
from decimal import Decimal
from unittest.mock import patch, MagicMock

from live_risk_policy import (
    sizing_mode, fixed_notional_usdt, per_entry_risk, policy_status,
    SIZING_MODE_RISK, SIZING_MODE_FIXED, MAX_FIXED_NOTIONAL_USDT,
)
from live_futures_engine import (
    build_plan, verify_execution_preflight, LiveSafetyError,
    MAX_POSITION_NOTIONAL_USDT, MAX_POSITION_INITIAL_MARGIN_USDT,
)
from live_execution_coordinator import risk_reserve

NOW = 1800000000
SIGNAL = {
    "event_id": "tv-fixed-notional-5m",
    "contract": "XRP_USDT",
    "side": "LONG",
    "timeframe": "5m",
    "levels_status": "VALID",
    "entry": "2.5",
    "sl": "2.45",
    "tp": "2.6",
    "leverage": "5",
    "position_mode": "single",
    "received_epoch": NOW - 1,
}
META = {
    "name": "XRP_USDT", "quanto_multiplier": "10",
    "order_size_min": "1", "order_size_max": "100000",
    "taker_fee_rate": "0.00075",
}


def fixed_env(**extra):
    env = {"LUXALGO_LIVE_SIZING_MODE": "fixed_notional",
           "LUXALGO_LIVE_FIXED_NOTIONAL_USDT": "100"}
    env.update(extra)
    return patch.dict(os.environ, env)


class SizingModeSwitchTests(unittest.TestCase):
    def test_default_mode_is_risk_percent(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LUXALGO_LIVE_SIZING_MODE", None)
            self.assertEqual(sizing_mode(), SIZING_MODE_RISK)

    def test_mode_aliases_and_unknown_value_fails_closed(self):
        for value in ("fixed_notional", "fixed", "notional", "FIXED_NOTIONAL"):
            with self.subTest(value=value), patch.dict(
                    os.environ, LUXALGO_LIVE_SIZING_MODE=value):
                self.assertEqual(sizing_mode(), SIZING_MODE_FIXED)
        for value in ("risk_percent", "risk", "percent", ""):
            with self.subTest(value=value), patch.dict(
                    os.environ, LUXALGO_LIVE_SIZING_MODE=value):
                self.assertEqual(sizing_mode(), SIZING_MODE_RISK)
        with patch.dict(os.environ, LUXALGO_LIVE_SIZING_MODE="yolo"):
            with self.assertRaises(ValueError):
                sizing_mode()

    def test_fixed_notional_config_fails_closed(self):
        # MAX_FIXED_NOTIONAL_USDT is None (switched off), so a value above the
        # old 100 ceiling is now accepted. Malformed values still fail closed.
        for value in ("", "NaN", "Infinity", "-1", "0", "true"):
            with self.subTest(value=value), patch.dict(
                    os.environ, LUXALGO_LIVE_FIXED_NOTIONAL_USDT=value):
                with self.assertRaises(ValueError):
                    fixed_notional_usdt()
        with patch.dict(os.environ, LUXALGO_LIVE_FIXED_NOTIONAL_USDT="25000"):
            self.assertEqual(fixed_notional_usdt(), Decimal("25000"))

    def test_policy_status_reports_active_mode(self):
        with fixed_env():
            status = policy_status()
            self.assertEqual(status["sizing_mode"], SIZING_MODE_FIXED)
            self.assertEqual(status["fixed_notional_usdt"], "100")
            self.assertEqual(status["per_trade_risk_percent"], "not_applied")
            self.assertEqual(status["aggregate_risk_percent"], "2.5")
        with patch.dict(os.environ, LUXALGO_LIVE_SIZING_MODE="risk_percent"):
            self.assertEqual(policy_status()["sizing_mode"], SIZING_MODE_RISK)


class FixedNotionalSizingTests(unittest.TestCase):
    def test_contracts_are_notional_over_price_times_multiplier_floored(self):
        with fixed_env():
            plan = build_plan(SIGNAL, META, "1000", "1000", [], now=NOW)
        # 100 / (2.5 * 10) = 4.0 exactly
        self.assertEqual(plan["contracts"], 4)
        self.assertEqual(plan["sizing_mode"], SIZING_MODE_FIXED)
        self.assertEqual(plan["target_notional_usdt"], "100")
        self.assertEqual(Decimal(plan["notional_usdt"]) if "notional_usdt" in plan
                         else Decimal(plan["contracts"]) * Decimal("2.5") * Decimal("10"),
                         Decimal("100"))

    def test_flooring_never_rounds_up_past_target(self):
        signal = dict(SIGNAL, entry="2.53")
        with fixed_env():
            plan = build_plan(signal, META, "1000", "1000", [], now=NOW)
        # 100 / 25.3 = 3.95 -> 3 contracts, notional 75.9 <= 100
        self.assertEqual(plan["contracts"], 3)
        self.assertLessEqual(Decimal(plan["contracts"]) * Decimal("2.53") * Decimal("10"),
                             Decimal("100"))

    def test_short_side_is_negative_and_same_notional(self):
        with fixed_env():
            plan = build_plan(dict(SIGNAL, side="SHORT", sl="2.55", tp="2.4"),
                              META, "1000", "1000", [], now=NOW)
        self.assertEqual(plan["contracts"], 4)
        self.assertEqual(plan["side"], "SHORT")

    def test_notional_ceiling_switched_off_allows_large_size(self):
        # MAX_POSITION_NOTIONAL_USDT is None (switched off), so a large target
        # is no longer clipped to 100 USDT. The aggregate risk budget and the
        # available margin are what bound the order now.
        self.assertIsNone(MAX_POSITION_NOTIONAL_USDT)
        with fixed_env(LUXALGO_LIVE_FIXED_NOTIONAL_USDT="20000"):
            plan = build_plan(SIGNAL, META, "100000", "100000", [], now=NOW)
        self.assertEqual(plan["maximum_notional_usdt"], "unlimited")
        self.assertGreater(Decimal(plan["contracts"]) * Decimal("2.5") * Decimal("10"),
                           Decimal("100"))

    def test_margin_ceiling_switched_off_allows_large_size(self):
        # MAX_POSITION_INITIAL_MARGIN_USDT is None (switched off), so the
        # 60-USDT margin ceiling no longer clips the order.
        self.assertIsNone(MAX_POSITION_INITIAL_MARGIN_USDT)
        with fixed_env(LUXALGO_LIVE_FIXED_NOTIONAL_USDT="20000"):
            plan = build_plan(SIGNAL, META, "100000", "100000", [], now=NOW)
        self.assertEqual(plan["maximum_initial_margin_usdt"], "unlimited")
        margin = (Decimal(plan["contracts"]) * Decimal("2.5") * Decimal("10")
                  * (Decimal(1) / Decimal(5) + Decimal("0.00075")))
        self.assertGreater(margin, Decimal("60"))

    def test_insufficient_available_margin_fails_closed(self):
        with fixed_env():
            with self.assertRaisesRegex(LiveSafetyError, "Insufficient margin/risk"):
                build_plan(SIGNAL, META, "1000", "0.01", [], now=NOW)

    def test_position_cap_still_enforced(self):
        positions = [dict(contract=f"C{i}_USDT", size="1") for i in range(5)]
        with fixed_env():
            with self.assertRaisesRegex(LiveSafetyError, "Maximum five"):
                build_plan(SIGNAL, META, "1000", "1000", positions, now=NOW)

    def test_leverage_ceiling_still_enforced(self):
        with fixed_env():
            with self.assertRaisesRegex(LiveSafetyError, "Leverage exceeds"):
                build_plan(dict(SIGNAL, leverage="6"), META, "1000", "1000", [], now=NOW)

    def test_signal_age_still_enforced(self):
        with fixed_env():
            with self.assertRaisesRegex(LiveSafetyError, "delayed or future"):
                build_plan(dict(SIGNAL, received_epoch=NOW - 600), META,
                           "1000", "1000", [], now=NOW)

    def test_aggregate_open_risk_still_enforced(self):
        with fixed_env():
            with self.assertRaisesRegex(LiveSafetyError, "Aggregate risk"):
                build_plan(SIGNAL, META, "1000", "1000", [], reserved_risk="25", now=NOW)

    def test_risk_percent_mode_unchanged(self):
        with patch.dict(os.environ, LUXALGO_LIVE_SIZING_MODE="risk_percent"):
            plan = build_plan(SIGNAL, META, "1000", "1000", [], now=NOW)
        self.assertEqual(plan["sizing_mode"], SIZING_MODE_RISK)
        self.assertIsNone(plan["target_notional_usdt"])
        self.assertLessEqual(Decimal(plan["estimated_risk_usdt"]),
                             Decimal("1000") * per_entry_risk())


class FixedNotionalPreflightTests(unittest.TestCase):
    def _meta(self):
        return dict(META, status="trading", in_delisting=False,
                    market_order_size_max=100000, order_price_round="0.01",
                    leverage_min="1", leverage_max="100", mark_price="2.5")

    def _snap(self, available="1000"):
        return dict(account=dict(currency="USDT", total="1000", unrealised_pnl="0",
                                 available=available, position_mode="single"),
                    position_settings=[dict(contract="XRP_USDT", mode="single",
                                            leverage="5")])

    def test_fixed_notional_plan_passes_preflight(self):
        with fixed_env():
            plan = build_plan(SIGNAL, META, "1000", "1000", [], now=NOW)
            result = verify_execution_preflight(plan, self._meta(), self._snap(), now=NOW)
        self.assertTrue(result["verified"])
        self.assertEqual(result["orders_sent"], 0)

    def test_preflight_still_rejects_oversize_plan(self):
        # With both ceilings switched off, the preflight no longer rejects on
        # notional/margin. It still rejects when the fresh margin exceeds what
        # the account actually has available -- that is the real bound now.
        plan = dict(received_epoch=NOW - 1, contract="XRP_USDT", contracts=4000,
                    side="LONG", entry="2.5", sl="2.45", tp="2.6",
                    position_mode="single", leverage="5",
                    max_entry_slippage_ratio="0.001", estimated_risk_usdt="100")
        with fixed_env():
            with self.assertRaisesRegex(LiveSafetyError, "Insufficient fresh margin"):
                verify_execution_preflight(plan, self._meta(), self._snap(), now=NOW)


class FixedNotionalCoordinatorTests(unittest.TestCase):
    def _con(self):
        con = MagicMock()
        cur = con.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = dict(halted=False)
        cur.fetchall.return_value = []
        return con, cur

    def test_fixed_mode_skips_per_entry_risk_cap_but_keeps_aggregate(self):
        con, cur = self._con()
        with fixed_env():
            risk_reserve(con, dict(contract="XRP_USDT", estimated_risk_usdt="20",
                                   event_id="e1", client_text="t1", side="LONG",
                                   timeframe="5m", position_mode="single",
                                   entry="2.5", sl="2.45", tp="2.6", contracts=4),
                         [], "1000")
        self.assertTrue(any("INSERT INTO" in str(c) for c in cur.execute.call_args_list))

    def test_fixed_mode_still_blocks_aggregate_risk(self):
        con, cur = self._con()
        with fixed_env():
            with self.assertRaisesRegex(LiveSafetyError, "account-wide"):
                risk_reserve(con, dict(contract="XRP_USDT", estimated_risk_usdt="26",
                                       event_id="e2", client_text="t2", side="LONG",
                                       timeframe="5m", position_mode="single",
                                       entry="2.5", sl="2.45", tp="2.6", contracts=4),
                             [], "1000")
        self.assertFalse(any("INSERT INTO" in str(c) for c in cur.execute.call_args_list))

    def test_risk_mode_still_blocks_per_entry_cap(self):
        con, cur = self._con()
        with patch.dict(os.environ, LUXALGO_LIVE_SIZING_MODE="risk_percent"):
            with self.assertRaisesRegex(LiveSafetyError, "per-entry"):
                risk_reserve(con, dict(contract="XRP_USDT", estimated_risk_usdt="20",
                                       event_id="e3", client_text="t3", side="LONG",
                                       timeframe="5m", position_mode="single",
                                       entry="2.5", sl="2.45", tp="2.6", contracts=4),
                             [], "1000")


if __name__ == "__main__":
    unittest.main()
