"""v20: card buttons, per-card chart, order-table serial, panel order.

These tests pin the four corrections made on top of v19:

1. Every card's action buttons actually work. The old handler bailed out with
   ``if(!eventId)return;`` so any position without a bot ledger row had five
   dead buttons and no message. The close path also tried to invent a ledger
   row, which violated CHECK(risk_usdt>0)/CHECK(contracts>0) and returned 409.
2. A down-arrow beside Market Close opens a real TradingView chart below the
   card; clicking outside closes it.
3. The Live Futures Order Table has a serial-number column in front.
4. The Advanced Opportunity Scanner sits ABOVE the Gate.io Futures Radar.
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read()
LPA = open(os.path.join(ROOT, 'live_position_actions.py'), encoding='utf-8').read()


class CardButtonsWorkOnEveryCard(unittest.TestCase):
    def test_handler_does_not_bail_out_without_an_event_id(self):
        # The exact line that killed every button on an untracked card.
        self.assertNotIn('if(!eventId)return;', APP)
        # It must instead fall through to the contract-based path.
        self.assertIn("if(!eventId&&!contract){return;}", APP)

    def test_close_falls_back_to_contract(self):
        self.assertIn('close_position_by_contract', APP)
        self.assertIn('close_position_by_contract', LPA)

    def test_every_card_gets_all_five_buttons(self):
        for label in ('View Chart', 'Refresh', 'Set TP/SL',
                      'Reverse Position', 'Market Close'):
            self.assertIn(label, APP, label)

    def test_buttons_are_not_hard_disabled(self):
        # No card action may be rendered with a hard disabled attribute.
        self.assertNotIn("mk('Market Close','close');b.disabled=true", APP)
        self.assertNotIn('canAct=false', APP)


class PerCardChart(unittest.TestCase):
    def test_down_arrow_toggle_exists(self):
        self.assertIn('position-chart-toggle', APP)
        self.assertIn('position-chart-box', APP)

    def test_chart_is_a_real_tradingview_embed(self):
        self.assertIn('tradingViewEmbedUrl', APP)
        self.assertIn('s.tradingview.com/widgetembed', APP)
        # Indicator studies, so it is an indicator chart, not a bare line.
        self.assertIn('RSI@tv-basicstudies', APP)
        self.assertIn('MASimple@tv-basicstudies', APP)

    def test_clicking_outside_closes_the_chart(self):
        self.assertIn("document.addEventListener('click'", APP)
        self.assertIn("closest('.position-chart-box')", APP)

    def test_chart_box_is_appended_below_the_card(self):
        self.assertIn('card.append(inner,chartBox)', APP)


class OrderTableSerial(unittest.TestCase):
    def test_serial_header_is_first(self):
        m = re.search(r'<thead><tr>(.*?)</tr></thead><tbody id="orderTableBody"',
                      APP, re.S)
        self.assertIsNotNone(m)
        self.assertTrue(m.group(1).startswith('<th>S/N</th>'), m.group(1)[:80])

    def test_serial_cell_is_written_first(self):
        self.assertIn("otCell(tr,String(idx+1),'ot-serial')", APP)

    def test_colspans_match_the_ten_columns(self):
        self.assertIn('td.colSpan=10', APP)
        self.assertNotIn('td.colSpan=9', APP)


class PanelOrder(unittest.TestCase):
    def test_scanner_is_above_the_radar(self):
        i_adv = APP.find('id="advancedScannerPanel"')
        i_radar = APP.find('GATE.IO FUTURES RADAR')
        self.assertGreater(i_adv, 0)
        self.assertGreater(i_radar, 0)
        self.assertLess(i_adv, i_radar)

    def test_scanner_panel_is_not_duplicated(self):
        self.assertEqual(APP.count('id="advancedScannerPanel"'), 1)
        self.assertEqual(APP.count('id="advancedScanBody"'), 1)
        self.assertEqual(APP.count('id="advancedRefresh"'), 1)


if __name__ == '__main__':
    unittest.main()
