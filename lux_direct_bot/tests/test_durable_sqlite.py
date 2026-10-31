"""Durable SQLite source-of-truth restart and fail-closed tests. No exchange calls."""
import importlib
import json
import sqlite3
from unittest.mock import patch
import pytest

from durable_sqlite import open_ledger, storage_health, initialize_audit


def test_unmounted_render_path_rejected_and_never_creates_fake_ledger(tmp_path):
    fake=tmp_path/"var"/"data"
    fake.mkdir(parents=True)
    filename=str(fake/"signals.sqlite3")
    state=storage_health(filename,require_persistence=True,mount_path=str(fake))
    assert state["durable"] is False
    assert state["reason"]=="PERSISTENT_DISK_NOT_MOUNTED"
    with pytest.raises(RuntimeError,match="PERSISTENT_DISK_NOT_MOUNTED"):
        with open_ledger(filename,require_persistence=True,mount_path=str(fake)):
            pass
    assert not (fake/"signals.sqlite3").exists()


def test_mocked_mount_supports_committed_reopen_and_duplicate_rejection(tmp_path):
    disk=tmp_path/"disk"
    disk.mkdir()
    path=str(disk/"ledger.sqlite3")
    with patch("durable_sqlite.os.path.ismount",return_value=True):
        with open_ledger(path,require_persistence=True,mount_path=str(disk)) as db:
            initialize_audit(db)
            db.execute("CREATE TABLE IF NOT EXISTS signal_data (id TEXT PRIMARY KEY, kind TEXT)")
            db.execute("INSERT OR IGNORE INTO signal_data(id,kind) VALUES('bar-5m','DRY_RUN')")
        with open_ledger(path,require_persistence=True,mount_path=str(disk)) as db:
            # Reopened connection, same unique ID never executes twice.
            assert db.execute("INSERT OR IGNORE INTO signal_data(id,kind) VALUES('bar-5m','DRY_RUN')").rowcount==0
            assert db.execute("SELECT COUNT(*) FROM signal_data").fetchone()[0]==1
            names={x[0] for x in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert "execution_audit" in names
        with open_ledger(path,require_persistence=True,mount_path=str(disk)) as db:
            assert db.execute("SELECT COUNT(*) FROM signal_data").fetchone()[0]==1


def test_transaction_rollback_after_failure(tmp_path):
    path=str(tmp_path/"atomic.sqlite3")
    with open_ledger(path) as db:
        db.execute("CREATE TABLE records(id TEXT PRIMARY KEY)")
    with pytest.raises(RuntimeError,match="unexpected crash"):
        with open_ledger(path) as db:
            db.execute("INSERT INTO records VALUES('should-rollback')")
            raise RuntimeError("unexpected crash")
    with open_ledger(path) as db:
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0]==0


def test_private_storage_readback_denies_public_access(monkeypatch,tmp_path):
    monkeypatch.setenv("DB_PATH",str(tmp_path/"receiver.sqlite"))
    monkeypatch.setenv("WEBHOOK_SECRET","unit-webhook-only")
    monkeypatch.setenv("ADMIN_TOKEN","unit-operator-only")
    monkeypatch.setenv("DIRECT_BOT_REQUIRE_PERSISTENCE","0")
    import app
    bot=importlib.reload(app)
    from fastapi.testclient import TestClient
    with TestClient(bot.app) as client:
        assert client.get("/operator/storage-readiness").status_code==403
        response=client.get("/operator/storage-readiness",
                            headers={"X-Admin-Token":"unit-operator-only"})
        assert response.status_code==200
        result=response.json()
        assert result["status"]=="NOT_DURABLE"
        assert result["orders_enabled"] is False
        assert result["can_trade"] is False


def test_receiver_restart_retains_bar_id_and_does_not_replay(monkeypatch,tmp_path):
    monkeypatch.setenv("DB_PATH",str(tmp_path/"same-file.sqlite3"))
    monkeypatch.setenv("WEBHOOK_SECRET","test-restart-hook")
    monkeypatch.setenv("ADMIN_TOKEN","test-restart-operator")
    monkeypatch.setenv("DIRECT_BOT_REQUIRE_PERSISTENCE","0")
    import app
    receiver=importlib.reload(app)
    payload={"alert":"Bullish Confirmation Signal","ticker":"XRPUSDT",
             "tf":"5","bartime":1793375400000}
    from fastapi.testclient import TestClient
    with TestClient(receiver.app) as c:
        a=c.post("/webhook/test-restart-hook",json=payload)
        assert a.status_code==200 and a.json()["result"]=="ACCEPTED"
        assert c.get("/connection/status",
                     headers={"X-Admin-Token":"test-restart-operator"}).json()["records"]==1
    # Simulate process restart: new module initialization on same filesystem.
    receiver=importlib.reload(app)
    with TestClient(receiver.app) as c:
        b=c.post("/webhook/test-restart-hook",json=payload)
        assert b.status_code==200 and b.json()["result"]=="DUPLICATE"
        rows=c.get("/signals",headers={"X-Admin-Token":"test-restart-operator"}).json()
        assert len(rows)==1 and rows[0]["status"] in ("DRY_RUN","PAUSED")
        assert c.get("/health").json()["live_trading"] is False
