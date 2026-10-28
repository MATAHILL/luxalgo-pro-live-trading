import unittest
from unittest.mock import patch
from live_preparation_terminal import (
    live_preparation_mode,live_preparation_checks,LIVE_PREPARATION_PAGE
)
class ProductionPreparationTests(unittest.TestCase):
    def test_preparation_flag_off_by_default(self):
        with patch.dict("os.environ",{"LIVE_TERMINAL_PREPARATION":"false"}):
            self.assertFalse(live_preparation_mode())
        with patch.dict("os.environ",{"LIVE_TERMINAL_PREPARATION":"true"}):
            self.assertTrue(live_preparation_mode())
    def test_cannot_mark_ready_even_if_account_and_db_work(self):
        result=live_preparation_checks(
            account_ok=True,positions_ok=True,db_ok=True,webhook_ok=True,
            legacy_count=0,dynamic_sizing_preview_ready=True)
        self.assertEqual(result["status"],"PREPARATION_ONLY")
        self.assertFalse(result["safe_to_trade"])
        self.assertFalse(result["orders_enabled"])
        self.assertFalse(result["live_trading"])
        self.assertEqual(result["orders_submitted"],0)
        self.assertTrue(any(not r["passed"] for r in result["checks"]))
    def test_live_flags_requested_never_turn_on_orders(self):
        r=live_preparation_checks(
            account_ok=True,positions_ok=True,db_ok=True,webhook_ok=True,
            legacy_count=3,prod_write_requested=True)
        self.assertTrue(r["real_money_write_requested"])
        self.assertFalse(r["orders_enabled"])
        self.assertFalse(r["safe_to_trade"])
        blockers={check["name"]:check["passed"] for check in r["checks"]}
        self.assertFalse(blockers["Dedicated production sender and order readbacks"])
        self.assertFalse(blockers["Real-money native SL/TP plus filled-order recovery"])
        self.assertFalse(blockers["Production live execution qualification"])
    def test_missing_legacy_readback_never_certifies_live_execution(self):
        # Retained Testnet recovery is now a separate archival concern.
        # The live market-feed check may PASS even when execution remains OFF.
        r=live_preparation_checks(
            account_ok=True,positions_ok=True,db_ok=True,webhook_ok=True,
            legacy_count=None)
        self.assertEqual(r["status"],"PREPARATION_ONLY")
        self.assertFalse(r["safe_to_trade"])
        self.assertFalse(r["orders_enabled"])
        blockers={check["name"]:check["passed"] for check in r["checks"]}
        self.assertFalse(blockers["Production live execution qualification"])
        self.assertFalse(blockers["Atomic live risk budget / emergency close / kill switch"])
        self.assertTrue(blockers["Production market feeds independent of order execution"])
    def test_page_has_no_execution_endpoints_or_enabled_buy_button(self):
        self.assertIn("REAL MONEY OFF",LIVE_PREPARATION_PAGE)
        self.assertIn("Live Buy — Locked",LIVE_PREPARATION_PAGE)
        self.assertIn('button disabled',LIVE_PREPARATION_PAGE)
        self.assertNotIn("POST /api/v4/futures",LIVE_PREPARATION_PAGE)
        self.assertNotIn("/terminal/testnet-manual-close",LIVE_PREPARATION_PAGE)
        self.assertIn("/terminal/live-preparation/status",LIVE_PREPARATION_PAGE)
        self.assertIn("legacy-testnet",LIVE_PREPARATION_PAGE)
if __name__=="__main__":unittest.main()
