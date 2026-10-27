import unittest
from unittest.mock import patch
from testnet_contract_leverage import prepare_contract_5x
from gate_testnet_broker import ROOT

CONTRACT="AVAX_USDT"
FLAGS={"GATE_TESTNET_LEVERAGE_AUTO_SETUP":"true",
 "GATE_TESTNET_PROTECTED_EXECUTION_ENABLED":"true",
 "LIVE_TRADING_ENABLED":"false","ORDERS_ENABLED":"false"}

class LeverageSetupTests(unittest.TestCase):
    def source(self,store,*,position=None,order=None,trigger=None):
        def read(method,path,*,query="",body=None):
            self.assertEqual(method,"GET")
            if path==ROOT+"/accounts":
                return {"currency":"USDT","in_dual_mode":True}
            if path==ROOT+"/contracts/"+CONTRACT:
                return {"name":CONTRACT,"leverage_max":"20","in_delisting":False}
            if path==ROOT+"/positions":return position if position is not None else []
            if path==ROOT+"/orders":return order if order is not None else []
            if path==ROOT+"/price_orders":return trigger if trigger is not None else []
            if path==ROOT+"/get_leverage/"+CONTRACT:return {"Lever":store["value"]}
            raise AssertionError(path)
        return read

    def test_absent_switch_does_not_access_exchange(self):
        with patch.dict("os.environ",{**FLAGS,"GATE_TESTNET_LEVERAGE_AUTO_SETUP":"false"}):
            result=prepare_contract_5x(CONTRACT,read=lambda *a,**k:(_ for _ in ()).throw(
                AssertionError("should not read")),writer=lambda x:1)
        self.assertEqual(result["state"],"TESTNET_LEVERAGE_SETUP_DISABLED")

    def test_account_has_position_never_updates_leverage(self):
        state={"value":"20"}
        with patch.dict("os.environ",FLAGS):
            result=prepare_contract_5x(CONTRACT,
                read=self.source(state,position=[{"contract":"ETH_USDT","size":"1"}]),
                writer=lambda c:(_ for _ in ()).throw(AssertionError("unsafe write")))
        self.assertEqual(result["state"],"ACCOUNT_NOT_FLAT")

    def test_account_has_trigger_never_updates_leverage(self):
        state={"value":"20"}
        with patch.dict("os.environ",FLAGS):
            result=prepare_contract_5x(CONTRACT,
                read=self.source(state,trigger=[{"id":"2"}]),
                writer=lambda c:(_ for _ in ()).throw(AssertionError("unsafe write")))
        self.assertEqual(result["state"],"ACCOUNT_HAS_OPEN_ORDERS_OR_TRIGGERS")

    def test_single_leverage_update_verifies_both_hedge_sides(self):
        state={"value":"20","writes":0}
        def change(contract):
            self.assertEqual(contract,CONTRACT)
            state["writes"]+=1
            state["value"]="5"
        with patch.dict("os.environ",FLAGS):
            result=prepare_contract_5x(CONTRACT,read=self.source(state),writer=change)
        self.assertEqual(result["state"],"LEVERAGE_5X_EXCHANGE_VERIFIED")
        self.assertTrue(result["changed"])
        self.assertEqual(state["writes"],1)

    def test_no_resubmission_after_uncertain_writer(self):
        state={"value":"20","writes":0}
        def broken(contract):
            state["writes"]+=1
            raise TimeoutError("Post may have succeeded")
        with patch.dict("os.environ",FLAGS):
            result=prepare_contract_5x(CONTRACT,read=self.source(state),writer=broken)
        self.assertEqual(result["state"],"LEVERAGE_SETUP_TIMEOUTERROR")
        self.assertEqual(state["writes"],1)
        self.assertFalse(result["changed"])

if __name__=="__main__":
    unittest.main()
