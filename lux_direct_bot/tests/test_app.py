import importlib
import os
from fastapi.testclient import TestClient

def test_safe_webhooks(monkeypatch, tmp_path):
    monkeypatch.setenv("WEBHOOK_SECRET", "test-hook")
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "signals.sqlite"))
    import app
    bot = importlib.reload(app)
    p = {"alert":"Bullish Confirmation Signal", "ticker":"XRPUSDT",
         "tf":"5", "bartime":"1793375400000"}
    with TestClient(bot.app) as c:
        assert c.get("/health").json()["live_trading"] is False
        root = c.get("/")
        assert root.status_code == 200
        assert "text/html" in root.headers["content-type"]
        assert "LUX DIRECT BOT" in root.text
        assert "No orders, positions or profit figures" in root.text
        assert "test-admin" not in root.text
        assert root.headers["cache-control"] == "no-store"
        assert c.get("/terminal").status_code == 200
        assert c.get("/setup").status_code == 403
        assert c.get("/binance/diagnostics").status_code == 403
        assert c.get("/binance/account").status_code == 403
        assert c.post("/admin/selftest").status_code == 403
        dry = c.post("/admin/selftest", headers={"X-Admin-Token": "test-admin"})
        assert dry.status_code == 200
        assert dry.headers["cache-control"] == "no-store"
        assert dry.json()["result"] == "PASS"
        assert [c["side"] for c in dry.json()["checks"]] == ["BUY", "SELL"]
        assert dry.json()["test_records_persisted"] == 0
        assert dry.json()["exchange_orders_sent"] == 0
        assert dry.json()["actual_luxalgo_alert_verified"] is False
        assert c.get("/signals", headers={"X-Admin-Token":"test-admin"}).json() == []
        setup = c.get("/setup", headers={"X-Admin-Token": "test-admin"})
        assert setup.status_code == 200
        assert setup.headers["cache-control"] == "no-store"
        assert setup.json()["mode"] == "DRY_RUN"
        assert setup.json()["real_money_enabled"] is False
        assert setup.json()["webhook_url"].endswith("/webhook/test-hook")
        assert '"bartime": {time}' in setup.json()["luxalgo_alert_message"]
        monkeypatch.setattr(bot, "get_binance_public_status", lambda: {
            "public_api": "REACHABLE", "xrp_usdt": "TRADING",
            "api_credentials": "NOT_CONNECTED", "orders_enabled": False,
        })
        public_check = c.get("/binance/diagnostics", headers={"X-Admin-Token": "test-admin"})
        assert public_check.status_code == 200
        assert public_check.json()["orders_enabled"] is False
        account = c.get("/binance/account", headers={"X-Admin-Token":"test-admin"})
        assert account.status_code == 200
        assert account.json()["orders_enabled"] is False
        assert account.headers["cache-control"] == "no-store"
        assert c.post("/webhook/bad", json=p).status_code == 403
        a = c.post("/webhook/test-hook", json=p).json()
        b = c.post("/webhook/test-hook", json=p).json()
        assert a["result"] == "ACCEPTED"
        assert a["side"] == "BUY"
        assert b["result"] == "DUPLICATE"
        assert c.get("/signals").status_code == 403
        rows = c.get("/signals", headers={"X-Admin-Token":"test-admin"}).json()
        assert len(rows) == 1 and rows[0]["status"] == "DRY_RUN"
        q = dict(p, alert="Bearish Confirmation Signal", bartime="1793375460000")
        assert c.post("/webhook/test-hook", json=q).json()["side"] == "SELL"
        assert c.post("/webhook/test-hook", json=dict(p,ticker="ETHUSDT")).status_code == 422
        assert c.post("/webhook/test-hook", json=dict(p,alert="Take Profit")).status_code == 422
        assert c.post("/webhook/test-hook", json=dict(p,tf="1")).status_code == 422
        assert c.post("/webhook/test-hook", json=dict(p,bartime="2026-10-30T16:00:00Z")).status_code == 422
        assert c.post("/webhook/test-hook", json=dict(p,bartime=1793375400000)).json()["result"] == "DUPLICATE"
        assert c.get("/setup", headers={"X-Admin-Token":"test-admin"}).json()["allowed_timeframes"] == ["5"]
