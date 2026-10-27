import unittest
from unittest.mock import patch
from testnet_readiness import inspect_testnet_readiness, CHECK_CONTRACT
from gate_testnet_broker import ROOT

def gateway(method,path,*,query="",body=None):
    if method!="GET" or body is not None:
        raise AssertionError("read-only probe must never submit orders")
    if path==ROOT+"/accounts":
        return {"currency":"USDT","available":"100","in_dual_mode":True}
    if path==ROOT+"/positions":
        return []
    if path==ROOT+"/contracts/"+CHECK_CONTRACT:
        return {"name":CHECK_CONTRACT,"mark_price":"100","quanto_multiplier":"0.001",
                "order_size_min":1,"order_size_max":100,"leverage_max":10}
    if path==ROOT+"/get_leverage/"+CHECK_CONTRACT:
        return {"Lever":"5"}
    if path==ROOT+"/price_orders":
        return []
    raise AssertionError("unknown query: "+path)

class TestReadiness(unittest.TestCase):
    def test_missing_credentials_is_blocked_without_network(self):
        with patch.dict("os.environ",{"GATE_TESTNET_API_KEY":"","GATE_TESTNET_API_SECRET":""}):
            def forbid(*args,**kwargs): raise AssertionError("network prohibited")
            result=inspect_testnet_readiness(request=forbid)
        self.assertFalse(result["ready"])
        self.assertIn("TESTNET_API_CREDENTIALS_MISSING",result["blockers"])

    def test_verified_readback_is_ready_without_order_submission(self):
        with patch.dict("os.environ",{"GATE_TESTNET_API_KEY":"fake","GATE_TESTNET_API_SECRET":"fake"}):
            result=inspect_testnet_readiness(request=gateway)
        self.assertTrue(result["ready"])
        self.assertTrue(result["native_protection_readback"])
        self.assertFalse(result["order_submitted"])

    def test_dual_mode_must_be_confirmed(self):
        def wrong_mode(method,path,*,query="",body=None):
            result=gateway(method,path,query=query,body=body)
            if path==ROOT+"/accounts":
                return {**result,"in_dual_mode":False}
            return result
        with patch.dict("os.environ",{"GATE_TESTNET_API_KEY":"fake","GATE_TESTNET_API_SECRET":"fake"}):
            result=inspect_testnet_readiness(request=wrong_mode)
        self.assertFalse(result["ready"])
        self.assertIn("HEDGE_DUAL_MODE_NOT_CONFIRMED",result["blockers"])

    def test_high_leverage_blocks_trial(self):
        def overleveraged(method,path,*,query="",body=None):
            if path==ROOT+"/get_leverage/"+CHECK_CONTRACT:
                return {"Lever":"20"}
            return gateway(method,path,query=query,body=body)
        with patch.dict("os.environ",{"GATE_TESTNET_API_KEY":"fake","GATE_TESTNET_API_SECRET":"fake"}):
            result=inspect_testnet_readiness(request=overleveraged)
        self.assertFalse(result["ready"])
        self.assertIn("TESTNET_LEVERAGE_NOT_WITHIN_5X",result["blockers"])

    def test_missing_protection_readback_blocks_trial(self):
        def missing_protection(method,path,*,query="",body=None):
            if path==ROOT+"/price_orders":
                return {"not":"a list"}
            return gateway(method,path,query=query,body=body)
        with patch.dict("os.environ",{"GATE_TESTNET_API_KEY":"fake","GATE_TESTNET_API_SECRET":"fake"}):
            result=inspect_testnet_readiness(request=missing_protection)
        self.assertFalse(result["ready"])
        self.assertIn("PROTECTION_READBACK_UNAVAILABLE",result["blockers"])

if __name__=="__main__":
    unittest.main()
