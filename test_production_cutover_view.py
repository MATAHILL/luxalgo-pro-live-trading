"""No Testnet order-entry controls on the main production dashboard."""
import unittest
from unittest.mock import patch
from flask import Response
from app import PAGE,app
from production_cutover_view import production_monitor_page

class ProductionCutoverTests(unittest.TestCase):
    def test_original_terminal_layout_kept_and_testnet_panels_removed(self):
        p=production_monitor_page(PAGE)
        self.assertIn('class="production-view"',p)
        self.assertIn('LUXALGO PRO · LIVE FUTURES TERMINAL',p)
        for part in ('id="incomingLuxPanel"','id="radarBody"','id="scannerBody"',
                     'id="productionCardsPanel"','id="productionPositionCards"',
                     'id="futuresEquity"','id="positionRefresh"'):
            self.assertIn(part,p,part)
        for retired in ('id="v4Workspace"','id="testnetReadinessPanel"',
                        'id="gateTestnetPositionPanel"','id="testnetOrdersPanel"',
                        'id="botTurnOn"','id="botTurnOff"'):
            self.assertNotIn(retired,p,retired)
        self.assertIn("fetch('/terminal/incoming-signals?production=1'",p)
        # The live BOT switch reports current state; never assert a retired static banner.
        # Keep explicit OFF, exchange-proof and read-only status UI verifiable.
        self.assertIn('id="productionBotIndicator"',p)
        self.assertIn('● BOT OFF',p)
        self.assertIn('LIVE FUTURES ORDERS',p)
        self.assertIn("const url='/terminal/production-bot-control'",p)
        self.assertIn("fetch('/terminal/live-execution/snapshot'",p)
        self.assertNotIn('id="btcAcceptancePanel"',p)
        self.assertNotIn('BTC_USDT · ONE-CONTRACT LIVE ACCEPTANCE TEST',p)
        self.assertIn('Market Close — LOCKED',p)
        self.assertIn('STOP LOSS',''.join(p))
        self.assertNotIn('<h1>LUXALGO PRO · TESTNET FUTURES TERMINAL</h1>',p)
        # No production exchange POST or any user-facing live execution authorization.
        self.assertNotIn("fetch('/api/v4/futures/usdt/orders'",p)

    def test_fail_closed_on_changed_dashboard_markup(self):
        with self.assertRaises(ValueError):
            production_monitor_page(PAGE.replace('id="v4Workspace"','id="changed-card-area"'))

    def test_production_home_is_original_template_with_live_view(self):
        with patch.dict("os.environ",{
            "TERMINAL_PASSWORD":"x"*32,"TERMINAL_USER":"admin",
            "PRODUCTION_TERMINAL_VIEW":"true",
            "LIVE_TRADING_ENABLED":"false","ORDERS_ENABLED":"false"}):
            client=app.test_client()
            with client.session_transaction() as session:
                session['authenticated']=True
                session['user']='admin'
                session['csrf_token']='test'
            with patch("app._render_legacy_home",
                       return_value=Response("PRODUCTION DASHBOARD",mimetype="text/html")) as fn:
                response=client.get("/")
            self.assertEqual(response.status_code,200)
            fn.assert_called_once_with(production_view=True)

    def test_old_testnet_bot_cannot_be_rearmed_after_production_cutover(self):
        with patch.dict("os.environ",{
            "TERMINAL_PASSWORD":"x"*32,"TERMINAL_USER":"admin",
            "PRODUCTION_TERMINAL_VIEW":"true",
            "LIVE_TRADING_ENABLED":"false","ORDERS_ENABLED":"false"}):
            client=app.test_client()
            with client.session_transaction() as session:
                session['authenticated']=True
                session['user']='admin'
                session['csrf_token']='test'
            response=client.post("/terminal/bot-control",json={"enabled":True})
            self.assertEqual(response.status_code,409)
            data=response.get_json()
            self.assertFalse(data['orders_enabled'])

class GateUnlockControlTests(unittest.TestCase):
    """CLEAR HALT / CERTIFY buttons exist and call the real endpoints."""

    def setUp(self):
        self.page=production_monitor_page(PAGE)

    def test_both_buttons_render_in_the_bot_control_panel(self):
        for part in ('id="productionHaltClear"','id="productionCertifyOpen"',
                     'id="productionCertifyForm"','id="productionEvidenceSha"',
                     'id="productionApprovalId"','id="productionValidHours"',
                     'id="productionCertifySubmit"','id="productionGateResult"'):
            self.assertIn(part,self.page,part)
        # The new controls live inside the same panel as BOT ON / BOT OFF.
        panel=self.page.split('class="bot-control-panel"',1)[1].split('</section>',1)[0]
        for part in ('id="productionBotOn"','id="productionBotOff"',
                     'id="productionHaltClear"','id="productionCertifyOpen"'):
            self.assertIn(part,panel,part)

    def test_buttons_call_the_same_endpoints_with_csrf(self):
        self.assertIn("postJson('/api/live/halt-clear',{})",self.page)
        self.assertIn("postJson('/api/live/certify'",self.page)
        self.assertIn("'X-Terminal-CSRF':csrf",self.page)
        for field in ('evidence_sha','signed_approval_id','valid_hours'):
            self.assertIn(field,self.page,field)

    def test_certify_form_defaults_are_valid(self):
        import re
        ev=re.search(r'id="productionEvidenceSha"[^>]*value="([^"]*)"',self.page).group(1)
        ap=re.search(r'id="productionApprovalId"[^>]*value="([^"]*)"',self.page).group(1)
        hr=re.search(r'id="productionValidHours"[^>]*value="(\d+)"',self.page).group(1)
        self.assertRegex(ev,r'^[A-Za-z0-9_.:-]{8,128}$')
        self.assertRegex(ap,r'^[A-Za-z0-9_.:-]{4,128}$')
        self.assertTrue(1<=int(hr)<=168)

    def test_valid_hours_input_is_bounded_1_to_168(self):
        self.assertIn('min="1" max="168"',self.page)

    def test_existing_bot_switch_and_gate_list_unchanged(self):
        self.assertIn("const url='/terminal/production-bot-control'",self.page)
        self.assertIn('id="productionBotIndicator"',self.page)
        self.assertIn('● BOT OFF',self.page)
        # No new order-sending path was introduced by the buttons.
        self.assertNotIn("fetch('/api/v4/futures/usdt/orders'",self.page)
        self.assertNotIn('/api/live/disarm',self.page)

    def test_buttons_do_not_bypass_server_validation(self):
        # The page must not send a fabricated success or skip the server call.
        self.assertNotIn('halted:false',self.page.replace('r.data.halted===false',''))
        self.assertIn('r.data.cleared===false',self.page)


if __name__=="__main__":unittest.main()
