"""The legacy locked position card is deleted; the working card is the only one.

The old PRODUCTION_CARDS panel rendered three permanently disabled buttons
("Market Close — LOCKED", "Close — LOCKED", "Reverse — LOCKED") directly above
the working position cards. It was a duplicate that could never work, so it is
removed from the code entirely rather than hidden or disabled.

These tests assert the deletion is real and that the surviving card panel is
the one with live action buttons.
"""
import unittest

import production_cutover_view as pcv
from app import PAGE
from production_cutover_view import production_monitor_page


class LegacyCardRemovedTests(unittest.TestCase):
    def test_production_cards_symbol_is_gone(self):
        self.assertFalse(hasattr(pcv, 'PRODUCTION_CARDS'),
                         'PRODUCTION_CARDS must be deleted, not hidden')

    def test_no_locked_buttons_anywhere_in_the_rendered_page(self):
        out = production_monitor_page(PAGE)
        for dead in ('Market Close \u2014 LOCKED', 'Close \u2014 LOCKED',
                     'Reverse \u2014 LOCKED', 'productionPositionCards'):
            self.assertNotIn(dead, out, dead)

    def test_legacy_panel_ids_are_gone(self):
        out = production_monitor_page(PAGE)
        for gone in ('id="productionCardsPanel"', 'id="productionPositionCards"',
                     'id="productionTabCount"', 'id="productionCardsRefresh"'):
            self.assertNotIn(gone, out, gone)

    def test_working_position_panel_survives(self):
        out = production_monitor_page(PAGE)
        self.assertIn('id="openPositionPanel"', out)
        self.assertIn('id="gatePositionsBody"', out)
        self.assertIn('Open Position', out)
        self.assertIn('Gate.io Futures', out)

    def test_legacy_css_is_gone(self):
        self.assertNotIn('.production-position-cards', pcv.PRODUCTION_CSS)
        self.assertNotIn('.production-token-toggle', pcv.PRODUCTION_CSS)

    def test_unrelated_cutover_logic_still_intact(self):
        # Deleting the card must not remove the testnet-hiding or bot control.
        out = production_monitor_page(PAGE)
        self.assertIn('id="productionBotIndicator"', out)
        self.assertIn('LIVE FUTURES ORDERS', out)
        self.assertNotIn('id="v4Workspace"', out)
        self.assertNotIn('id="testnetReadinessPanel"', out)


if __name__ == '__main__':
    unittest.main()
