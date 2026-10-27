import unittest
from unittest.mock import patch
from testnet_leverage_prepare import prepare_testnet_leverage_5x, ROOT, CONTRACT

def gateway(method,path,*,query="",body=None):
    assert method=="GET" and body is None
    if path==ROOT+"/accounts":
        return {"currency":"USDT","in_dual_mode":True}
    if path in (ROOT+"/positions",ROOT+"/orders",ROOT+"/price_orders"):
        return []
    if path==ROOT+"/get_leverage/"+CONTRACT:
        return {"Lever":"10"}
    raise ValueError(path)

class TestLeveragePreparation(unittest.TestCase):
    def test_disabled_never_reads_or_writes(self):
        with patch.dict("os.environ",{"GATE_TESTNET_PREPARE_LEVERAGE_5X":"false"}):
            self.assertEqual(prepare_testnet_leverage_5x(request=lambda *a,**k:1,
                                                        writer=lambda:1)["state"],
                             "LEVERAGE_SETUP_DISABLED")
    def test_existing_order_prevents_post(self):
        def with_order(method,path,*,query="",body=None):
            if path==ROOT+"/orders":return [{"id":1}]
            return gateway(method,path,query=query,body=body)
        with patch.dict("os.environ",{"GATE_TESTNET_PREPARE_LEVERAGE_5X":"true"}):
            def forbidden(): raise AssertionError("no update when open order")
            self.assertEqual(prepare_testnet_leverage_5x(request=with_order,writer=forbidden)["state"],
                             "TESTNET_ACCOUNT_NOT_FLAT")
    def test_verified_both_sides(self):
        state={"lever":"10","count":0}
        def request(method,path,*,query="",body=None):
            if path==ROOT+"/get_leverage/"+CONTRACT:return {"Lever":state["lever"]}
            return gateway(method,path,query=query,body=body)
        def write():
            state["count"]+=1;state["lever"]="5";return {}
        with patch.dict("os.environ",{"GATE_TESTNET_PREPARE_LEVERAGE_5X":"true"}):
            r=prepare_testnet_leverage_5x(request=request,writer=write)
        self.assertEqual(r["state"],"TESTNET_LEVERAGE_5X_VERIFIED")
        self.assertEqual(state["count"],1)
    def test_unverified_second_side_does_not_mark_success(self):
        state={"called":False}
        def request(method,path,*,query="",body=None):
            if path==ROOT+"/get_leverage/"+CONTRACT:
                if not state["called"]:return {"Lever":"10"}
                return {"Lever":"6" if "dual_short" in query else "5"}
            return gateway(method,path,query=query,body=body)
        with patch.dict("os.environ",{"GATE_TESTNET_PREPARE_LEVERAGE_5X":"true"}):
            r=prepare_testnet_leverage_5x(request=request,writer=lambda:state.update(called=True))
        self.assertEqual(r["state"],"LEVERAGE_5X_READBACK_FAILED")
if __name__=="__main__":unittest.main()
