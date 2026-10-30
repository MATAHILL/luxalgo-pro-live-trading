import importlib
import os
from fastapi.testclient import TestClient

def test_safe_webhooks(monkeypatch, tmp_path):
    monkeypatch.setenv("WEBHOOK_SECRET", "test-hook")
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "signals.sqlite"))
    import app
    bot = importlib.reload(app)
    p = {"alert":"Bullish Confirmation Signal", "ticker":"BTCUSDT",
         "tf":"5", "bartime":"1793375400000"}
    with TestClient(bot.app) as c:
        assert c.get("/health").json()["live_trading"] is False
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
