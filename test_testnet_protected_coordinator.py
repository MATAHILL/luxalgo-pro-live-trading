import unittest
from unittest.mock import Mock
from testnet_protected_coordinator import TestnetIntent, execute_protected_testnet

INTENT=TestnetIntent("evt123","BTC_USDT","LONG","100","90","110","t-testnet123")

class TestnetProtectedCoordinatorTests(unittest.TestCase):
    def test_protected_trade(self):
        journal=Mock()
        journal.reserve.return_value=True
        broker=Mock()
        broker.preflight.return_value=True
        broker.submit.return_value={"id":123}
        broker.order.return_value={"id":123,"contract":"BTC_USDT","finish_as":"filled"}
        broker.verify_protection.return_value=True
        result=execute_protected_testnet(INTENT,broker,journal)
        self.assertEqual(result["state"],"PROTECTED_VERIFIED")
        broker.emergency_close.assert_not_called()

    def test_no_entry_without_preflight(self):
        journal=Mock()
        journal.reserve.return_value=True
        broker=Mock()
        broker.preflight.return_value=False
        result=execute_protected_testnet(INTENT,broker,journal)
        self.assertEqual(result["state"],"BLOCKED")
        broker.submit.assert_not_called()

    def test_unprotected_fill_emergency_close(self):
        journal=Mock()
        journal.reserve.return_value=True
        broker=Mock()
        broker.preflight.return_value=True
        broker.submit.return_value={"id":123}
        broker.order.return_value={"id":123,"contract":"BTC_USDT","finish_as":"filled"}
        broker.verify_protection.return_value=False
        broker.emergency_close.return_value=True
        result=execute_protected_testnet(INTENT,broker,journal)
        self.assertEqual(result["state"],"EMERGENCY_CLOSED")
        broker.emergency_close.assert_called_once()

    def test_timeout_is_unknown_not_retry(self):
        journal=Mock()
        journal.reserve.return_value=True
        broker=Mock()
        broker.preflight.return_value=True
        broker.submit.side_effect=TimeoutError("timeout")
        with self.assertRaises(TimeoutError):
            execute_protected_testnet(INTENT,broker,journal)
        broker.submit.assert_called_once()
        states=[call.args[1] for call in journal.transition.call_args_list]
        self.assertIn("SUBMISSION_UNKNOWN",states)
        self.assertEqual(states[-1],"HALTED_UNKNOWN")

    def test_duplicate_never_submits(self):
        journal=Mock()
        journal.reserve.return_value=False
        broker=Mock()
        result=execute_protected_testnet(INTENT,broker,journal)
        self.assertEqual(result["state"],"DUPLICATE_BLOCKED")
        broker.submit.assert_not_called()

if __name__=="__main__":
    unittest.main()
