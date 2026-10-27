import unittest
from testnet_unknown_exposure import _snapshot,unknown_exposure_gate
from gate_testnet_broker import ROOT

CONTRACT="AIN_USDT"

def read(method,path,*,query=""):
    assert method=="GET"
    if path==ROOT+"/positions":
        return [{"contract":"ETH_USDT","size":1},
                {"contract":CONTRACT,"size":0}]
    if path==ROOT+"/orders" and query=="status=open":
        return []
    if path==ROOT+"/price_orders" and query=="status=open":
        return [{"initial":{"contract":"ETH_USDT"},"status":"open"}]
    raise AssertionError("Unexpected exchange request")

class UnknownExposureTests(unittest.TestCase):
    def test_flat_readback_requires_three_fresh_endpoints(self):
        self.assertEqual(_snapshot(read,CONTRACT),"NO_CURRENT_EXPOSURE")
    def test_pending_position_blocks(self):
        def exposed(method,path,*,query=""):
            if path==ROOT+"/positions":return [{"contract":CONTRACT,"size":-1}]
            return read(method,path,query=query)
        self.assertEqual(_snapshot(exposed,CONTRACT),"CURRENT_POSITION_EXPOSURE")
    def test_open_order_blocks(self):
        def open_order(method,path,*,query=""):
            if path==ROOT+"/orders":return [{"contract":CONTRACT,"status":"open"}]
            return read(method,path,query=query)
        self.assertEqual(_snapshot(open_order,CONTRACT),"OPEN_ENTRY_ORDER")
    def test_native_trigger_blocks(self):
        def t(method,path,*,query=""):
            if path==ROOT+"/price_orders":
                return [{"initial":{"contract":CONTRACT},"status":"open"}]
            return read(method,path,query=query)
        self.assertEqual(_snapshot(t,CONTRACT),"OPEN_PROTECTIVE_OR_TRIGGER_ORDER")
    def test_bad_exchange_readback_fails_closed(self):
        def bad(method,path,*,query=""):
            if path==ROOT+"/positions":return {}
            return read(method,path,query=query)
        with self.assertRaises(ValueError):
            _snapshot(bad,CONTRACT)
        self.assertFalse(unknown_exposure_gate(database_url=""))

if __name__=="__main__":unittest.main()
