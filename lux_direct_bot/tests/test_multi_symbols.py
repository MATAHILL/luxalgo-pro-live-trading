"""Six-symbol DRY_RUN intake contract: XRP plus five verified Binance USDⓈ-M pairs.

Unconfirmed QSDT is intentionally not supported. No exchange orders may be sent.
"""
import importlib

from fastapi.testclient import TestClient


def test_verified_multi_symbol_webhooks_are_isolated_and_dry_run(monkeypatch, tmp_path):
    symbols = ("XRPUSDT", "BNBUSDT", "AINUSDT", "MAGMAUSDT",
               "BIRBUSDT", "SANDUSDT")
    monkeypatch.setenv("WEBHOOK_SECRET", "multi-hook-test")
    monkeypatch.setenv("ADMIN_TOKEN", "multi-admin-test")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "multi.sqlite"))
    monkeypatch.setenv("ALLOWED_SYMBOLS", ",".join(symbols))
    monkeypatch.setenv("ALLOWED_TIMEFRAMES", "5")
    monkeypatch.setenv(
        "SYMBOL_MAP_JSON",
        __import__("json").dumps({symbol + ".P": symbol for symbol in symbols}),
    )
    import app
    bot = importlib.reload(app)
    with TestClient(bot.app) as c:
        admin = {"X-Admin-Token": "multi-admin-test"}
        config = c.get("/setup", headers=admin).json()
        assert config["allowed_symbols"] == sorted(symbols)
        assert config["mode"] == "DRY_RUN"
        assert config["real_money_enabled"] is False
        assert c.post("/operator/switch", headers=admin, json={"enabled": True}).json()["enabled"] is True
        ids = []
        for idx, symbol in enumerate(symbols):
            payload = {
                "alert": "Bullish Confirmation Signal" if idx % 2 == 0
                         else "Bearish Confirmation Signal",
                "ticker": symbol + ".P", "tf": "5",
                "bartime": 1793375400000 + 300000 * idx,
            }
            result = c.post("/webhook/multi-hook-test", json=payload)
            assert result.status_code == 200, (symbol, result.text)
            data = result.json()
            assert data["result"] == "ACCEPTED"
            assert data["mode"] == "DRY_RUN"
            ids.append(data["signal_id"])
        assert len(set(ids)) == len(symbols)
        rows = c.get("/signals", headers=admin).json()
        assert {x["symbol"] for x in rows} == set(symbols)
        assert all(x["status"] == "DRY_RUN" for x in rows)
        assert c.get("/connection/status", headers=admin).json()["records"] == len(symbols)

        # Do not silently map the ambiguous requested QSDT to Binance QUSDT.
        bad = c.post("/webhook/multi-hook-test", json={
            "alert": "Bullish Confirmation Signal", "ticker": "QSDTUSDT.P",
            "tf": "5", "bartime": 1793377200000,
        })
        assert bad.status_code == 422
        assert bad.json()["detail"] == "SYMBOL_NOT_ALLOWED"
        assert c.get("/connection/status", headers=admin).json()["records"] == len(symbols)
        health = c.get("/health").json()
        assert health["live_trading"] is False
        assert health["allowed_symbols"] == sorted(symbols)
        assert health["allowed_timeframes"] == ["5"]
        assert c.get("/operator/readiness", headers=admin).json()["live_orders_enabled"] is False
