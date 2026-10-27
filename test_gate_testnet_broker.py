import unittest
from unittest.mock import patch, Mock
from gate_testnet_execution import atomic_protected_entry_payload, TestnetExecutionDisabled
from gate_testnet_broker import GateFuturesTestnetBroker, _request_testnet, ROOT
from testnet_protected_coordinator import TestnetIntent

INTENT=TestnetIntent("evt-unit123","BTC_USDT","LONG","100","90","110","t-brokerunit01")
ORDER=atomic_protected_entry_payload("BTC_USDT","LONG","100","90","110","t-brokerunit01")

def fake_testnet_request(method,path,*,body=None,query=""):
    if method=="GET" and path==ROOT+"/accounts":
        return {"currency":"USDT","in_dual_mode":True,"available":"100"}
    if method=="GET" and path==ROOT+"/contracts/BTC_USDT":
        return {"name":"BTC_USDT","in_delisting":False,"mark_price":"100",
            "quanto_multiplier":"0.001","order_size_min":"1","order_size_max":"100","leverage_max":"10"}
    if method=="GET" and path==ROOT+"/get_leverage/BTC_USDT":
        return {"Lever":"5"}
    if method=="GET" and path==ROOT+"/positions":
        return []
    if method=="POST" and path==ROOT+"/orders":
        return {"id":123,"contract":"BTC_USDT"}
    raise AssertionError("Unexpected broker request")

