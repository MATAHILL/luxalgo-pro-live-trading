"""Production BOT ON/OFF safety tests; all DB and HTTP effects mocked."""
import os
import unittest
from contextlib import nullcontext
from datetime import datetime,timezone,timedelta
from unittest.mock import patch,Mock
import app as backend
import production_bot_control as prod
from production_cutover_view import production_monitor_page

class ProductionBotTests(unittest.TestCase):
    def test_no_source_unlock_can_enable_bot(self):
        op=dict(operator_on=True,reason="old requested ON",
                changed_at=datetime.now(timezone.utc))
        certified=dict(exchange="GATE_IO_LIVE_USDT_FUTURES",
                acceptance_verified=True,final_operator_authorized=True,
                approved_until=datetime.now(timezone.utc)+timedelta(hours=1),
                evidence_sha="evidence-only",signed_approval_id="approval")
        with (patch.object(prod,"install"),patch.object(prod,"control",return_value=dict(halted=False)),
              patch.object(prod,"_read_row",side_effect=[op,certified]),
              patch.dict(os.environ,{"LIVE_TRADING_ENABLED":"true","ORDERS_ENABLED":"true",
                                      "LIVE_ORDERS_SEPARATELY_AUTHORIZED":"true"})):
            status=prod.state(object())
        self.assertFalse(status["bot_on"])
        self.assertIn("SOURCE_LOCKED",status["safety_gates"])
        self.assertFalse(status["orders_enabled"])

    def test_missing_certification_and_active_halt_block(self):
        with (patch.object(prod,"install"),
              patch.object(prod,"control",return_value=dict(halted=True)),
              patch.object(prod,"_read_row",side_effect=[
                dict(operator_on=False,reason="off",changed_at=None),None])):
            s=prod.state(object())
        self.assertIn("EMERGENCY_HALT",s["safety_gates"])
        self.assertIn("ACCEPTANCE_AND_FINAL_AUTHORIZATION_MISSING",s["safety_gates"])
        self.assertFalse(s["can_arm"])

    def test_operator_off_is_transactional_and_halts_entries(self):
        conn=Mock()
        cursor=Mock()
        cursor.rowcount=1
        conn.cursor.return_value.__enter__=Mock(return_value=cursor)
        conn.cursor.return_value.__exit__=Mock(return_value=False)
        with (patch.object(prod,"install"),
              patch.object(prod,"halt",return_value=dict(halted=True)) as emergency,
              patch.object(prod,"state",return_value=dict(
                  bot_on=False,orders_enabled=False,halt_active=True))):
            status=prod.turn_off(conn)
        self.assertFalse(status["bot_on"])
        emergency.assert_called_once_with(conn,"OPERATOR_OFF_NO_NEW_ENTRIES")
        statements=[x.args[0] for x in cursor.execute.call_args_list]
        self.assertTrue(any("SET operator_on=FALSE" in sql for sql in statements))
        self.assertTrue(any("pg_advisory_xact_lock" in sql for sql in statements))

    def test_dashboard_restores_two_buttons_and_both_indicators_above_radar(self):
        html=production_monitor_page(backend.PAGE)
        self.assertEqual(html.count('id="productionBotOn"'),1)
        self.assertEqual(html.count('id="productionBotOff"'),1)
        self.assertEqual(html.count('id="productionBotIndicator"'),1)
        self.assertIn('● BOT OFF',html)
        self.assertIn('LIVE MARKET FEED',html)
        self.assertNotIn('id="productionBotSwitch"',html)
        # Historical Testnet labels remain in hidden legacy source markup.
        # Verify the two visible production components, not archival HTML.
        from production_cutover_view import PRODUCTION_CONTROL, PRODUCTION_BANNER
        self.assertNotIn('EXECUTION OFF',PRODUCTION_CONTROL)
        self.assertNotIn('LIVE ORDERS OFF',PRODUCTION_BANNER)
        self.assertLess(html.index('id="productionBotOn"'),
                        html.index('GATE.IO FUTURES RADAR <span'))
        self.assertLess(html.index('id="productionBotOff"'),
                        html.index('GATE.IO FUTURES RADAR <span'))
        for x in ("radarBody","scannerBody","advancedScanBody","newsStack","productionPositionCards"):
            self.assertIn('id="'+x+'"',html)
        self.assertIn("'/terminal/production-bot-control'",html)

    def test_authenticated_control_endpoints_and_fail_closed_on(self):
        with patch.dict(os.environ,{"TERMINAL_PASSWORD":"x"*28,"TERMINAL_USER":"admin"}):
            client=backend.app.test_client()
            r=client.get("/terminal/production-bot-control")
            self.assertEqual(r.status_code,401)
            with client.session_transaction() as sess:
                sess["authenticated"]=True;sess["user"]="admin";sess["csrf_token"]="test-csrf"
            r=client.post("/terminal/production-bot-control",json={"enabled":True})
            self.assertEqual(r.status_code,403)
            with (patch.object(backend,"_db_connect",return_value=nullcontext(object())),
                  patch.object(prod,"turn_on",return_value=dict(bot_on=False,
                    can_arm=False,reason="SOURCE_LOCKED",orders_enabled=False)) as on):
                result=client.post("/terminal/production-bot-control",
                    headers={"X-Terminal-CSRF":"test-csrf"},json={"enabled":True})
                self.assertEqual(result.status_code,409)
                self.assertFalse(result.json["bot_on"])
                on.assert_called_once()
            with (patch.object(backend,"_db_connect",return_value=nullcontext(object())),
                  patch.object(prod,"turn_off",return_value=dict(bot_on=False,
                      reason="OPERATOR_OFF",orders_enabled=False)) as off):
                result=client.post("/terminal/production-bot-control",
                    headers={"X-Terminal-CSRF":"test-csrf"},json={"enabled":False})
                self.assertEqual(result.status_code,200)
                self.assertFalse(result.json["bot_on"])
                off.assert_called_once()

    def test_live_order_inventory_replaces_expired_btc_acceptance_panel(self):
        html=production_monitor_page(backend.PAGE)
        self.assertEqual(html.count('id="productionOrdersPanel"'),1)
        self.assertEqual(html.count('id="productionOrdersRefresh"'),1)
        self.assertLess(html.index('id="productionBotOff"'),
                        html.index('id="productionOrdersPanel"'))
        self.assertLess(html.index('id="productionOrdersPanel"'),
                        html.index('GATE.IO FUTURES RADAR <span'))
        self.assertIn("OPEN EXCHANGE ORDERS",html)
        self.assertIn("PROTECTIVE TRIGGERS",html)
        self.assertIn("0 · EMPTY",html)
        self.assertIn("UNVERIFIED",html)
        self.assertIn("This is NOT an empty-order confirmation",html)
        self.assertIn("'/terminal/live-execution/snapshot'",html)
        self.assertNotIn('id="btcAcceptancePanel"',html)
        self.assertNotIn("BTC_USDT · ONE-CONTRACT LIVE ACCEPTANCE TEST",html)
        self.assertNotIn("'/terminal/acceptance-test/status'",html)
        self.assertEqual(html.count('id="productionBotOn"'),1)

    def test_acceptance_status_endpoint_auth_and_zero_order_evidence(self):
        with patch.dict(os.environ,{"TERMINAL_PASSWORD":"x"*28,"TERMINAL_USER":"admin"}):
            client=backend.app.test_client()
            self.assertEqual(client.get("/terminal/acceptance-test/status").status_code,401)
            with client.session_transaction() as sess:
                sess["authenticated"]=True;sess["user"]="admin";sess["csrf_token"]="test-csrf"
            con=Mock()
            cursor=Mock()
            cursor.fetchone.side_effect=[None,None,dict(halted=True)]
            con.cursor.return_value.__enter__=Mock(return_value=cursor)
            con.cursor.return_value.__exit__=Mock(return_value=False)
            with patch.object(backend,"_db_connect",return_value=nullcontext(con)):
                res=client.get("/terminal/acceptance-test/status")
            self.assertEqual(res.status_code,200)
            self.assertEqual(res.json["status"],"NO_BOT_TEST_ORDER_RECORDED")
            self.assertFalse(res.json["bot_order_recorded"])
            self.assertFalse(res.json["eligible_btc_long_signal_seen"])
            self.assertFalse(res.json["exchange_fill_verified"])
            self.assertFalse(res.json["native_sl_tp_verified"])
            self.assertTrue(res.json["persistent_emergency_halt"])
            self.assertFalse(res.json["can_execute"])
            self.assertEqual(cursor.execute.call_count,3)

    def test_acceptance_ledger_does_not_falsely_certify_a_fill(self):
        with patch.dict(os.environ,{"TERMINAL_PASSWORD":"x"*28,"TERMINAL_USER":"admin"}):
            client=backend.app.test_client()
            with client.session_transaction() as sess:
                sess["authenticated"]=True;sess["user"]="admin";sess["csrf_token"]="test-csrf"
            con=Mock();cursor=Mock()
            row=dict(event_id="f"*20,state="ENTRY_SUBMITTED",contracts=1,
                     entry_order_id="exchange-order-123",
                     stop_order_id=None,take_order_id=None,
                     created_at=datetime.now(timezone.utc),
                     updated_at=datetime.now(timezone.utc))
            alert=dict(event_id="f"*20,
                       received_at=datetime.now(timezone.utc),
                       source_bar_id="TVBAR_5m_00005977339",
                       order_type="MARKET",level_status="VALID")
            cursor.fetchone.side_effect=[row,alert,dict(halted=True)]
            con.cursor.return_value.__enter__=Mock(return_value=cursor)
            con.cursor.return_value.__exit__=Mock(return_value=False)
            with patch.object(backend,"_db_connect",return_value=nullcontext(con)):
                res=client.get("/terminal/acceptance-test/status")
            self.assertEqual(res.status_code,200)
            self.assertTrue(res.json["bot_order_recorded"])
            self.assertTrue(res.json["eligible_btc_long_signal_seen"])
            self.assertEqual(res.json["status"],"BOT_LEDGER_PRESENT_EXCHANGE_READBACK_REQUIRED")
            self.assertFalse(res.json["exchange_fill_verified"])
            self.assertFalse(res.json["native_sl_tp_verified"])
            self.assertFalse(res.json["can_execute"])


    def test_live_order_inventory_is_authenticated_bounded_fail_closed(self):
        html=production_monitor_page(backend.PAGE)
        self.assertIn("AbortController()",html)
        self.assertIn("credentials:'same-origin',cache:'no-store'",html)
        self.assertIn("'/terminal/live-execution/snapshot'",html)
        self.assertIn("setTimeout(()=>controller.abort(),12000)",html)
        self.assertIn("snapshot.exchange_open_orders",html)
        self.assertIn("snapshot.exchange_open_triggers",html)
        self.assertIn("p!==snapshot.positions.length",html)
        self.assertIn("orders.textContent='UNVERIFIED'",html)
        self.assertIn("triggers.textContent='UNVERIFIED'",html)
        self.assertNotIn('id="btcAcceptanceCaps"',html)
        self.assertNotIn("fetch('/terminal/acceptance-test/status'",html)

    def test_ready_requires_production_safety_schema_not_only_old_testnet_tables(self):
        client=backend.app.test_client()
        con=Mock();cur=Mock()
        con.cursor.return_value.__enter__=Mock(return_value=cur)
        con.cursor.return_value.__exit__=Mock(return_value=False)
        good=dict(receipts=True,intents=True,control=True,halt=True,
                  live_handoff=True,signal_levels=True,prod_halt=True,
                  prod_ledger=True,prod_operator=True,prod_certification=True)
        with patch.object(backend,"_db_connect",return_value=nullcontext(con)):
            cur.fetchone.return_value=dict(good,prod_halt=False)
            failed=client.get("/ready")
            self.assertEqual(failed.status_code,503)
            self.assertFalse(failed.json["orders_enabled"])
            self.assertFalse(failed.json["live_trading"])
            cur.fetchone.return_value=good
            passed=client.get("/ready")
            self.assertEqual(passed.status_code,200)
            self.assertFalse(passed.json["orders_enabled"])
            self.assertFalse(passed.json["live_trading"])
        sql=cur.execute.call_args.args[0]
        for marker in ("luxalgo_prod_execution_control","luxalgo_prod_execution_ledger",
                       "luxalgo_prod_bot_operator_control","luxalgo_prod_activation_certification"):
            self.assertIn(marker,sql)

if __name__=="__main__":unittest.main()
