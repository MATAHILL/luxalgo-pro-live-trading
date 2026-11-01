"""Tests for private signed-GET account evidence, never a real Binance connection."""
import importlib
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from exchange_verification import signed_exchange_verification

ACCOUNT={
    "totalMarginBalance": "400.00",
    "assets":[{"asset":"USDT","walletBalance":"400.00",
               "marginBalance":"400.00","availableBalance":"400.00"}],
}
ANSWERS={
    "/fapi/v3/account": ACCOUNT,
    "/fapi/v1/accountConfig": {"multiAssetsMargin":False, "dualSidePosition":False, "canTrade":True},
    "/fapi/v3/positionRisk": [],
    "/fapi/v1/openOrders": [],
    "/fapi/v1/openAlgoOrders": [],
    "/fapi/v1/positionSide/dual": {"dualSidePosition":False},
    "/fapi/v1/symbolConfig":[{"symbol":"XRPUSDT","marginType":"CROSSED","leverage":5}],
}
class Fake:
    def __init__(self, changes=None):
        self.answers=ANSWERS | (changes or {})
        self.calls=[]
    def get(self,path,params=None):
        self.calls.append((path,params))
        return self.answers[path]

@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY","fixture-key-not-real")
    monkeypatch.setenv("BINANCE_API_SECRET","fixture-secret-not-real")

def test_full_signed_get_fixture_and_equity_not_wallet_deposit():
    c=Fake()
    result=signed_exchange_verification(c)
    assert [x[0] for x in c.calls]==list(ANSWERS)
    assert c.calls[-1]==("/fapi/v1/symbolConfig",{"symbol":"XRPUSDT"})
    assert result["status"]=="CONNECTED_READ_ONLY"
    assert result["usdt_equity"]=="400.00"
    assert result["modeled_loss_budget_3pct"]=="12.0000"
    assert result["position_mode"]=="ONE_WAY"
    assert result["target_margin_type"]=="CROSSED"
    assert result["target_leverage"]==5
    assert result["orders_enabled"] is False
    assert result["can_trade"] is False
    assert result["acceptance_certified"] is False
    assert result["orders_sent"]==0
    assert "LIVE_ORDER_SENDER_HARD_LOCKED" in result["blocked_reasons"]

def test_usdt_equity_includes_unrealized_loss():
    c=Fake({"/fapi/v3/account":{
        "totalMarginBalance":"395",
        "assets":[{"asset":"USDT","walletBalance":"400","marginBalance":"395","availableBalance":"350"}]
    }})
    r=signed_exchange_verification(c)
    assert r["usdt_equity"]=="395"
    assert r["modeled_loss_budget_3pct"]=="11.85"
    assert r["usdt_wallet"]=="400"

@pytest.mark.parametrize("changes,reason",[
    ({"/fapi/v1/accountConfig":{"multiAssetsMargin":False,"dualSidePosition":True,"canTrade":True},
      "/fapi/v1/positionSide/dual":{"dualSidePosition":True}},"HEDGE_MODE_NOT_SUPPORTED_BY_EXECUTION_PLAN"),
    ({"/fapi/v1/symbolConfig":[{"symbol":"XRPUSDT","marginType":"ISOLATED","leverage":5}]},"TARGET_SYMBOL_NOT_CROSS_MARGIN"),
    ({"/fapi/v1/symbolConfig":[{"symbol":"XRPUSDT","marginType":"CROSSED","leverage":20}]},"TARGET_SYMBOL_NOT_SET_TO_5X"),
    ({"/fapi/v3/positionRisk":[{"symbol":"XRPUSDT","positionAmt":"10"}]},"EXCHANGE_NOT_FLAT_EXISTING_POSITIONS_OR_ORDERS"),
    ({"/fapi/v1/openOrders":[{"symbol":"ETHUSDT","orderId":12}]},"EXCHANGE_NOT_FLAT_EXISTING_POSITIONS_OR_ORDERS"),
    ({"/fapi/v1/openAlgoOrders":[{"symbol":"BNBUSDT","algoId":9}]},"EXCHANGE_NOT_FLAT_EXISTING_POSITIONS_OR_ORDERS"),
    ({"/fapi/v1/accountConfig":{"multiAssetsMargin":True,"dualSidePosition":False,"canTrade":True}},"MULTI_ASSETS_EQUITY_POLICY_NOT_SUPPORTED"),
    ({"/fapi/v1/accountConfig":{"multiAssetsMargin":False,"dualSidePosition":False,"canTrade":False}},"BINANCE_REPORTS_TRADING_NOT_ALLOWED"),
])
def test_unfavorable_exchange_state_never_claimed_ready(changes,reason):
    r=signed_exchange_verification(Fake(changes))
    assert r["status"]=="CONNECTED_READ_ONLY"
    assert reason in r["blocked_reasons"]
    assert r["can_trade"] is False

@pytest.mark.parametrize("changes",[
    {"/fapi/v3/account":ACCOUNT | {"assets":[]}},
    {"/fapi/v3/account":ACCOUNT | {"totalMarginBalance":"999"}},
    {"/fapi/v3/account":ACCOUNT | {"assets":[{"asset":"USDT","walletBalance":"0","marginBalance":"NaN","availableBalance":"0"}]}},
    {"/fapi/v1/symbolConfig":[]},
    {"/fapi/v1/positionSide/dual":{"dualSidePosition":"false"}},
    {"/fapi/v1/accountConfig":{"multiAssetsMargin":False,"dualSidePosition":"false","canTrade":True}},
    {"/fapi/v1/accountConfig":{"dualSidePosition":False,"canTrade":True}},
    {"/fapi/v3/positionRisk":[{"symbol":"XRPUSDT"}]},
    {"/fapi/v1/openAlgoOrders":None},
    {"/fapi/v1/openOrders":[{}]},
])
def test_missing_or_inconsistent_snapshot_fails_closed(changes):
    r=signed_exchange_verification(Fake(changes))
    assert r["status"]=="READBACK_UNVERIFIED"
    assert r["can_trade"] is False and r["orders_sent"]==0
    assert "usdt_equity" not in r

