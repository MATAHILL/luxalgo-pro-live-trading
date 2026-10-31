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
        assert "Real-money orders remain OFF" in root.text
        assert "test-admin" not in root.text
        assert root.headers["cache-control"] == "no-store"
        assert c.get("/terminal").status_code == 200
        assert 'id="binanceSummary"' in root.text
        assert "CONNECTED READ-ONLY" in root.text
        assert "Available USDT" in root.text
        assert "Live orders OFF" not in root.text or "Real-money orders remain OFF" in root.text
        connect = c.get("/connect")
        assert connect.status_code == 200
        assert connect.headers["cache-control"] == "no-store"
        assert "Copy private webhook URL" in connect.text
        assert "Test private webhook route" in connect.text
        assert 'id="key"' in connect.text
        assert "test-hook" not in connect.text
        assert "test-admin" not in connect.text
        assert "BINANCE:XRPUSDT.P" in connect.text
        assert c.get("/setup").status_code == 403
        assert c.get("/binance/diagnostics").status_code == 403
        assert c.get("/binance/account").status_code == 403
        assert c.get("/connection/status").status_code == 403
        assert c.get("/connection/attempts").status_code == 403
        empty_attempts = c.get("/connection/attempts", headers={"X-Admin-Token":"test-admin"}).json()
        assert empty_attempts["attempts"] == []
        assert empty_attempts["orders_enabled"] is False
        assert c.post("/admin/selftest").status_code == 403
        dry = c.post("/admin/selftest", headers={"X-Admin-Token": "test-admin"})
        assert dry.status_code == 200
        assert dry.headers["cache-control"] == "no-store"
        assert dry.json()["result"] == "PASS"
        assert [c["side"] for c in dry.json()["checks"]] == ["BUY", "SELL"]
        assert dry.json()["test_records_persisted"] == 0
        assert dry.json()["exchange_orders_sent"] == 0
        assert dry.json()["actual_luxalgo_alert_verified"] is False
        assert c.get("/operator/status").status_code == 403
        assert c.get("/operator/readiness").status_code == 403
        assert c.post("/operator/switch", json={"enabled": True}).status_code == 403
        auth = {"X-Admin-Token":"test-admin"}
        state = c.get("/operator/status", headers=auth).json()
        assert state["enabled"] is False
        readiness = c.get("/operator/readiness",headers=auth).json()
        assert readiness["ready"] is False
        assert readiness["live_orders_enabled"] is False
        assert readiness["operator_on"] is False
        assert "LIVE_ORDER_SENDER_HARD_LOCKED" in readiness["blockers"]
        assert "PERSISTENT_SIGNAL_LEDGER_REQUIRED" in readiness["blockers"]
        assert state["orders_enabled"] is False
        assert c.post("/operator/switch", json={"enabled": "true"},headers=auth).status_code == 422
        on = c.post("/operator/switch",json={"enabled":True},headers=auth).json()
        assert on["enabled"] is True
        assert on["orders_enabled"] is False
        assert c.get("/operator/status",headers=auth).json()["enabled"] is True

        after_selftest = c.get("/connection/attempts", headers={"X-Admin-Token":"test-admin"}).json()
        assert after_selftest["attempts"] == []
        assert c.get("/signals", headers={"X-Admin-Token":"test-admin"}).json() == []
        state = c.get("/connection/status", headers={"X-Admin-Token":"test-admin"}).json()
        assert state["state"] == "NO_WEBHOOK_RECORDS"
        assert state["records"] == 0
        assert state["tradingview_alert_confirmed"] is False
        assert state["origin_independently_verified"] is False
        assert state["orders_enabled"] is False
        setup = c.get("/setup", headers={"X-Admin-Token": "test-admin"})
        assert setup.status_code == 200
        assert setup.headers["cache-control"] == "no-store"
        assert setup.json()["mode"] == "DRY_RUN"
        assert setup.json()["selected_chart"] == "BINANCE:XRPUSDT.P"
        assert setup.json()["real_money_enabled"] is False
        assert setup.json()["webhook_url"].endswith("/webhook/test-hook")
        assert '"bartime": {time}' in setup.json()["luxalgo_alert_message"]
        assert '"entry": "{close}"' in setup.json()["luxalgo_alert_message"]
        assert '"sl": "{sl1}"' in setup.json()["luxalgo_alert_message"]
        assert '"tp1": "{tp1}"' in setup.json()["luxalgo_alert_message"]
        assert '"order_type": "MARKET"' in setup.json()["luxalgo_alert_message"]
        assert '"token"' not in setup.json()["luxalgo_alert_message"]
        assert setup.json()["luxalgo_alert_message"].index('"ticker"') < setup.json()["luxalgo_alert_message"].index('"alert"')
        import json
        # This LuxAlgo template intentionally uses unquoted {time}. Replace
        # placeholder values to assert runtime JSON remains syntactically valid.
        template = setup.json()["luxalgo_alert_message"]
        for name, value in (("{ticker}", "XRPUSDT.P"), ("{default}", "Bullish Confirmation Signal"),
                            ("{tf}", "5"), ("{close}", "1.5"), ("{sl1}", "1.4"), ("{tp1}", "1.7")):
            template = template.replace(name, value)
        parsed_template = json.loads(template.replace("{time}", "1793375400000"))
        assert parsed_template["order_type"] == "MARKET"
        assert parsed_template["entry"] == "1.5"
        assert parsed_template["sl"] == "1.4"
        assert parsed_template["tp1"] == "1.7"

        assert setup.json()["luxalgo_alert_message"].count("{default}") == 1
        assert '"ticker": "{ticker}"' in setup.json()["luxalgo_alert_message"]
        assert '"tf": "{tf}"' in setup.json()["luxalgo_alert_message"]
        # Transport test uses the real protected webhook route but MUST NOT
        # appear as a LuxAlgo signal or enable exchange execution.
        probe = c.post("/webhook/test-hook",
                       json={"_probe": "CHECK_WEBHOOK_ROUTE_V1"})
        assert probe.status_code == 200
        assert probe.json() == {"result": "PROBE_OK", "mode": "DRY_RUN",
                                "orders_enabled": False, "signal_created": False}
        assert c.get("/signals", headers=auth).json() == []
        after_probe = c.get("/connection/status", headers=auth).json()
        assert after_probe["records"] == 0
        recorded_probe = c.get("/connection/attempts", headers=auth).json()
        assert recorded_probe["attempts"][0]["result"] == "PROBE"
        assert recorded_probe["attempts"][0]["reason"] == "TEST_OK"
        assert recorded_probe["attempts"][0].keys() == {"at", "result", "reason"}

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
        state_after = c.get("/connection/status", headers={"X-Admin-Token":"test-admin"}).json()
        assert state_after["state"] == "WEBHOOK_RECORDS_PRESENT"
        assert state_after["records"] == 1
        assert state_after["latest"]["symbol"] == "XRPUSDT"
        assert state_after["tradingview_alert_confirmed"] is False
        q = dict(p, alert="Bearish Confirmation Signal", bartime="1793375460000")
        assert c.post("/webhook/test-hook", json=q).json()["side"] == "SELL"
        assert c.post("/webhook/test-hook", json=dict(p,ticker="ETHUSDT")).status_code == 422
        assert c.post("/webhook/test-hook", json=dict(p,alert="Take Profit")).status_code == 422
        seen_attempts = c.get("/connection/attempts", headers={"X-Admin-Token":"test-admin"}).json()
        assert seen_attempts["attempts"][0]["reason"] == "UNSUPPORTED_LUXALGO_SIGNAL"
        assert any(x["result"] == "DUPLICATE" for x in seen_attempts["attempts"])
        assert any(x["result"] == "ACCEPTED" for x in seen_attempts["attempts"])
        assert seen_attempts["tradingview_sender_verified"] is False
        assert all("test-hook" not in str(x) for x in seen_attempts["attempts"])
        assert c.post("/webhook/test-hook", json=dict(p,tf="1")).status_code == 422
        assert c.post("/webhook/test-hook", json=dict(p,bartime="2026-10-30T16:00:00Z")).status_code == 422
        assert c.post("/webhook/test-hook", json=dict(p,bartime=1793375400000)).json()["result"] == "DUPLICATE"
        assert c.get("/setup", headers={"X-Admin-Token":"test-admin"}).json()["allowed_timeframes"] == ["5"]
        off = c.post("/operator/switch",json={"enabled":False},headers=auth).json()
        assert off["enabled"] is False and off["orders_enabled"] is False
        paused = dict(p, alert="Bearish Confirmation Signal", bartime=1793375580000)
        assert c.post("/webhook/test-hook",json=paused).json()["result"] == "ACCEPTED"
        rows = c.get("/signals",headers=auth).json()
        assert rows[0]["status"] == "PAUSED"
        assert c.get("/connection/status",headers=auth).json()["records"] == 3

