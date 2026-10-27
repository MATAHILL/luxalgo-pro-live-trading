import unittest
from gate_testnet_execution import (
    TestnetExecutionDisabled, order_payload, prepare_order_for_review,
    send_protected_order, signing_message, protective_trigger_payload, verify_testnet_protection,
)

class TestnetExecutionSafetyTests(unittest.TestCase):
    def test_market_order_envelope(self):
        p=order_payload("BTC_USDT", 1, "MARKET", None, "t-test123")
        self.assertEqual((p["price"],p["tif"]),("0","ioc"))
    def test_limit_order_envelope(self):
        p=order_payload("BTC_USDT", -2, "LIMIT", "100", "t-test123")
        self.assertEqual((p["price"],p["tif"]),("100","gtc"))
    def test_market_rejects_limit_price(self):
        with self.assertRaises(ValueError):
            order_payload("BTC_USDT", 1, "MARKET", "100", "t-test123")
    def test_size_must_be_integer_nonzero(self):
        for size in (0, True, 1.5, "1"):
            with self.subTest(size=size), self.assertRaises(ValueError):
                order_payload("BTC_USDT", size, "MARKET", None, "t-test123")
    def test_invalid_limit_price(self):
        for price in ("NaN", "-1", "0", None):
            with self.subTest(price=price), self.assertRaises(ValueError):
                order_payload("BTC_USDT", 1, "LIMIT", price, "t-test123")
    def test_signing_message_blocks_spot_path(self):
        with self.assertRaises(ValueError):
            signing_message("POST", "/api/v4/spot/orders", "", b"{}", "123")
    def test_preview_never_executes(self):
        p=prepare_order_for_review(dict(contract="BTC_USDT",size=1,
            order_type="MARKET",client_id="t-test123"))
        self.assertFalse(p["submitted"])
        self.assertFalse(p["protective_orders_verified"])
    def test_native_long_stop_and_target(self):
        stop=protective_trigger_payload("BTC_USDT","LONG","STOP_LOSS","90","100")
        target=protective_trigger_payload("BTC_USDT","LONG","TAKE_PROFIT","110","100")
        self.assertEqual(stop["trigger"]["rule"],2)
        self.assertEqual(target["trigger"]["rule"],1)
        self.assertEqual(stop["initial"]["auto_size"],"close_long")
    def test_native_short_stop_and_target(self):
        stop=protective_trigger_payload("BTC_USDT","SHORT","STOP_LOSS","110","100")
        target=protective_trigger_payload("BTC_USDT","SHORT","TAKE_PROFIT","90","100")
        self.assertEqual(stop["trigger"]["rule"],1)
        self.assertEqual(target["trigger"]["rule"],2)
        self.assertEqual(target["initial"]["auto_size"],"close_short")
    def test_wrong_side_trigger_rejected(self):
        with self.assertRaises(ValueError):
            protective_trigger_payload("BTC_USDT","LONG","STOP_LOSS","110","100")

    def test_fill_and_native_protection_readback(self):
        order={"contract":"BTC_USDT","finish_as":"filled"}
        positions=[{"contract":"BTC_USDT","mode":"dual_long","size":1}]
        triggers=[
          {"id":123,"status":"open","initial":{"contract":"BTC_USDT","auto_size":"close_long"},"trigger":{"price":"90","rule":2}},
          {"id":124,"status":"open","initial":{"contract":"BTC_USDT","auto_size":"close_long"},"trigger":{"price":"110","rule":1}},
        ]
        args=dict(expected_contract="BTC_USDT",expected_side="LONG",stop_price="90",target_price="110")
        self.assertTrue(verify_testnet_protection(order,positions,triggers,**args)["verified"])
        self.assertFalse(verify_testnet_protection(order,positions,triggers[:1],**args)["verified"])
        self.assertFalse(verify_testnet_protection({**order,"finish_as":"cancelled"},positions,triggers,**args)["verified"])
        self.assertFalse(verify_testnet_protection(order,[],triggers,**args)["verified"])
        self.assertFalse(verify_testnet_protection(order,positions,triggers[:1]+[dict(triggers[1],id=123)],**args)["verified"])

    def test_submit_disabled(self):
        with self.assertRaises(TestnetExecutionDisabled):
            send_protected_order()

if __name__=="__main__":
    unittest.main()
