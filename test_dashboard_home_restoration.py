"""Browser routing regression tests for restoring the existing dashboard."""
import unittest
from unittest.mock import patch
from flask import Response
from app import app

class OriginalDashboardRoutingTests(unittest.TestCase):
    def setUp(self):
        self.ctx=patch.dict("os.environ",{"TERMINAL_PASSWORD":"Z"*32,
            "TERMINAL_USER":"admin","LIVE_TERMINAL_PREPARATION":"true",
            "LIVE_TRADING_ENABLED":"false","ORDERS_ENABLED":"false",
            "PRODUCTION_TERMINAL_VIEW":"false"})
        self.ctx.start()
        self.client=app.test_client()

    def tearDown(self):
        self.ctx.stop()

    def sign_in(self):
        with self.client.session_transaction() as session:
            session["authenticated"]=True
            session["user"]="admin"
            session["csrf_token"]="test-csrf-token"

    def test_home_unauthenticated_redirects_to_sign_in(self):
        r=self.client.get("/")
        self.assertEqual(r.status_code,303)
        self.assertIn("/login",r.headers["Location"])

    def test_old_link_unauthenticated_redirects_to_sign_in_not_json(self):
        r=self.client.get("/terminal/legacy-testnet?utm_source=chatgpt")
        self.assertEqual(r.status_code,303)
        self.assertIn("/login",r.headers["Location"])
        self.assertIn("legacy-testnet",r.headers["Location"])
        self.assertNotIn("application/json",r.content_type)

    def test_home_restores_original_dashboard_even_in_live_preparation_mode(self):
        self.sign_in()
        with patch("app._render_legacy_home",
                   return_value=Response("ORIGINAL DASHBOARD",mimetype="text/html")) as dashboard:
            r=self.client.get("/")
        self.assertEqual(r.status_code,200)
        self.assertIn(b"ORIGINAL DASHBOARD",r.data)
        dashboard.assert_called_once_with(production_view=False)

    def test_authenticated_legacy_bookmark_keeps_private_recovery_view(self):
        self.sign_in()
        with patch("app._render_legacy_home",
                   return_value=Response("PRIVATE RECOVERY",mimetype="text/html")) as legacy:
            r=self.client.get("/terminal/legacy-testnet")
        self.assertEqual(r.status_code,200)
        self.assertIn(b"PRIVATE RECOVERY",r.data)
        legacy.assert_called_once_with(production_view=False)

    def test_preparation_is_separate_and_has_no_live_order_controls(self):
        self.sign_in()
        r=self.client.get("/terminal/live-preparation")
        self.assertEqual(r.status_code,200)
        self.assertIn(b"REAL MONEY OFF",r.data)
        self.assertIn(b"Live Buy",r.data)
        self.assertIn(b"disabled",r.data)
        self.assertNotIn(b"/terminal/testnet-manual-close",r.data)
        self.assertEqual(r.headers["X-Frame-Options"],"DENY")

    def test_preparation_link_unauthenticated_is_browser_redirect(self):
        r=self.client.get("/terminal/live-preparation")
        self.assertEqual(r.status_code,303)
        self.assertIn("/login",r.headers["Location"])

if __name__=="__main__":
    unittest.main()
