"""LUX DIRECT BOT durable PostgreSQL signal ledger, opt-in only.

Unrelated to old LUXALGO PRO. Requires a dedicated paid Postgres database in
the same Render region as the new bot, never reuse the old bot's database.
No orders, exchange writes, API key reading, or trading state changes here.
"""
import psycopg
from psycopg.rows import dict_row

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS direct_signals (
        id TEXT PRIMARY KEY,
        ingest_id BIGSERIAL UNIQUE,
        at BIGINT NOT NULL,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        timeframe TEXT NOT NULL,
        bartime TEXT NOT NULL,
        event TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('DRY_RUN','PAUSED','TEST_ONLY')),
        entry_reference TEXT, stop_reference TEXT,
        take_profit_reference TEXT,
        levels_status TEXT NOT NULL DEFAULT 'NOT_PROVIDED')""",
    """CREATE TABLE IF NOT EXISTS direct_webhook_attempts (
        id BIGSERIAL PRIMARY KEY,
        at BIGINT NOT NULL,
        result TEXT NOT NULL,
        reason TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS direct_operator_control (
        id INTEGER PRIMARY KEY CHECK(id=1),
        enabled INTEGER NOT NULL CHECK(enabled IN (0,1)))""",
    """INSERT INTO direct_operator_control(id,enabled)
       VALUES (1,0) ON CONFLICT(id) DO NOTHING""",
    """CREATE TABLE IF NOT EXISTS direct_execution_audit (
        event_id TEXT PRIMARY KEY,
        signal_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN
          ('BLOCKED','REQUESTED','UNKNOWN_ACK','PROTECTION_PENDING',
           'PROTECTED','HALTED','CLOSED_VERIFIED')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
)

TABLES = {
    "signals":"direct_signals",
    "webhook_attempts":"direct_webhook_attempts",
    "operator_control":"direct_operator_control",
}

class DurablePostgres:
    """Small restricted adapter for this application's known SQL statements."""
    def __init__(self, url, *, connector=psycopg.connect):
        if not isinstance(url,str) or not url.startswith(("postgresql://","postgres://")):
            raise RuntimeError("Dedicated PostgreSQL URL required")
        self.conn=connector(url, connect_timeout=6, row_factory=dict_row)
    def __enter__(self):
        return self
    def __exit__(self,exc_type,exc,tb):
        try:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.conn.close()
        return False
    @staticmethod
    def translate(sql):
        if not isinstance(sql,str):
            raise TypeError("Invalid SQL")
        sql=sql.strip()
        if not sql:
            raise ValueError("Empty SQL")
        if sql.upper()=="BEGIN IMMEDIATE":
            return "SELECT 1"
        if "PRAGMA " in sql.upper():
            raise RuntimeError("SQLite PRAGMA must never reach Postgres")
        was_ignore="INSERT OR IGNORE INTO" in sql.upper()
        if was_ignore:
            sql=sql.replace("INSERT OR IGNORE INTO","INSERT INTO",1)
            sql+=" ON CONFLICT DO NOTHING"
        for source,target in TABLES.items():
            import re
            sql=re.sub(r"\b"+source+r"\b",target,sql)
        sql=sql.replace("rowid DESC","ingest_id DESC")
        return sql.replace("?","%s")
    def execute(self,sql,params=None):
        return self.conn.execute(self.translate(sql),params)

def initialize_postgres(db):
    """Transactional schema setup. Never import or guess historical SQLite fills."""
    for statement in SCHEMA:
        db.execute(statement)
