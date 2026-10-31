"""Source-only LuxAlgo reference level and existing-history migration tests."""
import importlib
import sqlite3
from fastapi.testclient import TestClient
from signal_levels import parse_luxalgo_reference_levels


def test_source_price_geometry_and_unavailable_values():
    payload={"entry":"650.12","sl":"648.40","tp1":"655.20"}
    assert parse_luxalgo_reference_levels(payload,"BUY")==(
        "650.12","648.40","655.20","VALID")
    assert parse_luxalgo_reference_levels(
        {"entry":"10","sl":"12","tp1":"8"},"SELL")==(
        "10","12","8","VALID")
    assert parse_luxalgo_reference_levels({},"BUY")==(
        None,None,None,"NOT_PROVIDED")
    for bad in (
        {"entry":"10","sl":"11","tp1":"12"},
        {"entry":"10","sl":"NaN","tp1":"12"},
        {"entry":True,"sl":"9","tp1":"12"},
        {"entry":"1","sl":"1","tp1":"2"},
    ):
        result=parse_luxalgo_reference_levels(bad,"BUY")
        assert result==(None,None,None,"INVALID")


def test_existing_webhook_history_retained_by_additive_upgrade(monkeypatch,tmp_path):
    db_path=tmp_path/"existing.sqlite"
    with sqlite3.connect(db_path) as con:
        con.execute("""CREATE TABLE signals(
            id TEXT PRIMARY KEY,at INTEGER NOT NULL,symbol TEXT NOT NULL,
            side TEXT NOT NULL,timeframe TEXT NOT NULL,bartime TEXT NOT NULL,
            event TEXT NOT NULL,status TEXT NOT NULL)""")
        con.execute("""INSERT INTO signals VALUES
            ('old-birb-0001',1793375100,'BIRBUSDT','BUY','5',
            '1793375100000','bullish confirmation','DRY_RUN')""")
    monkeypatch.setenv("WEBHOOK_SECRET","test-webhook-secret")
    monkeypatch.setenv("ADMIN_TOKEN","test-dashboard-secret")
    monkeypatch.setenv("DB_PATH",str(db_path))
    monkeypatch.setenv("ALLOWED_SYMBOLS","BIRBUSDT,BNBUSDT,XRPUSDT")
    monkeypatch.setenv("ALLOWED_TIMEFRAMES","5")
    import app
    bot=importlib.reload(app)
    auth={"X-Admin-Token":"test-dashboard-secret"}
    with TestClient(bot.app) as client:
        old=client.get("/signals",headers=auth).json()
        assert len(old)==1
        assert old[0]["symbol"]=="BIRBUSDT"
        assert old[0]["entry_reference"] is None
        assert old[0]["levels_status"]=="NOT_PROVIDED"
        p=dict(alert="Bullish Confirmation Signal",ticker="BNBUSDT",
               tf="5",bartime=1793375400000,
               entry="655.2",sl="654.1",tp1="660.5")
        result=client.post("/webhook/test-webhook-secret",json=p)
        assert result.status_code==200
        assert result.json()["result"]=="ACCEPTED"
        rows=client.get("/signals",headers=auth).json()
        assert len(rows)==2
        newest=rows[0]
        assert newest["symbol"]=="BNBUSDT"
        assert newest["entry_reference"]=="655.2"
        assert newest["stop_reference"]=="654.1"
        assert newest["take_profit_reference"]=="660.5"
        assert newest["levels_status"]=="VALID"
        assert newest["status"]=="PAUSED" or newest["status"]=="DRY_RUN"
        assert result.json()["mode"]=="DRY_RUN"
        assert client.get("/health").json()["live_trading"] is False
        # No exchange order endpoint is called by signal intake or retrieval.
        assert client.get("/connection/status",headers=auth).json()["records"]==2
        rejected=client.post("/webhook/test-webhook-secret",json=dict(
            p,bartime=1793375700000,sl="NaN"))
        assert rejected.status_code==200
        after=client.get("/signals",headers=auth).json()
        assert after[0]["levels_status"]=="INVALID"
        assert after[0]["entry_reference"] is None
        assert client.get("/health").json()["live_trading"] is False

def test_partial_reference_prices_are_preserved_without_certifying_protection():
    assert parse_luxalgo_reference_levels({"entry": "1.25"}, "BUY") == (
        "1.25", None, None, "NOT_PROVIDED")
    assert parse_luxalgo_reference_levels(
        {"entry": "1.25", "sl": "1.20"}, "BUY") == (
        "1.25", "1.20", None, "NOT_PROVIDED")
    assert parse_luxalgo_reference_levels(
        {"entry": "NaN"}, "BUY") == (None, None, None, "INVALID")
    assert parse_luxalgo_reference_levels(
        {"entry": True}, "BUY") == (None, None, None, "INVALID")
