import unittest
from unittest.mock import patch
from testnet_mode_prepare import prepare_testnet_hedge_mode, ROOT

def fake_gateway(method,path,*,query="",body=None):
    if method!="GET":
        raise AssertionError("GET only")
    if path==ROOT+"/accounts":
        return {"currency":"USDT","in_dual_mode":False,"position_mode":"single"}
    if path in (ROOT+"/positions",ROOT+"/orders",ROOT+"/price_orders"):
        return []
    raise AssertionError(path)

class ModePrepTests(unittest.TestCase):
    def test_optin_required_and_never_writes_when_disabled(self):
        with patch.dict("os.environ",{"GATE_TESTNET_PREPARE_HEDGE_MODE":"false"}):
            def forbid(): raise AssertionError("should not POST")
            result=prepare_testnet_hedge_mode(request=fake_gateway,writer=forbid)
        self.assertEqual(result["state"],"MODE_SETUP_DISABLED")

    def test_no_mode_switch_when_open_orders(self):
        def open_orders(method,path,*,query="",body=None):
            if path==ROOT+"/orders": return [{"id":321}]
            return fake_gateway(method,path,query=query,body=body)
        with patch.dict("os.environ",{"GATE_TESTNET_PREPARE_HEDGE_MODE":"true"}):
            def forbid(): raise AssertionError("should not POST")
            result=prepare_testnet_hedge_mode(request=open_orders,writer=forbid)
        self.assertEqual(result["state"],"OPEN_POSITIONS_OR_ORDERS_BLOCK_MODE_SWITCH")

    def test_no_mode_switch_when_open_position(self):
        def open_position(method,path,*,query="",body=None):
            if path==ROOT+"/positions": return [{"contract":"BTC_USDT","size":"1"}]
            return fake_gateway(method,path,query=query,body=body)
        with patch.dict("os.environ",{"GATE_TESTNET_PREPARE_HEDGE_MODE":"true"}):
            result=prepare_testnet_hedge_mode(request=open_position,writer=lambda:None)
        self.assertEqual(result["state"],"OPEN_POSITIONS_OR_ORDERS_BLOCK_MODE_SWITCH")

    def test_flat_account_switch_requires_readback(self):
        changed=[False]
        def gateway(method,path,*,query="",body=None):
            if path==ROOT+"/accounts" and changed[0]:
                return {"currency":"USDT","in_dual_mode":True,"position_mode":"dual"}
            return fake_gateway(method,path,query=query,body=body)
        def writer():
            changed[0]=True
            return {"currency":"USDT","position_mode":"dual"}
        with patch.dict("os.environ",{"GATE_TESTNET_PREPARE_HEDGE_MODE":"true"}):
            result=prepare_testnet_hedge_mode(request=gateway,writer=writer)
        self.assertEqual(result["state"],"TESTNET_DUAL_MODE_CONFIRMED")

    def test_mode_switch_failure_never_reports_success(self):
        with patch.dict("os.environ",{"GATE_TESTNET_PREPARE_HEDGE_MODE":"true"}):
            result=prepare_testnet_hedge_mode(request=fake_gateway,
                                             writer=lambda:{"currency":"USDT"})
        self.assertEqual(result["state"],"MODE_SWITCH_READBACK_FAILED")

if __name__=="__main__":
    unittest.main()
