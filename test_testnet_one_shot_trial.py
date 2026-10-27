import unittest
from unittest.mock import Mock,patch
from testnet_one_shot_trial import run_one_shot_protected_testnet_trial
from gate_testnet_broker import ROOT
ENV={"GATE_TESTNET_ONE_SHOT_PROTECTED_TRIAL":"true",
     "GATE_TESTNET_PROTECTED_EXECUTION_ENABLED":"true",
     "GATE_TESTNET_RECOVERY_ENABLED":"true",
     "GATE_TESTNET_WEBHOOK_AUTOMATION_ENABLED":"false",
     "LIVE_TRADING_ENABLED":"false","ORDERS_ENABLED":"false",
     "GATE_TESTNET_API_KEY":"test-only-key","GATE_TESTNET_API_SECRET":"test-only-secret"}
SPEC={"name":"ETH_USDT","mark_price":"2000","order_price_round":"0.1",
      "quanto_multiplier":"0.0001","order_size_min":"1","order_size_max":"1000",
      "leverage_max":"50","in_delisting":False}
ACCT={"currency":"USDT","available":"1000","in_dual_mode":True}
def request(method,path,*,query="",body=None):
    if method!="GET":raise AssertionError("fake must never submit a real POST")
    if path==ROOT+"/accounts":return ACCT
    if path==ROOT+"/contracts/ETH_USDT":return SPEC
    if path in (ROOT+"/positions",ROOT+"/orders",ROOT+"/price_orders"):return []
    if path==ROOT+"/get_leverage/ETH_USDT":return {"Lever":"5"}
    raise AssertionError(path)

class TestOneShot(unittest.TestCase):
    def test_disabled_without_any_network(self):
        with patch.dict("os.environ",{**ENV,"GATE_TESTNET_ONE_SHOT_PROTECTED_TRIAL":"false"}):
            r=run_one_shot_protected_testnet_trial()
        self.assertEqual(r["state"],"TRIAL_DISABLED")
    def test_cannot_run_with_webhook_or_live(self):
        for k in ("GATE_TESTNET_WEBHOOK_AUTOMATION_ENABLED","LIVE_TRADING_ENABLED","ORDERS_ENABLED"):
            with self.subTest(flag=k),patch.dict("os.environ",{**ENV,k:"true"}):
                r=run_one_shot_protected_testnet_trial()
                self.assertEqual(r["state"],"UNSAFE_EXECUTION_FLAG_COMBINATION")
    def test_one_contract_risk_bounded_with_journal(self):
        gateway=Mock()
        gateway.request=request
        gateway.preflight.return_value=True
        gateway.submit.return_value={"id":12345}
        gateway.order.return_value={"id":12345,"contract":"ETH_USDT","finish_as":"filled"}
        gateway.positions.return_value=[{"mode":"dual_long","contract":"ETH_USDT","size":1}]
        gateway.native_triggers.return_value=[{"id":567}]
        gateway.verify_protection.return_value=True
        journal=Mock()
        journal.reserve.return_value=True
        with patch.dict("os.environ",ENV):
            r=run_one_shot_protected_testnet_trial(broker=gateway,journal=journal)
        self.assertEqual(r["state"],"PROTECTED_VERIFIED")
        self.assertEqual(gateway.submit.call_count,1)
        payload=gateway.submit.call_args.args[0]
        self.assertEqual(payload["contract"],"ETH_USDT")
        self.assertEqual(payload["size"],1)
        self.assertEqual(payload["text"],"t-lx-trial-20261027-a")
        self.assertLess(float(payload["tpsl_sl_trigger_price"]),2000)
        self.assertGreater(float(payload["tpsl_tp_trigger_price"]),2000)
        gateway.emergency_close.assert_not_called()
    def test_repeated_signal_never_resends(self):
        gw=Mock();gw.request=request
        ledger=Mock();ledger.reserve.return_value=False
        with patch.dict("os.environ",ENV):
            r=run_one_shot_protected_testnet_trial(broker=gw,journal=ledger)
        self.assertEqual(r["state"],"DUPLICATE_BLOCKED")
        gw.submit.assert_not_called()
    def test_fresh_account_with_pending_triggers_blocks_order(self):
        def unsafe(method,path,*,query="",body=None):
            if path==ROOT+"/price_orders":return [{"id":100,"status":"open"}]
            return request(method,path,query=query,body=body)
        gw=Mock();gw.request=unsafe
        ledger=Mock()
        with patch.dict("os.environ",ENV):
            r=run_one_shot_protected_testnet_trial(broker=gw,journal=ledger)
        self.assertEqual(r["state"],"TESTNET_ACCOUNT_NOT_EMPTY_OR_UNVERIFIED")
        ledger.reserve.assert_not_called()
        gw.submit.assert_not_called()
if __name__=="__main__":unittest.main()
