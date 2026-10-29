"""Production position charts: read-only manual positions can expand TradingView."""
import unittest
from app import PAGE
from production_cutover_view import PRODUCTION_CARDS, production_monitor_page

class ProductionLiveChartTests(unittest.TestCase):
    def test_chart_attached_to_production_cards_not_retired_testnet(self):
        out=production_monitor_page(PAGE)
        self.assertIn('id="productionPositionCards"',out)
        self.assertIn("node('button','v4-token production-token-toggle'",out)
        self.assertIn("title.addEventListener('click'",out)
        self.assertIn("widget=document.createElement('tv-advanced-chart')",out)
        self.assertIn("widget.setAttribute('symbol',market)",out)
        self.assertIn("widget.setAttribute('interval','5')",out)
        self.assertIn("'GATEIO:'+pair+'.P'",out)
        self.assertIn("mount.appendChild(section)",out)
        self.assertIn(".production-chart[hidden]{display:none!important}",out)
        self.assertNotIn('id="v4Workspace"',out)

    def test_refresh_preserves_embedded_chart_instances(self):
        self.assertIn("const old=new Map",PRODUCTION_CARDS)
        self.assertIn("if(!card){card=makeCard(p,key);cards.appendChild(card);}",PRODUCTION_CARDS)
        self.assertIn("updateCard(card,p,reconciled)",PRODUCTION_CARDS)
        self.assertNotIn("cards.replaceChildren();count.textContent",PRODUCTION_CARDS)

    def test_market_close_is_still_safe_and_exchange_link_is_explicit(self):
        self.assertIn("Market Close — LOCKED",PRODUCTION_CARDS)
        self.assertIn("b.disabled=true",PRODUCTION_CARDS)
        self.assertIn("Manage / Close on Gate.io",PRODUCTION_CARDS)
        self.assertIn("noopener noreferrer",PRODUCTION_CARDS)
        self.assertNotIn("method:'POST'",PRODUCTION_CARDS)

if __name__ == '__main__':
    unittest.main()
