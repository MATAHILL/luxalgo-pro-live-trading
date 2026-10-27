import unittest
from unittest.mock import patch,Mock
from testnet_recovery import recover_testnet_once
from test_testnet_protection_watchdog import TestProtectionWatchdog

ROW={"signal_id":"abc123456789","client_id":"t-recovery123","contract":"BTC_USDT",
     "side":"LONG","payload":{"contract":"BTC_USDT","text":"t-recovery123","size":1,
       "tpsl_sl_trigger_price":"90","tpsl_tp_trigger_price":"110"}}

class RecoveryTests(unittest.TestCase):
    def run_case(self,protected=True,close_success=True,lookup_error=False):
        journal=Mock()
        journal.recovery_candidates.return_value=[{"signal_id":ROW["signal_id"]}]
        journal.claim_recovery.return_value=dict(ROW)
        broker=Mock()
        broker.lookup_by_client_id.return_value={"id":321,"contract":"BTC_USDT",
           "text":"t-recovery123","size":1,"status":"finished","finish_as":"filled"}
        if lookup_error:
            broker.lookup_by_client_id.side_effect=TimeoutError
        broker.positions.return_value=[{"contract":"BTC_USDT","mode":"dual_long","size":1}]
        broker.native_triggers.return_value=[{"id":1}]
        broker.verify_protection.return_value=protected
        broker.emergency_close.return_value=close_success
        with patch.dict("os.environ",{"GATE_TESTNET_PROTECTED_EXECUTION_ENABLED":"true",
                                      "GATE_TESTNET_WEBHOOK_AUTOMATION_ENABLED":"true",
                                      "GATE_TESTNET_RECOVERY_ENABLED":"true"}):
            result=recover_testnet_once(journal=journal,broker_factory=lambda:broker)
        return result,journal,broker

    def test_recovery_verifies_linked_protection_without_reentry(self):
        result,journal,broker=self.run_case()
        self.assertEqual(result["outcomes"],["PROTECTED_VERIFIED"])
        broker.submit.assert_not_called()
        self.assertTrue(any(c.args[1]=="PROTECTED_VERIFIED"
                       for c in journal.transition.call_args_list))

    def test_missing_protection_emergency_closes_testnet(self):
        result,journal,broker=self.run_case(protected=False)
        self.assertEqual(result["outcomes"],["EMERGENCY_CLOSED"])
        broker.emergency_close.assert_called_once()
        broker.submit.assert_not_called()

    def test_known_order_id_recovery_uses_numeric_get_not_client_text(self):
        journal=Mock()
        journal.recovery_candidates.return_value=[{"signal_id":ROW["signal_id"]}]
        journal.claim_recovery.return_value={**ROW,"details":{"order_id":"321"}}
        broker=Mock()
        broker.order.return_value={"id":321,"contract":"BTC_USDT",
            "text":"t-recovery123","size":1,"status":"finished","finish_as":"filled"}
        broker.positions.return_value=[{"contract":"BTC_USDT","mode":"dual_long","size":1}]
        broker.native_triggers.return_value=[{"id":1}]
        broker.verify_protection.return_value=True
        with patch.dict("os.environ",{"GATE_TESTNET_RECOVERY_ENABLED":"true"}):
            result=recover_testnet_once(journal=journal,broker_factory=lambda:broker)
        self.assertEqual(result["outcomes"],["PROTECTED_VERIFIED"])
        broker.order.assert_called_once_with("321")
        broker.lookup_by_client_id.assert_not_called()
        broker.submit.assert_not_called()

    def test_unknown_client_id_never_retries_entry(self):
        result,journal,broker=self.run_case(lookup_error=True)
        self.assertEqual(result["outcomes"],["HALTED_UNKNOWN"])
        broker.submit.assert_not_called()
        broker.emergency_close.assert_not_called()

    def test_failed_emergency_close_halts(self):
        result,journal,broker=self.run_case(protected=False,close_success=False)
        self.assertEqual(result["outcomes"],["HALTED_UNPROTECTED"])

    def test_already_flat_recovery_does_not_send_any_close(self):
        journal=Mock()
        journal.recovery_candidates.return_value=[{"signal_id":ROW["signal_id"]}]
        journal.claim_recovery.return_value=dict(ROW)
        broker=Mock()
        broker.lookup_by_client_id.return_value={"id":321,"contract":"BTC_USDT",
           "text":"t-recovery123","size":1,"status":"finished","finish_as":"filled"}
        broker.positions.return_value=[]
        broker.native_triggers.return_value=[]
        with patch.dict("os.environ",{"GATE_TESTNET_PROTECTED_EXECUTION_ENABLED":"true",
             "GATE_TESTNET_WEBHOOK_AUTOMATION_ENABLED":"true",
                                      "GATE_TESTNET_RECOVERY_ENABLED":"true"}):
            result=recover_testnet_once(journal=journal,broker_factory=lambda:broker)
        self.assertEqual(result["outcomes"],["CLOSED_VERIFIED"])
        broker.emergency_close.assert_not_called()
        broker.submit.assert_not_called()

    def test_disabled_recovery_does_not_contact_exchange(self):
        with patch.dict("os.environ",{"GATE_TESTNET_PROTECTED_EXECUTION_ENABLED":"false",
                 "GATE_TESTNET_WEBHOOK_AUTOMATION_ENABLED":"false",
                 "GATE_TESTNET_RECOVERY_ENABLED":"false"}):
            journal=Mock()
            result=recover_testnet_once(journal=journal)
            self.assertEqual(result["state"],"TESTNET_DISABLED")
            journal.recovery_candidates.assert_not_called()

if __name__=="__main__":
    unittest.main()
