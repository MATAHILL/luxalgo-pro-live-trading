import unittest
from datetime import timezone
from incoming_testnet_status import classify_signal,parse_reset_time

class IncomingStatusTests(unittest.TestCase):
    def test_live_requires_both_protected_journal_and_exchange(self):
        self.assertEqual(classify_signal(journal_state="PROTECTED_VERIFIED",
                     exchange_open=True,exchange_checked=True),"LIVE")
        self.assertEqual(classify_signal(journal_state="PROTECTED_VERIFIED",
                     exchange_open=False,exchange_checked=True),"VERIFY EXCHANGE")
        self.assertEqual(classify_signal(journal_state="PROTECTED_VERIFIED",
                     exchange_open=True,exchange_checked=False),"VERIFY EXCHANGE")
    def test_closed_is_exchange_journal_only(self):
        for state in ("CLOSED_VERIFIED","EMERGENCY_CLOSED"):
            self.assertEqual(classify_signal(journal_state=state),"CLOSED")
        self.assertNotEqual(classify_signal(attempt_state="CLOSED_VERIFIED"),"CLOSED")
    def test_waiting_only_fresh_signal(self):
        self.assertEqual(classify_signal(queue_state="PENDING",age_seconds=20),"WAITING FOR EXECUTION")
        self.assertEqual(classify_signal(queue_state="PENDING",age_seconds=90),"NOT EXECUTED")
        self.assertEqual(classify_signal(attempt_state="WORKER_QUEUED",
                                         queue_state="PENDING",age_seconds=20),"WAITING FOR EXECUTION")
    def test_failure_is_not_misreported_waiting(self):
        self.assertEqual(classify_signal(attempt_state="TESTNET_PREFLIGHT_BLOCKED",
                                 queue_state="PENDING",age_seconds=2),"NOT EXECUTED")
        self.assertEqual(classify_signal(journal_state="HALTED_UNKNOWN",
                                 exchange_checked=True,exchange_open=False),"VERIFY EXCHANGE")
    def test_unlisted_testnet_market_is_distinct_from_execution_failure(self):
        state=classify_signal(attempt_state="TESTNET_PREFLIGHT_BLOCKED",
            attempt_detail="CONTRACT_NOT_LISTED_ON_TESTNET",
            queue_state="PENDING",age_seconds=3)
        self.assertEqual(state,"UNSUPPORTED ON TESTNET")
        self.assertEqual(classify_signal(attempt_state="TESTNET_PREFLIGHT_BLOCKED",
            attempt_detail="LEVERAGE_EXCEEDS_TESTNET_MAX_5X"),"NOT EXECUTED")
        self.assertNotEqual(state,"LIVE")

    def test_reset_time_requires_zone(self):
        x=parse_reset_time("2026-10-27T19:35:00Z")
        self.assertEqual(x.tzinfo,timezone.utc)
        self.assertEqual(x.hour,19)
        with self.assertRaises(ValueError):parse_reset_time("2026-10-27T19:35:00")
        with self.assertRaises(ValueError):parse_reset_time("not-a-date")

if __name__=="__main__":unittest.main()
