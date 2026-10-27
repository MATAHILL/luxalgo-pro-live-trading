import unittest
from unittest.mock import Mock,patch
from live_execution_safety import ProposedRisk
from live_execution_coordinator import attempt_guarded_live_execution

P=ProposedRisk("0123456789abcdefabcd","BTC_USDT","LONG","3","1000","800","5","100")
ENV={"LIVE_TRADING_ENABLED":"true","ORDERS_ENABLED":"true",
     "PRODUCTION_EXECUTION_REVIEWED":"true"}

class CoordinatorTests(unittest.TestCase):
    def setup_case(self):
        b=Mock()
        j=Mock()
        j.reserve.return_value={"accepted":True,"client_id":"t-lux-safe-test"}
        b.preflight.return_value=True
        b.submit.return_value={"id":123}
        b.order.return_value={"id":123,"contract":"BTC_USDT","status":"finished","finish_as":"filled"}
        b.positions.return_value=[{"contract":"BTC_USDT","mode":"dual_long","size":1}]
        b.native_triggers.return_value=[{"id":456},{"id":457}]
        b.verify_protection.return_value={"verified":True,"entry_order_id":"123",
                                         "stop_order_id":"456","take_order_id":"457"}
        return b,j

    def test_disabled_sends_nothing(self):
        b,j=self.setup_case()
        with patch.dict("os.environ",{"LIVE_TRADING_ENABLED":"false",**{k:v for k,v in ENV.items() if k!="LIVE_TRADING_ENABLED"}}):
            r=attempt_guarded_live_execution(P,b,j)
        self.assertEqual(r["state"],"DISABLED_NO_REAL_MONEY")
        b.submit.assert_not_called()
        j.reserve.assert_not_called()

    def test_filled_protected_order_requires_all_three_ids(self):
        b,j=self.setup_case()
        with patch.dict("os.environ",ENV):
            r=attempt_guarded_live_execution(P,b,j)
        self.assertEqual(r["state"],"PROTECTED_VERIFIED")
        b.submit.assert_called_once()
        details=[c.args[2] for c in j.transition.call_args_list
                 if c.args[1]=="PROTECTED_VERIFIED"]
        self.assertEqual(details[0]["stop_order_id"],"456")
        b.emergency_close.assert_not_called()

    def test_missing_stop_quarantined_and_closed(self):
        b,j=self.setup_case()
        b.verify_protection.return_value={"verified":False}
        b.emergency_close.return_value=True
        with patch.dict("os.environ",ENV):
            r=attempt_guarded_live_execution(P,b,j)
        self.assertEqual(r["state"],"CLOSED_VERIFIED")
        b.emergency_close.assert_called_once()

    def test_ambiguous_submit_timeout_never_retried(self):
        b,j=self.setup_case()
        b.submit.side_effect=TimeoutError("timeout")
        with patch.dict("os.environ",ENV):
            with self.assertRaises(TimeoutError):
                attempt_guarded_live_execution(P,b,j)
        b.submit.assert_called_once()
        states=[c.args[1] for c in j.transition.call_args_list]
        self.assertIn("SUBMISSION_UNKNOWN",states)
        self.assertEqual(states[-1],"HALTED_UNKNOWN")

    def test_duplicate_is_rejected_before_broker(self):
        b,j=self.setup_case()
        j.reserve.return_value={"accepted":False,"reason":"DUPLICATE_SIGNAL"}
        with patch.dict("os.environ",ENV):
            r=attempt_guarded_live_execution(P,b,j)
        self.assertEqual(r["state"],"DUPLICATE_SIGNAL")
        b.submit.assert_not_called()

if __name__=="__main__":
    unittest.main()
