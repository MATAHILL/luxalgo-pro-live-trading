import hashlib
import hmac
from urllib.parse import parse_qs
import httpx
import pytest
from binance_transport import (
    BinanceReadOnly, BinanceTransportBlocked, signed_parameters,
    LIVE_ORDER_TRANSMISSION,
)

def test_binance_signing_matches_hmac_sha256():
    data=signed_parameters({"symbol":"XRPUSDT"}, "local-test-secret", timestamp=1234567)
    payload,signature=data.rsplit("&signature=",1)
    assert payload=="symbol=XRPUSDT&timestamp=1234567&recvWindow=5000"
    assert signature==hmac.new(b"local-test-secret",payload.encode(),hashlib.sha256).hexdigest()
    with pytest.raises(BinanceTransportBlocked):
        signed_parameters({"timestamp":100},"secret")

def test_network_is_get_only_and_xrp_only():
    requests=[]
    def handler(req):
        requests.append((req.method,req.url.path))
        assert req.method=="GET"
        assert req.url.host=="fapi.binance.com"
        assert req.headers.get("X-MBX-APIKEY")=="example-key"
        parsed=parse_qs(req.url.query.decode())
        assert parsed["symbol"]==["XRPUSDT"]
        return httpx.Response(200,json={"symbol":"XRPUSDT","status":"FILLED"})
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        gateway=BinanceReadOnly("example-key","secret",client=c)
        result=gateway.get("/fapi/v1/order",{"symbol":"XRPUSDT","origClientOrderId":"entry"},timestamp=1234567)
        assert result["status"]=="FILLED"
        with pytest.raises(BinanceTransportBlocked):
            gateway.get("/fapi/v1/order",{"symbol":"BTCUSDT"},timestamp=1234567)
        with pytest.raises(BinanceTransportBlocked):
            gateway.get("/fapi/v1/batchOrders",{"symbol":"XRPUSDT"},timestamp=1234567)
        assert requests==[("GET","/fapi/v1/order")]

def test_live_writes_always_blocked_without_http():
    calls=[]
    def handler(req):
        calls.append(req.method)
        raise AssertionError("No network calls for live dispatch")
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        gateway=BinanceReadOnly("key","secret",client=c)
        assert LIVE_ORDER_TRANSMISSION is False
        for method,args in (
            ("submit_entry",({"symbol":"XRPUSDT","side":"BUY"},)),
            ("submit_protection",({"type":"STOP_MARKET"},)),
            ("close_reduce_only",({"symbol":"XRPUSDT"},)),
            ("cancel_protection",("algo-1",)),
        ):
            with pytest.raises(BinanceTransportBlocked,match="locked"):
                getattr(gateway,method)(*args)
    assert calls==[]
