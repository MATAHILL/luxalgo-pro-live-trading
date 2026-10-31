"""Binance public endpoint diagnostics never imply credential status."""
import httpx
from exchange_diagnostics import get_binance_public_status

def test_binance_public_http_error_reports_code_not_key_failure():
    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(
            lambda r:httpx.Response(451,json={"code":-1})),**kwargs)
    out=get_binance_public_status(client_factory=factory)
    assert out["public_api"]=="UNAVAILABLE"
    assert out["http_status"]==451
    assert out["api_credentials"]=="NOT_CHECKED"
    assert out["orders_enabled"] is False
    assert "api_key" not in out and "signature" not in out

def test_public_success_does_not_prove_account_signed_auth():
    def handler(req):
        if req.url.path=="/fapi/v1/time":
            return httpx.Response(200,json={"serverTime":1793500000000})
        if req.url.path=="/fapi/v1/exchangeInfo":
            return httpx.Response(200,json={"symbols":[
                {"symbol":"XRPUSDT","status":"TRADING","filters":[]}]})
        return httpx.Response(404)
    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(handler),**kwargs)
    out=get_binance_public_status(client_factory=factory)
    assert out["public_api"]=="REACHABLE"
    assert out["xrp_usdt"]=="TRADING"
    assert out["api_credentials"]=="NOT_CONNECTED"
    assert out["orders_enabled"] is False
