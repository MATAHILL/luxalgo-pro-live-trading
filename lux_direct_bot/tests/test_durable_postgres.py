"""Isolated durable PostgreSQL adapter regressions. No real DB or Binance."""
import importlib
from unittest.mock import Mock

import pytest

from durable_postgres import DurablePostgres, initialize_postgres, SCHEMA


class FakePg:
    def __init__(self):
        self.calls=[]
        self.closed=False
        self.commits=0
        self.rollbacks=0

    def execute(self,sql,params=None):
        self.calls.append((sql,params))
        return Mock(rowcount=1)

    def commit(self):
        self.commits+=1

    def rollback(self):
        self.rollbacks+=1

    def close(self):
        self.closed=True


def connect_fake(connection):
    def factory(uri,**kwargs):
        assert uri.startswith("postgresql://")
        assert kwargs["connect_timeout"]==6
        assert kwargs["row_factory"] is not None
        return connection
    return factory


def test_postgres_adapter_maps_only_direct_bot_tables_and_placeholders():
    c=FakePg()
    with DurablePostgres("postgresql://some-private-service/fresh_db",connector=connect_fake(c)) as db:
        sql="""INSERT OR IGNORE INTO signals
                 (id,at,symbol,side,timeframe,bartime,event,status)
                 VALUES(?,?,?,?,?,?,?,?)"""
        db.execute(sql,("id",1,"XRPUSDT","BUY","5","1234567890123","bullish confirmation","DRY_RUN"))
        db.execute("SELECT id FROM signals ORDER BY at DESC, rowid DESC LIMIT 30")
        db.execute("UPDATE operator_control SET enabled=? WHERE id=1",(1,))
        db.execute("SELECT at FROM webhook_attempts WHERE at > ?",(5,))
        db.execute("BEGIN IMMEDIATE")
        assert "INSERT INTO direct_signals" in c.calls[0][0]
        assert "ON CONFLICT DO NOTHING" in c.calls[0][0]
        assert c.calls[0][0].count("%s")==8
        assert "ingest_id DESC" in c.calls[1][0]
        assert "UPDATE direct_operator_control" in c.calls[2][0]
        assert "%s" in c.calls[2][0]
        assert "FROM direct_webhook_attempts" in c.calls[3][0]
        assert c.calls[4][0]=="SELECT 1"
    assert c.commits==1 and c.rollbacks==0 and c.closed


def test_postgres_exception_rolls_back_without_committing():
    c=FakePg()
    with pytest.raises(RuntimeError):
        with DurablePostgres("postgresql://private/dedicated",connector=connect_fake(c)) as db:
            db.execute("UPDATE operator_control SET enabled=?",(1,))
            raise RuntimeError("simulated failed authorization")
    assert c.rollbacks==1 and c.commits==0 and c.closed


def test_pg_startup_has_dedicated_schema_and_immutable_signal_identity():
    c=FakePg()
    with DurablePostgres("postgresql://private/dedicated",connector=connect_fake(c)) as db:
        initialize_postgres(db)
    statements="\n".join(row[0] for row in c.calls)
    assert "CREATE TABLE IF NOT EXISTS direct_signals" in statements
    assert "id TEXT PRIMARY KEY" in statements
    assert "ingest_id BIGSERIAL UNIQUE" in statements
    assert "direct_operator_control" in statements
    assert "direct_webhook_attempts" in statements
    assert "direct_execution_audit" in statements
    assert "ON CONFLICT(id) DO NOTHING" in statements
    assert "luxalgo_prod" not in statements


@pytest.mark.parametrize("bad",["",":memory:","sqlite:///tmp/x.db","wrong"])
def test_pg_refuses_missing_or_non_postgres_url(bad):
    with pytest.raises(RuntimeError):
        DurablePostgres(bad,connector=connect_fake(FakePg()))


def test_unknown_sqlite_pragma_cannot_run_on_postgres():
    with pytest.raises(RuntimeError):
        DurablePostgres.translate("PRAGMA table_info(signals)")


def test_new_bot_sqlite_default_does_not_switch_without_explicit_env(monkeypatch,tmp_path):
    monkeypatch.delenv("DIRECT_BOT_STORAGE_BACKEND",raising=False)
    monkeypatch.setenv("WEBHOOK_SECRET","local-test-hook")
    monkeypatch.setenv("ADMIN_TOKEN","local-test-admin")
    monkeypatch.setenv("DB_PATH",str(tmp_path/"default.sqlite"))
    import app
    bot=importlib.reload(app)
    assert bot.STORAGE_BACKEND=="sqlite"
    from fastapi.testclient import TestClient
    with TestClient(bot.app) as client:
        assert client.get("/health").json()["live_trading"] is False
        assert client.get("/signals",headers={"X-Admin-Token":"local-test-admin"}).json()==[]


def test_postgres_mode_never_silently_falls_back_to_ephemeral_sqlite(monkeypatch,tmp_path):
    monkeypatch.setenv("DIRECT_BOT_STORAGE_BACKEND","postgres")
    monkeypatch.delenv("DIRECT_BOT_DATABASE_URL",raising=False)
    monkeypatch.setenv("DB_PATH",str(tmp_path/"disallowed.sqlite"))
    monkeypatch.setenv("WEBHOOK_SECRET","different-webhook")
    monkeypatch.setenv("ADMIN_TOKEN","different-admin")
    import app
    bot=importlib.reload(app)
    with pytest.raises(RuntimeError,match="Dedicated new-bot PostgreSQL URL"):
        bot.initialize()
    assert not (tmp_path/"disallowed.sqlite").exists()
