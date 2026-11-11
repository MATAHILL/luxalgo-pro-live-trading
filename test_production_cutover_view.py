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

    # --- v6: the panel must reflect LIVE state, never stale text -------------

    def test_gate_sync_hook_exists_and_reads_server_gates(self):
        """The panel re-renders from safety_gates instead of a frozen message."""
        self.assertIn('window.__luxGateSync',self.page)
        self.assertIn("gates.indexOf('EMERGENCY_HALT')",self.page)
        self.assertIn("gates.indexOf('ACCEPTANCE_AND_FINAL_AUTHORIZATION_MISSING')",self.page)

    def test_gate_sync_is_called_from_the_state_poll(self):
        """Every BOT ON/OFF poll feeds the gate panel, so it cannot go stale."""
        self.assertIn("window.__luxGateSync(state);",self.page)

    def test_stale_next_step_message_is_replaced_when_armed(self):
        """The old 'Next step: press BOT ON' must not survive an armed bot."""
        self.assertIn("if(armed){",self.page)
        self.assertIn('all gates open',self.page)

    def test_relatched_halt_is_reported_in_plain_words(self):
        self.assertIn('HALT RE-LATCHED',self.page)
        self.assertIn('UNPROTECTED_OR_UNKNOWN_EXCHANGE_EXPOSURE',self.page)
        self.assertIn('EXCHANGE_RECONCILIATION_FAILED',self.page)

    def test_buttons_disable_once_their_gate_is_open(self):
        """A finished step must not keep inviting another click."""
        self.assertIn('haltBtn.disabled=!halted;',self.page)
        self.assertIn('certOpen.disabled=!needsCert;',self.page)
        self.assertIn('HALT CLEARED \\u2713',self.page)
        self.assertIn('CERTIFIED \\u2713',self.page)

    def test_refusal_message_is_never_overwritten(self):
        """A refusal must stay on screen, not be replaced by a calm status line."""
        self.assertIn('refused',self.page)
        self.assertIn('NOT CLEARED',self.page)

    def test_halt_reason_is_explained_in_words(self):
        self.assertIn('HALT because ',self.page)
        self.assertIn('halt_reason',self.page)


class ProductionStateDisclosureTests(unittest.TestCase):
    """state() must disclose WHY the halt is on and when the cert expires."""

    def test_state_exposes_halt_reason_and_certified_until(self):
        import inspect as _inspect
        from production_bot_control import state
        src=_inspect.getsource(state)
        self.assertIn('halt_reason',src)
        self.assertIn('certified_until',src)
        self.assertIn('operator_reason',src)

    def test_state_does_not_relax_any_gate(self):
        """Disclosure only: the gate list logic must be untouched."""
        import inspect as _inspect
        from production_bot_control import state
        src=_inspect.getsource(state)
        for gate in ('SOURCE_LOCKED','ENVIRONMENT_LOCKED','EMERGENCY_HALT',
                     'ACCEPTANCE_AND_FINAL_AUTHORIZATION_MISSING','OPERATOR_OFF'):
            self.assertIn(gate,src)
        self.assertIn('effective=not gates',src)


if __name__=="__main__":unittest.main()