class TestnetBrokerTests(unittest.TestCase):
    def test_requires_testnet_flag_for_preflight(self):
        broker=GateFuturesTestnetBroker(fake_testnet_request)
        with patch.dict("os.environ",{"GATE_TESTNET_PROTECTED_EXECUTION_ENABLED":"false"}):
            self.assertFalse(broker.preflight(INTENT,ORDER))

    def test_preflight_and_single_use_testnet_submission(self):
        broker=GateFuturesTestnetBroker(fake_testnet_request)
        with patch.dict("os.environ",{"GATE_TESTNET_PROTECTED_EXECUTION_ENABLED":"true"}):
            self.assertTrue(broker.preflight(INTENT,ORDER))
            self.assertEqual(broker.submit(ORDER)["id"],123)
            with self.assertRaises(TestnetExecutionDisabled):
                broker.submit(ORDER)

    def test_leverage_above_five_rejected(self):
        def unsafe_request(method,path,*,body=None,query=""):
            if "get_leverage" in path:
                return {"Lever":"20"}
            return fake_testnet_request(method,path,body=body,query=query)
        with patch.dict("os.environ",{"GATE_TESTNET_PROTECTED_EXECUTION_ENABLED":"true"}):
            broker=GateFuturesTestnetBroker(unsafe_request)
            self.assertFalse(broker.preflight(INTENT,ORDER))

    def test_unsupported_testnet_symbol_exposes_stage_not_credentials(self):
        from urllib.error import HTTPError
        def fake(method,path,*,body=None,query=""):
            if path==ROOT+"/contracts/BTC_USDT":
                raise HTTPError("https://testnet.invalid",404,"not found",{},None)
            return fake_testnet_request(method,path,body=body,query=query)
        broker=GateFuturesTestnetBroker(fake)
        with patch.dict("os.environ",{"GATE_TESTNET_PROTECTED_EXECUTION_ENABLED":"true"}):
            self.assertFalse(broker.preflight(INTENT,ORDER))
        self.assertEqual(broker.last_preflight_reason,"CONTRACT_HTTP_404")
        self.assertEqual(broker.last_preflight_stage,"CONTRACT")
        self.assertNotIn("https://",broker.last_preflight_reason)

    def test_stale_signal_reference_reports_exact_rejection(self):
        old=TestnetIntent(INTENT.signal_id,INTENT.contract,INTENT.side,
            "98","90","110",INTENT.client_id)
        payload=atomic_protected_entry_payload("BTC_USDT","LONG","98","90","110",old.client_id)
        broker=GateFuturesTestnetBroker(fake_testnet_request)
        with patch.dict("os.environ",{"GATE_TESTNET_PROTECTED_EXECUTION_ENABLED":"true"}):
            self.assertFalse(broker.preflight(old,payload))
        self.assertEqual(broker.last_preflight_reason,
                         "REFERENCE_PRICE_OUTSIDE_0_5_PERCENT_MARK_BAND")

    def test_preflight_rejects_mutated_stop(self):
        broker=GateFuturesTestnetBroker(fake_testnet_request)
        with patch.dict("os.environ",{"GATE_TESTNET_PROTECTED_EXECUTION_ENABLED":"true"}):
            self.assertFalse(broker.preflight(INTENT,{**ORDER,"tpsl_sl_trigger_price":"89"}))

    def test_exchange_transport_flag_is_off(self):
        with patch.dict("os.environ",{"GATE_TESTNET_PROTECTED_EXECUTION_ENABLED":"false"}):
            with patch("gate_testnet_broker.urlopen") as network:
                with self.assertRaises(TestnetExecutionDisabled):
                    _request_testnet("POST",ROOT+"/orders",body=ORDER)
                network.assert_not_called()

    def test_exchange_transport_prohibits_live_or_spot_routes(self):
        with self.assertRaises(ValueError):
            _request_testnet("GET","/api/v4/spot/orders")
        with self.assertRaises(ValueError):
            _request_testnet("POST","/api/v4/futures/btc/orders",body=ORDER)

    def test_testnet_close_accepts_filled_order_and_confirmed_flat_position(self):
        calls=[]
        def fake(method,path,*,body=None,query=""):
            calls.append((method,path))
            if method=="POST" and path==ROOT+"/orders":
                self.assertTrue(body["reduce_only"])
                self.assertEqual(body["auto_size"],"close_long")
                return {"id":999}
            if method=="GET" and path==ROOT+"/orders/999":
                return {"id":999,"contract":"BTC_USDT","status":"finished","finish_as":"filled"}
            if method=="GET" and path==ROOT+"/positions":
                return []  # Gate may omit positions closed to zero
            raise AssertionError((method,path))
        broker=GateFuturesTestnetBroker(fake)
        self.assertTrue(broker.emergency_close(INTENT))
        self.assertEqual(calls.count(("GET",ROOT+"/positions")),2)

    def test_testnet_close_fails_if_any_remaining_position(self):
        def fake(method,path,*,body=None,query=""):
            if method=="POST":return {"id":999}
            if path==ROOT+"/orders/999":
                return {"id":999,"contract":"BTC_USDT","status":"finished","finish_as":"filled"}
            if path==ROOT+"/positions":
                return [{"contract":"BTC_USDT","mode":"dual_long","size":1}]
            raise AssertionError((method,path))
        self.assertFalse(GateFuturesTestnetBroker(fake).emergency_close(INTENT))

    def test_native_attached_trigger_verification(self):
        broker=GateFuturesTestnetBroker(fake_testnet_request)
        order={"id":123,"contract":"BTC_USDT","status":"finished","finish_as":"filled",
               **ORDER}
        positions=[{"contract":"BTC_USDT","mode":"dual_long","size":1}]
        triggers=[
          {"id":987,"status":"open","order_type":"close-long-order","me_order_id":123,
           "initial":{"contract":"BTC_USDT"},"trigger":{"price":"90","rule":2}},
          {"id":988,"status":"open","order_type":"close-long-order","me_order_id":123,
           "initial":{"contract":"BTC_USDT"},"trigger":{"price":"110","rule":1}}
        ]
        self.assertTrue(broker.verify_protection(order,positions,triggers,INTENT,ORDER))
        self.assertEqual(broker.last_protection_evidence["native_protection_ids"],["987","988"])
        self.assertEqual(broker.last_protection_evidence["entry_finish_as"],"filled")
        self.assertFalse(broker.verify_protection(order,positions,triggers[:1],INTENT,ORDER))

if __name__=="__main__":
    unittest.main()
