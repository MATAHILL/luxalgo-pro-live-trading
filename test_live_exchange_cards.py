import unittest
from live_exchange_cards import exchange_position_cards

POS=[{"contract":"BTC_USDT","mode":"dual_long","size":"2","entry_price":"100",
      "mark_price":"101","unrealised_pnl":"0.20"}]
LEDGER=[{"contract":"BTC_USDT","side":"LONG","state":"PROTECTED_VERIFIED",
         "details":{"stop_order_id":"202","take_order_id":"203"}}]
TRIG=[{"id":202,"status":"open","initial":{"contract":"BTC_USDT"}},
      {"id":203,"status":"open","initial":{"contract":"BTC_USDT"}}]

class CardAdapterTests(unittest.TestCase):
    def test_protection_and_exchange_pnl(self):
        cards=exchange_position_cards(POS,LEDGER,TRIG)
        self.assertEqual(len(cards),1)
        self.assertEqual(cards[0]["unrealised_pnl_usdt"],"0.20")
        self.assertEqual(cards[0]["protection_state"],"EXCHANGE_VERIFIED")
        self.assertFalse(cards[0]["simulated"])

    def test_missing_stop_is_not_treated_as_protected(self):
        card=exchange_position_cards(POS,LEDGER,TRIG[1:])[0]
        self.assertEqual(card["protection_state"],"UNVERIFIED_HALT")

    def test_bad_pnl_or_orphan_position_never_guess(self):
        with self.assertRaises(ValueError):
            exchange_position_cards([{**POS[0],"unrealised_pnl":"NaN"}],LEDGER,TRIG)
        with self.assertRaises(ValueError):
            exchange_position_cards(POS,[],TRIG)

    def test_five_position_limit(self):
        with self.assertRaises(ValueError):
            exchange_position_cards(POS*6,LEDGER,TRIG)

if __name__=="__main__":
    unittest.main()
