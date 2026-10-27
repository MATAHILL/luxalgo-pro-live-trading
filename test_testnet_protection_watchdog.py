import unittest
from unittest.mock import Mock,patch
from testnet_protection_watchdog import audit_protected_testnet_once

ROW={"signal_id":"evt-monitor01","client_id":"t-monitor01",
     "contract":"ETH_USDT","side":"LONG",
     "details":{"order_id":"6789"},
     "payload":{"contract":"ETH_USDT","size":1,
                "tpsl_tp_trigger_price":"110","tpsl_sl_trigger_price":"90"}}

class TestProtectionWatchdog(unittest.TestCase):
    def inspect(self,*,position_size="1",protect=True,emergency=True,
                orphan=False,readback_error=False):
        journal=Mock()
        journal.protected_audit_candidates.return_value=[{"signal_id":ROW["signal_id"]}]
        journal.claim_protected_audit.return_value=dict(ROW)
        broker=Mock()
        broker.positions.return_value=[{"contract":"ETH_USDT",
                "mode":"dual_long","size":position_size}]
        broker.native_triggers.return_value=[
            {"id":1,"me_order_id":6789,"status":"open"}] if orphan else []
        broker.order.return_value={"id":6789,"contract":"ETH_USDT"}
        if readback_error:
            broker.positions.side_effect=TimeoutError("testnet is unreachable")
        broker.verify_protection.return_value=protect
        if protect:
            broker.last_protection_evidence={
                "entry_id":"6789","entry_finish_as":"filled",
                "native_protection_ids":["201","202"]}
        broker.emergency_close.return_value=emergency
        with patch.dict("os.environ",{
              "GATE_TESTNET_PROTECTED_EXECUTION_ENABLED":"true",
              "GATE_TESTNET_WEBHOOK_AUTOMATION_ENABLED":"true",
              "GATE_TESTNET_RECOVERY_ENABLED":"true",
              "LIVE_TRADING_ENABLED":"false","ORDERS_ENABLED":"false"}):
            result=audit_protected_testnet_once(journal=journal,broker_factory=lambda:broker)
        broker.submit.assert_not_called()
        return result,journal,broker

    def test_active_protection_verified_without_entry(self):
        result,journal,broker=self.inspect()
        self.assertEqual(result["outcomes"],["PROTECTED_VERIFIED"])
        broker.verify_protection.assert_called_once()
        verified=[c for c in journal.transition.call_args_list
                  if c.args[1]=="PROTECTED_VERIFIED"]
        self.assertEqual(verified[-1].args[2]["native_protection_ids"],["201","202"])
        broker.emergency_close.assert_not_called()

    def test_disappearing_native_stop_triggers_emergency_close(self):
        result,journal,broker=self.inspect(protect=False)
        self.assertEqual(result["outcomes"],["EMERGENCY_CLOSED"])
        broker.emergency_close.assert_called_once()
        self.assertTrue(any(c.args[1]=="EMERGENCY_CLOSE_REQUIRED"
                            for c in journal.transition.call_args_list))

    def test_failed_emergency_close_is_quarantined(self):
        result,journal,broker=self.inspect(protect=False,emergency=False)
        self.assertEqual(result["outcomes"],["HALTED_UNPROTECTED"])
        broker.emergency_close.assert_called_once()

    def test_flat_position_closes_journal_without_order(self):
        result,journal,broker=self.inspect(position_size="0")
        self.assertEqual(result["outcomes"],["CLOSED_VERIFIED"])
        broker.order.assert_not_called()
        broker.emergency_close.assert_not_called()

    def test_orphan_native_trigger_never_marked_flat(self):
        result,journal,broker=self.inspect(position_size="0",orphan=True)
        self.assertEqual(result["outcomes"],["HALTED_UNKNOWN"])
        broker.emergency_close.assert_not_called()

    def test_unknown_exchange_readback_never_repeats_order(self):
        result,journal,broker=self.inspect(readback_error=True)
        self.assertEqual(result["outcomes"],["HALTED_UNKNOWN"])
        broker.emergency_close.assert_not_called()

    def test_bad_position_quantity_not_assumed_flat(self):
        result,journal,broker=self.inspect(position_size="NaN")
        self.assertEqual(result["outcomes"],["HALTED_UNKNOWN"])
        broker.emergency_close.assert_not_called()

    def test_monitor_off_does_not_read_exchange(self):
        with patch.dict("os.environ",{
              "GATE_TESTNET_PROTECTED_EXECUTION_ENABLED":"false",
              "GATE_TESTNET_WEBHOOK_AUTOMATION_ENABLED":"false",
              "GATE_TESTNET_RECOVERY_ENABLED":"false"}):
            ledger=Mock()
            result=audit_protected_testnet_once(journal=ledger)
            self.assertEqual(result["state"],"TESTNET_DISABLED")
            ledger.protected_audit_candidates.assert_not_called()

if __name__=="__main__":
    unittest.main()
