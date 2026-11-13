"""Position card layout, per-card indicators and per-card action buttons.

Covers the four v19 requirements:
  1. two cards per row on desktop, stacking below
  2. clicking the token name slides that card's indicator table down
  3. every card's action buttons are wired (no silent no-op)
  4. the legacy locked card is gone (asserted in test_live_production_card_dom)
"""
import pathlib
import re
import unittest

from app import PAGE
from production_cutover_view import production_monitor_page

SOURCE = pathlib.Path(__file__).resolve().parents[1] / 'app.py'
SRC = SOURCE.read_text(encoding='utf-8')


class PositionCardLayoutTests(unittest.TestCase):
    def test_two_cards_per_row_on_desktop(self):
        self.assertIn(
            '.position-card-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))',
            PAGE)
        # The old three-per-row rule must be gone.
        self.assertNotIn(
            '.position-card-list{display:grid;grid-template-columns:repeat(3,minmax(0,1fr))',
            PAGE)

    def test_stacks_to_one_column_on_small_screens(self):
        self.assertIn('@media(max-width:900px){.position-card-list{grid-template-columns:1fr}}',
                      PAGE)

    def test_card_heading_is_not_a_collapsing_details_element(self):
        # A <details> summary would collapse the whole card, hiding the buttons.
        self.assertNotIn("element('details','position-card')", PAGE)
        self.assertIn("element('div','position-card-head')", PAGE)


class PerCardIndicatorTests(unittest.TestCase):
    def test_token_name_is_a_toggle_button(self):
        self.assertIn("element('button','position-token-toggle')", PAGE)
        self.assertIn("token.setAttribute('aria-expanded','false')", PAGE)

    def test_each_card_owns_its_own_indicator_panel(self):
        # The panel is created inside the per-position loop, so it is per-card.
        self.assertIn("const ind=element('div','position-indicators')", PAGE)
        self.assertIn("ind.dataset.indOpen='false'", PAGE)

    def test_second_click_collapses_only_that_card(self):
        self.assertIn("const opening=ind.dataset.indOpen!=='true'", PAGE)
        self.assertIn("ind.dataset.indOpen=opening?'true':'false'", PAGE)

    def test_slide_down_animation_is_css_transition(self):
        self.assertIn('.position-indicators{overflow:hidden;max-height:0;opacity:0', PAGE)
        self.assertIn('.position-indicators[data-ind-open="true"]{max-height:900px', PAGE)

    def test_indicators_come_from_a_read_only_endpoint(self):
        self.assertIn("fetch('/terminal/position/indicators?contract='", PAGE)
        self.assertIn("@app.get('/terminal/position/indicators')", SRC)

    def test_indicator_endpoint_places_no_orders(self):
        src = SRC
        start = src.index("@app.get('/terminal/position/indicators')")
        end = src.index("@app.get('/terminal/position/refresh')")
        body = src[start:end]
        self.assertNotIn("method='POST'", body)
        self.assertNotIn('/orders', body)
        self.assertIn('orders_enabled=False', body)


class PerCardActionButtonTests(unittest.TestCase):
    def test_no_silent_return_when_a_card_has_no_ledger_row(self):
        # The old bug: `if(!eventId)return;` made every untracked card a no-op.
        self.assertNotIn('const kind=b.dataset.posAction,eventId=b.dataset.eventId;',
                         PAGE)
        self.assertIn("const kind=b.dataset.posAction,eventId=b.dataset.eventId||''", PAGE)

    def test_buttons_carry_their_own_contract(self):
        self.assertIn('b.dataset.contract=p.contract', PAGE)
        self.assertIn("const contract=b.dataset.contract||(card?card.dataset.contract:'')",
                      PAGE)

    def test_buttons_are_not_disabled_for_untracked_positions(self):
        self.assertNotIn("if(!canAct){b.disabled=true;", PAGE)

    def test_all_five_actions_are_present(self):
        for label in ('View Chart', 'Refresh', 'Set TP/SL',
                      'Reverse Position', 'Market Close'):
            self.assertIn(label, PAGE, label)

    def test_close_sends_contract_so_the_server_can_resolve_it(self):
        self.assertIn("body_={event_id:eventId,contract:contract}", PAGE)

    def test_close_endpoint_accepts_a_contract(self):
        self.assertIn("from live_position_actions import resolve_event_for_contract",
                      SRC)
        self.assertIn("re.fullmatch(r'[A-Z0-9]{2,28}_USDT', contract)", SRC)

    def test_protection_and_reverse_still_require_an_exact_record(self):
        # These open or re-price exposure, so they must not be guessed.
        self.assertIn("Set TP/SL and Reverse need an exact bot execution record", PAGE)


class ResolveEventTests(unittest.TestCase):
    def test_resolver_exists_and_is_documented(self):
        from live_position_actions import resolve_event_for_contract
        self.assertTrue(callable(resolve_event_for_contract))
        doc = resolve_event_for_contract.__doc__ or ''
        self.assertIn('no matching bot ledger row', doc)

    def test_resolver_never_creates_an_entry(self):
        import inspect
        from live_position_actions import resolve_event_for_contract
        src = inspect.getsource(resolve_event_for_contract)
        self.assertNotIn('reduce_only', src)
        self.assertNotIn('/orders', src)
        # The resolver must never fabricate a ledger row: the ledger enforces
        # CHECK(risk_usdt>0)/CHECK(contracts>0), so a zero placeholder raised
        # CheckViolation and the operator's Market Close click did nothing.
        # A position with no row is closed by contract instead.
        self.assertNotIn('INSERT INTO luxalgo_prod_execution_ledger', src)
        self.assertIn('return None', src)

    def test_close_by_contract_exists_and_is_reduce_only(self):
        import inspect
        from live_position_actions import close_position_by_contract
        self.assertTrue(callable(close_position_by_contract))
        src = inspect.getsource(close_position_by_contract)
        self.assertIn('CONTRACT_RE.fullmatch', src)
        # It must delegate to the shared, reduce-only close core.
        self.assertIn('_close_contract', src)
        core = inspect.getsource(
            __import__('live_position_actions')._close_contract)
        self.assertIn('reduce_only', core)
        self.assertIn('CONTRACT_RE', inspect.getsource(
            __import__('live_position_actions')))


if __name__ == '__main__':
    unittest.main()
