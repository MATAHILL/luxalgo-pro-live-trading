import hashlib
import hmac
from urllib.parse import parse_qs

import httpx

from binance_account import read_account_snapshot, signed_read


def test_binance_credentials_absent(monkeypatch):
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    result = read_account_snapshot()
    assert result["status"] == "NOT_CONFIGURED"
    assert result["orders_enabled"] is False


def test_read_only_binance_signed_get(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "unit-test-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "unit-test-secret")
    paths = []

    def handler(request):
        assert request.method == "GET"
        assert request.url.host == "fapi.binance.com"
        assert request.headers["X-MBX-APIKEY"] == "unit-test-key"
        qs = parse_qs(request.url.query.decode())
        signature = qs.pop("signature")[0]
        body = "&".join(k + "=" + v[0] for k, v in qs.items())
        correct = hmac.new(b"unit-test-secret", body.encode(), hashlib.sha256).hexdigest()
        assert hmac.compare_digest(signature, correct)
        paths.append(request.url.path)
        if request.url.path == "/fapi/v3/balance":
            return httpx.Response(200, json=[{
                "asset": "USDT", "balance": "70.00", "availableBalance": "67.00"
            }])
        if request.url.path == "/fapi/v3/positionRisk":
            return httpx.Response(200, json=[{
                "symbol": "BTCUSDT", "positionAmt": "0.001", "positionSide": "BOTH",
                "entryPrice": "60000", "markPrice": "61000", "unRealizedProfit": "1.0",
                "leverage": "3"
            }, {"symbol": "ETHUSDT", "positionAmt": "0"}])
        return httpx.Response(500)

    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    result = read_account_snapshot(client_factory=factory)
    assert result["status"] == "CONNECTED_READ_ONLY"
    assert result["orders_enabled"] is False
    assert result["usdt_balance"] == "70.00"
    assert result["usdt_available"] == "67.00"
    assert len(result["positions"]) == 1
    assert paths == ["/fapi/v3/balance", "/fapi/v3/positionRisk"]


def test_write_routes_rejected():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500))) as c:
        try:
            signed_read(c, "/fapi/v1/order", "key", "secret")
        except ValueError:
            pass
        else:
            raise AssertionError("Unsafe Binance endpoint was not rejected")
