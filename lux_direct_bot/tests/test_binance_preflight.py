"""No-money Binance USD-M preflight tests; fake signed-GET responses only."""
import importlib
import os
from unittest.mock import patch

from binance_preflight import snapshot_read_only


class FakeBinance:
    def __init__(self, **overrides):
        self.calls=[]
        self.answers={
            "/fapi/v3/balance":[{"asset":"USDT","balance":"120","availableBalance":"98"}],
            "/fapi/v3/account":{"assets":[{"asset":"USDT","walletBalance":"120",
                 "availableBalance":"98","marginBalance":"118"}]},
            "/fapi/v1/accountConfig":{"multiAssetsMargin":False,
                "canTrade":True,"dualSidePosition":False},
            "/fapi/v3/positionRisk":[{"symbol":"BNBUSDT","positionAmt":"0"},
                                     {"symbol":"BIRBUSDT","positionAmt":"0"}],
            "/fapi/v1/openOrders":[],
            "/fapi/v1/openAlgoOrders":[],
            "/fapi/v1/positionSide/dual":{"dualSidePosition":False},
        }
        self.answers.update(overrides)
    def get(self,path):
        self.calls.append(path)
        return self.answers[path]


def test_without_keys_fails_closed(monkeypatch):
    monkeypatch.delenv("BINANCE_API_KEY",raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET",raising=False)
    r=snapshot_read_only()
    assert r["status"]=="NOT_CONFIGURED"
    assert r["orders_enabled"] is False


def test_all_5_signed_gets_required_and_no_order_dispatch(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY","fake")
    monkeypatch.setenv("BINANCE_API_SECRET","fake")
    c=FakeBinance()
    r=snapshot_read_only(c)
    assert r["status"]=="CONNECTED_READ_ONLY"
    assert r["exchange_flat"] is True
    assert r["position_mode"]=="ONE_WAY"
    assert r["open_positions"]==r["open_orders"]==r["open_conditional_orders"]==0
    assert r["available_usdt"]=="98"
    assert r["futures_equity_usdt"]=="118"
    assert r["equity_basis"]=="SIGNED_USDT_FUTURES_MARGIN_BALANCE"
    assert r["risk_budget_supported"] is True
    assert r["multi_assets_margin"] is False
    assert r["exchange_reports_can_trade"] is True
    assert r["orders_enabled"] is False and r["can_trade"] is False
    assert r["acceptance_certified"] is False
    assert c.calls==list(c.answers)
    assert all(path.startswith("/fapi/") for path in c.calls)


def test_nonempty_positions_and_native_triggers_not_called_empty(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY","fake")
    monkeypatch.setenv("BINANCE_API_SECRET","fake")
    cases=[
        {"/fapi/v3/positionRisk":[{"symbol":"BNBUSDT","positionAmt":"-2"}]},
        {"/fapi/v1/openOrders":[{"symbol":"BNBUSDT","orderId":123}]},
        {"/fapi/v1/openAlgoOrders":[{"symbol":"BIRBUSDT","algoId":456}]},
    ]
    for case in cases:
        r=snapshot_read_only(FakeBinance(**case))
        assert r["status"]=="CONNECTED_READ_ONLY"
        assert r["exchange_flat"] is False
        assert r["orders_enabled"] is False


def test_missing_any_response_or_bad_type_fails_closed(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY","fake")
    monkeypatch.setenv("BINANCE_API_SECRET","fake")
    cases=[
        {"/fapi/v1/openAlgoOrders":None},
        {"/fapi/v1/positionSide/dual":{"dualSidePosition":"false"}},
        {"/fapi/v3/balance":[]},
        {"/fapi/v3/account":{"assets":[]}},
        {"/fapi/v1/accountConfig":None},
        {"/fapi/v1/accountConfig":{"multiAssetsMargin":"false","canTrade":True,
            "dualSidePosition":False}},
        {"/fapi/v1/accountConfig":{"multiAssetsMargin":False,"canTrade":True,
            "dualSidePosition":True}},
        {"/fapi/v3/account":{"assets":[{"asset":"USDT","walletBalance":"120",
            "availableBalance":"98","marginBalance":"NaN"}]}},
        {"/fapi/v3/account":{"assets":[{"asset":"USDT","walletBalance":"119",
            "availableBalance":"98","marginBalance":"118"}]}},
        {"/fapi/v3/balance":[{"asset":"USDT","balance":"NaN","availableBalance":"100"}]},
        {"/fapi/v3/positionRisk":[{"symbol":"BNBUSDT"}]},
        {"/fapi/v3/positionRisk":[{"symbol":"BNBUSDT","positionAmt":"nan"}]},
        {"/fapi/v1/openOrders":[{}]},
    ]
    for case in cases:
        r=snapshot_read_only(FakeBinance(**case))
        assert r["status"]=="READBACK_UNVERIFIED",case
        assert r["orders_enabled"] is False
        assert r["can_trade"] is False


def test_auth_required_for_private_binance_readback(monkeypatch,tmp_path):
    monkeypatch.setenv("WEBHOOK_SECRET","private-webhook-test")
    monkeypatch.setenv("ADMIN_TOKEN","private-admin-test")
    monkeypatch.setenv("DB_PATH",str(tmp_path/"preflight-test.sqlite"))
    import app
    bot=importlib.reload(app)
    from fastapi.testclient import TestClient
    with TestClient(bot.app) as client:
        assert client.get("/binance/preflight").status_code==403
        with patch.object(bot,"snapshot_read_only",return_value={
            "status":"CONNECTED_READ_ONLY","orders_enabled":False,
            "can_trade":False,"exchange_flat":True}):
            r=client.get("/binance/preflight",
                headers={"X-Admin-Token":"private-admin-test"})
        assert r.status_code==200
        assert r.headers["cache-control"]=="private, no-store"
        assert r.json()["orders_enabled"] is False

def test_signed_multi_asset_and_disabled_exchange_permissions_stay_blocked(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY","fake")
    monkeypatch.setenv("BINANCE_API_SECRET","fake")
    multi=snapshot_read_only(FakeBinance(**{
        "/fapi/v1/accountConfig":{"multiAssetsMargin":True,
             "canTrade":True,"dualSidePosition":False}
    }))
    assert multi["status"]=="CONNECTED_READ_ONLY"
    assert multi["multi_assets_margin"] is True
    assert multi["risk_budget_supported"] is False
    assert multi["orders_enabled"] is False
    halted=snapshot_read_only(FakeBinance(**{
        "/fapi/v1/accountConfig":{"multiAssetsMargin":False,
             "canTrade":False,"dualSidePosition":False}
    }))
    assert halted["status"]=="CONNECTED_READ_ONLY"
    assert halted["exchange_reports_can_trade"] is False
    assert halted["can_trade"] is False