def test_no_exchange_credentials_and_other_symbol_not_certified(monkeypatch):
    monkeypatch.delenv("BINANCE_API_KEY")
    assert signed_exchange_verification()["status"]=="NOT_CONFIGURED"
    assert signed_exchange_verification(symbol="BNBUSDT")["status"]=="BLOCKED"

def test_private_readback_cannot_be_queried_unauthenticated(monkeypatch,tmp_path):
    monkeypatch.setenv("WEBHOOK_SECRET","test-exchange-hook")
    monkeypatch.setenv("ADMIN_TOKEN","test-exchange-admin")
    monkeypatch.setenv("DB_PATH",str(tmp_path/"exchange.sqlite"))
    import app
    bot=importlib.reload(app)
    with TestClient(bot.app) as c:
        assert c.get("/operator/exchange-verification").status_code==403
        with patch.object(bot,"signed_exchange_verification",
                          return_value={"status":"CONNECTED_READ_ONLY","orders_enabled":False}):
            result=c.get("/operator/exchange-verification",
                headers={"X-Admin-Token":"test-exchange-admin"})
        assert result.status_code==200
        assert result.headers["Cache-Control"]=="private, no-store"
        assert result.json()["orders_enabled"] is False


def test_one_shot_startup_log_never_exposes_balances_or_credentials(monkeypatch,caplog):
    import app
    bot=importlib.reload(app)
    fake={
        "status":"CONNECTED_READ_ONLY", "usdt_equity":"997.321",
        "modeled_loss_budget_3pct":"29.91963",
        "api_secret":"fixture-secret-not-real", "orders_enabled":False,
    }
    import logging
    with patch.object(bot,"signed_exchange_verification",return_value=fake):
        with caplog.at_level(logging.INFO,logger="uvicorn.error"):
            bot._startup_signed_exchange_diagnostic()
    combined="\n".join(x.message for x in caplog.records)
    assert "DIRECT_BINANCE_SIGNED_GET_AUDIT status=CONNECTED_READ_ONLY" in combined
    assert "997.321" not in combined
    assert "29.91963" not in combined
    assert "fixture-secret" not in combined
    assert "orders_enabled=FALSE" in combined


@pytest.mark.parametrize("failed_path,code,binance_code",[
    ("/fapi/v3/account",403,-2015),
    ("/fapi/v1/openAlgoOrders",400,-1102),
    ("/fapi/v1/symbolConfig",429,-1003),
])
def test_http_failure_identifies_endpoint_not_secrets(
        failed_path, code, binance_code, monkeypatch, caplog):
    import httpx
    class Rejecting(Fake):
        def get(self,path,params=None):
            if path==failed_path:
                url="https://fapi.binance.com"+path+"?timestamp=123&signature=leak-me-not"
                request=httpx.Request("GET",url,headers={"X-MBX-APIKEY":"sensitive-key"})
                response=httpx.Response(code,json={"code":binance_code,
                    "msg":"Secret user account number should never be returned"},
                    request=request)
                response.raise_for_status()
            return super().get(path,params)
    result=signed_exchange_verification(Rejecting())
    assert result["status"]=="READBACK_UNVERIFIED"
    assert result["failed_endpoint"]==failed_path
    assert result["http_status"]==code
    assert result["binance_code"]==binance_code
    assert result["orders_enabled"] is False and result["orders_sent"]==0
    shown=str(result)
    assert "signature" not in shown
    assert "leak-me-not" not in shown
    assert "sensitive-key" not in shown
    assert "account number" not in shown
    import app
    # Uvicorn logger may not propagate into pytest caplog; inspect the
    # formatting template and sanitized arguments at the logger boundary.
    with patch.object(app,"signed_exchange_verification",return_value=result), \
         patch.object(app.logging,"getLogger") as logger:
        app._startup_signed_exchange_diagnostic()
    args=logger.return_value.info.call_args.args
    template=args[0]
    message=template % args[1:]
    assert "endpoint="+failed_path in message
    assert "http_status="+str(code) in message
    assert "binance_code="+str(binance_code) in message
    assert "sensitive-key" not in message
    assert "leak-me-not" not in message

def test_invalid_status_body_redacted(monkeypatch):
    import httpx
    class Rejecting(Fake):
        def get(self,path,params=None):
            request=httpx.Request("GET","https://fapi.binance.com"+path+
                "?signature=123private")
            response=httpx.Response(400,text="private error credentials",request=request)
            response.raise_for_status()
    result=signed_exchange_verification(Rejecting())
    assert result["http_status"]==400
    assert result["binance_code"] is None
    assert "private" not in str(result)


def test_account_v3_does_not_need_deprecated_configuration_field():
    # Binance's official v3 account response excludes multiAssetsMargin.
    assert "multiAssetsMargin" not in ACCOUNT
    state=signed_exchange_verification(Fake())
    assert state["status"]=="CONNECTED_READ_ONLY"
    assert state["multi_assets_margin"] is False
    assert state["can_trade"] is False
