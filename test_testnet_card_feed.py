import unittest
from unittest.mock import Mock
from testnet_card_feed import build_testnet_position_cards

POSITION={"contract":"ETH_USDT","side":"LONG","position_mode":"dual_long",
    "size_contracts":1,"entry_price":2400,"mark_price":2410,
    "unrealized_pnl":0.1,"leverage_label":"5×","margin_usdt":5,
    "roi_percent":2,"liquidation_price":1800}
ROW={"signal_id":"testnet-trial-20261027-a","client_id":"t-lx-trial-20261027-a",
    "contract":"ETH_USDT","side":"LONG","state":"PROTECTED_VERIFIED",
    "payload":{"contract":"ETH_USDT","size":1,"text":"t-lx-trial-20261027-a",
               "tpsl_sl_trigger_price":"2380","tpsl_tp_trigger_price":"2450"},
    "details":{"order_id":"123"}}

def broker():
    b=Mock()
    b.order.return_value={"id":123,"contract":"ETH_USDT","status":"finished","finish_as":"filled"}
    b.positions.return_value=[{"contract":"ETH_USDT","mode":"dual_long","size":1}]
    b.native_triggers.return_value=[
        {"id":401,"status":"open"},{"id":402,"status":"open"}]
    b.verify_protection.return_value=True
    b.last_protection_evidence={
        "entry_id":"123","entry_finish_as":"filled",
        "native_protection_ids":["401","402"]}
    return b

class TestnetCardFeedTests(unittest.TestCase):
    def test_exchange_position_and_verified_native_tpsl(self):
        b=broker()
        cards=build_testnet_position_cards([POSITION],[ROW],b)
        self.assertEqual(len(cards),1)
        card=cards[0]
        self.assertEqual(card["source"],"GATE_FUTURES_TESTNET")
        self.assertEqual(card["unrealized_pnl"],0.1)
        self.assertEqual(card["mark_price"],2410)
        self.assertEqual(card["stop_loss"],"2380")
        self.assertEqual(card["take_profit"],"2450")
        self.assertEqual(card["stop_order_id"],"401")
        self.assertEqual(card["protection_state"],"EXCHANGE_VERIFIED")
        b.submit.assert_not_called()
        b.emergency_close.assert_not_called()

    def test_untracked_position_visible_but_unprotected(self):
        b=broker()
        card=build_testnet_position_cards([POSITION],[],b)[0]
        self.assertEqual(card["protection_state"],"UNTRACKED_TESTNET_POSITION")
        self.assertIsNone(card["stop_loss"])
        b.order.assert_not_called()

    def test_missing_native_protection_never_claims_verified(self):
        b=broker()
        b.verify_protection.return_value=False
        card=build_testnet_position_cards([POSITION],[ROW],b)[0]
        self.assertEqual(card["protection_state"],"NATIVE_PROTECTION_UNVERIFIED")
        self.assertIsNone(card["take_profit"])

    def test_readback_failure_preserves_position_not_stale_tpsl(self):
        b=broker()
        b.order.side_effect=TimeoutError("exchange unavailable")
        card=build_testnet_position_cards([POSITION],[ROW],b)[0]
        self.assertEqual(card["symbol"],"ETHUSDT")
        self.assertEqual(card["protection_state"],"PROTECTION_READBACK_UNAVAILABLE")
        self.assertIsNone(card["stop_loss"])

    def test_journal_mismatch_never_displays_protection(self):
        b=broker()
        card=build_testnet_position_cards([POSITION],[{**ROW,"client_id":"t-other"}],b)[0]
        self.assertEqual(card["protection_state"],"JOURNAL_IDENTITY_MISMATCH")
        b.order.assert_not_called()

    def test_no_magic_pnl_or_positions(self):
        b=broker()
        self.assertEqual(build_testnet_position_cards([],[],b),[])
        with self.assertRaises(ValueError):
            build_testnet_position_cards("not-an-exchange-list",[],b)

if __name__=="__main__":
    unittest.main()
