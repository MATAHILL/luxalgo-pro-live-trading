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
        self.assertIn('REAL MONEY ORDER SUBMISSION OFF',p)
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

if __name__=="__main__":unittest.main()
