"""Safety contract: visible ON is dry-run intake, never a live order grant."""
import importlib
from fastapi.testclient import TestClient
from binance_transport import BinanceReadOnly, BinanceTransportBlocked, LIVE_ORDER_TRANSMISSION
from live_readiness_policy import MANDATORY_BARRIERS
import pytest

def test_runtime_interlocks_remain_independent_of_ui_and_log_flags(monkeypatch, tmp_path):
    monkeypatch.setenv("WEBHOOK_SECRET","test-webhook-audit")
    monkeypatch.setenv("ADMIN_TOKEN","test-admin-audit")
    monkeypatch.setenv("DB_PATH",str(tmp_path/"audit.sqlite"))
    monkeypatch.setenv("ALLOWED_SYMBOLS","XRPUSDT")
    monkeypatch.setenv("ALLOWED_TIMEFRAMES","5")
    import app
    bot=importlib.reload(app)
    assert bot.MODE=="DRY_RUN"
    assert LIVE_ORDER_TRANSMISSION is False
    assert "PRODUCTION_ENTRY_SENDER_NOT_CERTIFIED" in MANDATORY_BARRIERS
    with TestClient(bot.app) as client:
        assert client.get("/operator/execution-audit").status_code==403
        auth={"X-Admin-Token":"test-admin-audit"}
        initial=client.get("/operator/execution-audit",headers=auth)
        assert initial.status_code==200
        assert initial.headers["cache-control"]=="private, no-store"
        r=initial.json()
        assert r["entry_sender"]=="HARD_LOCKED"
        assert r["production_execution_worker"]=="NOT_CONNECTED"
        assert r["orders_enabled"] is False
        assert r["can_trade"] is False
        assert r["acceptance_certified"] is False
        assert r["operator_intake_enabled"] is False
        assert client.post("/operator/switch",headers=auth,json={"enabled":True}).status_code==200
        updated=client.get("/operator/execution-audit",headers=auth).json()
        assert updated["operator_intake_enabled"] is True
        assert updated["orders_enabled"] is False
        assert updated["entry_sender"]=="HARD_LOCKED"
        health=client.get("/health").json()
        assert health["exchange_authentication"]=="NOT_CHECKED_BY_PUBLIC_HEALTH"
        assert health["live_trading"] is False
        signal={"alert":"Bullish Confirmation Signal","ticker":"XRPUSDT",
                "tf":"5","bartime":1793375400000,
                "entry":"2.00","sl":"1.90","tp1":"2.10"}
        accepted=client.post("/webhook/test-webhook-audit",json=signal)
        assert accepted.status_code==200
        body=accepted.json()
        assert body["result"]=="ACCEPTED"
        assert body["levels_status"]=="VALID"
        assert body["trade_candidate"] is False
        assert body["tradingview_sender_verified"] is False
        assert body["orders_enabled"] is False
        assert body["execution_blocked_reason"]=="LIVE_ORDER_EXECUTION_NOT_CERTIFIED"
        duplicate=client.post("/webhook/test-webhook-audit",json=signal).json()
        assert duplicate["result"]=="DUPLICATE"
        missing=client.post("/webhook/test-webhook-audit",json=dict(signal,bartime=1793375700000,sl="")).json()
        assert missing["result"]=="ACCEPTED"
        assert missing["levels_status"]=="NOT_PROVIDED"
        assert missing["trade_candidate"] is False
        assert missing["execution_blocked_reason"]=="ORIGINAL_ENTRY_SL_TP_UNVERIFIED"
        with pytest.raises(BinanceTransportBlocked,match="locked"):
            BinanceReadOnly("key","secret").submit_entry({"symbol":"XRPUSDT"})

def test_audit_labels_cannot_be_faked_by_switch(monkeypatch,tmp_path):
    monkeypatch.setenv("WEBHOOK_SECRET","test-other-hook")
    monkeypatch.setenv("ADMIN_TOKEN","test-other-admin")
    monkeypatch.setenv("DB_PATH",str(tmp_path/"other.sqlite"))
    import app
    bot=importlib.reload(app)
    with TestClient(bot.app) as client:
        auth={"X-Admin-Token":"test-other-admin"}
        client.post("/operator/switch",headers=auth,json={"enabled":True})
        a=client.get("/operator/execution-audit",headers=auth).json()
        assert a["mode"]=="DRY_RUN"
        assert a["operator_intake_enabled"] is True
        assert a["native_sl_tp"]=="NOT_EXCHANGE_CERTIFIED"
        assert a["automatic_emergency_recovery"]=="NOT_PRODUCTION_CERTIFIED"
        assert a["real_money_orders_sent_by_this_endpoint"]==0
