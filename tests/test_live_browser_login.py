"""Human-facing login recovery remains separate from JSON/API authentication.

All database and exchange operations are mocked. No order-writing permissions.
"""
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
import app as backend


class BrowserLiveActivationTests(unittest.TestCase):
    def test_anonymous_browser_navigation_redirects_to_signin(self):
        c=backend.app.test_client()
        with patch.dict(os.environ,{"TERMINAL_PASSWORD":"x"*30,"TERMINAL_USER":"admin"}):
            res=c.get("/terminal/live-execution/unlock-readiness",
                      headers={"Accept":"text/html,application/xhtml+xml,application/json;q=0.7"})
            self.assertEqual(res.status_code,303)
            self.assertIn("/login?next=",res.headers["Location"])
            self.assertIn("unlock-readiness",res.headers["Location"])
            self.assertEqual(c.get("/live-activation").status_code,303)
            self.assertIn("/login?next=/live-activation",
                          c.get("/live-activation").headers["Location"])
            login=c.get("/login?next=/live-activation")
            self.assertEqual(login.status_code,200)
            self.assertIn(b"LUXALGO PRO",login.data)

    def test_existing_json_clients_get_401_not_html(self):
        c=backend.app.test_client()
        with patch.dict(os.environ,{"TERMINAL_PASSWORD":"x"*30,"TERMINAL_USER":"admin"}):
            for accept in (None,"application/json","*/*"):
                with self.subTest(accept=accept):
                    headers={"Accept":accept} if accept is not None else {}
                    r=c.get("/terminal/live-execution/unlock-readiness",headers=headers)
                    self.assertEqual(r.status_code,401)
                    self.assertEqual(r.get_json()["login_url"],"/login")
            r=c.post("/terminal/production-bot-control",
                     json={"enabled":True},headers={"Accept":"text/html"})
            self.assertEqual(r.status_code,401)
            self.assertEqual(r.get_json()["login_url"],"/login")

    def test_logged_in_page_is_read_only_and_shows_actual_state(self):
        c=backend.app.test_client()
        with patch.dict(os.environ,{"TERMINAL_PASSWORD":"x"*30,"TERMINAL_USER":"admin"}):
            with c.session_transaction() as sess:
                sess["authenticated"]=True
                sess["user"]="admin"
                sess["csrf_token"]="example-only"
            with patch.object(backend,"_db_connect") as db, \
                 patch("production_bot_control.state") as state:
                state.return_value={
                    "operator_requested_on":False,"orders_enabled":False,
                    "certified":False,"halt_active":True,
                    "safety_gates":["SOURCE_LOCKED","EMERGENCY_HALT"]
                }
                cursor=db.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
                cursor.fetchone.return_value={"latest":datetime(2026,11,3,tzinfo=timezone.utc)}
                res=c.get("/live-activation")
            self.assertEqual(res.status_code,200)
            self.assertIn(b"text/html",res.headers["Content-Type"].encode())
            self.assertIn(b"Live activation blocked",res.data)
            self.assertIn(b"EMERGENCY_HALT",res.data)
            self.assertIn(b"2026-11-03",res.data)
            self.assertEqual(res.headers["X-Frame-Options"],"DENY")
            self.assertEqual(res.headers["Cache-Control"],"private, no-store")
            for marker in (b"Open trading dashboard",b"View detailed readiness data"):
                self.assertIn(marker,res.data)

    def test_database_unavailable_never_claims_armed(self):
        c=backend.app.test_client()
        with patch.dict(os.environ,{"TERMINAL_PASSWORD":"x"*30,"TERMINAL_USER":"admin"}):
            with c.session_transaction() as sess:
                sess["authenticated"]=True
                sess["user"]="admin"
            with patch.object(backend,"_db_connect",side_effect=RuntimeError("offline")):
                res=c.get("/live-activation")
            self.assertEqual(res.status_code,200)
            self.assertIn(b"treat live trading as OFF",res.data)
            self.assertNotIn(b"Operator reports orders enabled",res.data)


if __name__=="__main__":
    unittest.main()
