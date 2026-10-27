import unittest
from unittest.mock import Mock,patch
from testnet_manual_close import request_testnet_close

EID="testnet-trial-20261027-a"
OID="12345"
CLIENT="t-lx-trial-20261027-a"
FLAGS={"LIVE_TRADING_ENABLED":"false","ORDERS_ENABLED":"false",
       "GATE_TESTNET_RECOVERY_ENABLED":"true",
       "GATE_TESTNET_ONE_SHOT_PROTECTED_TRIAL":"false"}
CLAIM={"signal_id":EID,"contract":"ETH_USDT","side":"LONG",
       "client_id":CLIENT,"payload":{"contract":"ETH_USDT","size":1,
       "text":CLIENT,"tpsl_sl_trigger_price":"1900","tpsl_tp_trigger_price":"2100"},
       "details":{"order_id":OID}}
def fixtures():
    b=Mock()
    b.order.return_value={"id":12345,"contract":"ETH_USDT","finish_as":"filled",
                          "text":CLIENT}
    b.positions.return_value=[{"contract":"ETH_USDT","mode":"dual_long","size":1}]
    b.native_triggers.return_value=[]
    b.emergency_close.return_value=True
    l=Mock()
    l.claim_manual_close.return_value=CLAIM
    return b,l

class CloseTests(unittest.TestCase):
    def call(self,b,l):
        with patch.dict("os.environ",FLAGS):
            return request_testnet_close(EID,"ETH_USDT","LONG",OID,broker=b,journal=l)
    def test_success_only_after_exchange_readbacks(self):
        b,l=fixtures()
        self.assertEqual(self.call(b,l)["state"],"CLOSED_VERIFIED")
        b.emergency_close.assert_called_once()
        self.assertEqual(l.transition.call_args.args[1],"CLOSED_VERIFIED")
    def test_repeat_claim_rejected_before_broker(self):
        b,l=fixtures();l.claim_manual_close.return_value=None
        self.assertEqual(self.call(b,l)["state"],"POSITION_NOT_CLAIMABLE")
        b.emergency_close.assert_not_called()
    def test_unverified_entry_not_closed(self):
        b,l=fixtures();b.order.return_value={**b.order.return_value,"finish_as":"cancelled"}
        self.assertEqual(self.call(b,l)["state"],"HALTED_UNKNOWN")
        b.emergency_close.assert_not_called()
    def test_unverified_close_quarantined(self):
        b,l=fixtures();b.emergency_close.return_value=False
        self.assertEqual(self.call(b,l)["state"],"HALTED_UNKNOWN")
        b.emergency_close.assert_called_once()
    def test_orphan_stop_not_reported_closed(self):
        b,l=fixtures();b.native_triggers.return_value=[{"status":"open","me_order_id":12345}]
        self.assertEqual(self.call(b,l)["state"],"HALTED_UNKNOWN")
    def test_live_flags_block_all_close_network(self):
        b,l=fixtures()
        with patch.dict("os.environ",{**FLAGS,"LIVE_TRADING_ENABLED":"true"}):
            r=request_testnet_close(EID,"ETH_USDT","LONG",OID,broker=b,journal=l)
        self.assertEqual(r["state"],"TESTNET_CLOSE_DISABLED")
        l.claim_manual_close.assert_not_called()
        b.emergency_close.assert_not_called()

if __name__=="__main__": unittest.main()
