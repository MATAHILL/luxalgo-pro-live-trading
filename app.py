"""LuxAlgo PRO: authenticated signal intake + READ-ONLY Gate.io and Binance API connectivity.

NO order submission, funds transfer, or withdrawal functions exist.
Authenticated /bot/status reports PostgreSQL-backed DRY RUN intent records only.
The /gate/plan endpoint only calculates indicative ATR levels; no orders are placed.
Exchange credentials must be supplied via hosting environment variables only.
"""
import hmac
import hashlib
import json
import os
import re
import time
import math
import secrets
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import base64
from collections import deque
from hashlib import sha256
from datetime import datetime, timezone, timedelta
from threading import Lock
from concurrent.futures import ThreadPoolExecutor
from xml.etree import ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from flask import Flask, jsonify, render_template_string, request, Response, session, redirect, url_for

# PostgreSQL dependency: psycopg[binary] (see requirements addition).
import psycopg
from psycopg.rows import dict_row

app = Flask(__name__)
# Secure signed cookie sessions. Changing TERMINAL_PASSWORD revokes old cookies.
_password_for_cookie = os.getenv('TERMINAL_PASSWORD', '')
app.secret_key = hmac.new(_password_for_cookie.encode('utf-8'), b'luxalgo-pro/session/v1', hashlib.sha256).digest()
app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE='Strict', PERMANENT_SESSION_LIFETIME=28800,
                  MAX_CONTENT_LENGTH=400000)
_login_failures = {}
_login_lock = Lock()
_lock = Lock()

# Durable signal-only audit store. All trade execution remains disabled.
# Do not fall back to RAM on DB failures: that would lose duplicate protection.
DB_SCHEMA = '''
-- Testnet-only order journal: separate from production exchange execution.
CREATE TABLE IF NOT EXISTS luxalgo_testnet_readiness (
 id SMALLINT PRIMARY KEY CHECK (id=1),
 state TEXT NOT NULL,
 details JSONB NOT NULL,
 checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS luxalgo_testnet_protected_journal (
 signal_id TEXT PRIMARY KEY,
 client_id TEXT NOT NULL UNIQUE,
 contract TEXT NOT NULL,
 side TEXT NOT NULL,
 state TEXT NOT NULL,
 payload JSONB NOT NULL,
 details JSONB NOT NULL DEFAULT '{}'::jsonb,
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_testnet_journal_state
 ON luxalgo_testnet_protected_journal(state,updated_at);

CREATE TABLE IF NOT EXISTS luxalgo_testnet_execution_handoff (
 event_id TEXT PRIMARY KEY,
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 received_at TIMESTAMPTZ NOT NULL,
 symbol TEXT NOT NULL,
 side TEXT NOT NULL,
 timeframe TEXT NOT NULL,
 order_type TEXT,
 status TEXT NOT NULL CHECK (status IN ('BLOCKED','REJECTED')),
 reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS luxalgo_live_signal_handoff (
 event_id TEXT PRIMARY KEY,
 received_at TIMESTAMPTZ NOT NULL,
 symbol TEXT NOT NULL,
 side TEXT NOT NULL,
 timeframe TEXT NOT NULL,
 order_type TEXT,
 level_status TEXT NOT NULL,
 source_bar_id TEXT,
 status TEXT NOT NULL DEFAULT 'SIGNAL_ONLY' CHECK(status='SIGNAL_ONLY'),
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE luxalgo_live_signal_handoff ADD COLUMN IF NOT EXISTS source_bar_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_live_handoff_source_bar
ON luxalgo_live_signal_handoff(symbol,side,timeframe,source_bar_id)
WHERE source_bar_id IS NOT NULL;
-- Historical Testnet journal retained for audit only, not market data.
CREATE TABLE IF NOT EXISTS luxalgo_one_trade_candidate (
 singleton_id SMALLINT PRIMARY KEY CHECK(singleton_id=1),
 event_id TEXT NOT NULL UNIQUE,
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 symbol TEXT NOT NULL,
 side TEXT NOT NULL,
 timeframe TEXT NOT NULL,
 entry_price NUMERIC NOT NULL,
 stop_loss NUMERIC NOT NULL,
 take_profit NUMERIC NOT NULL,
 status TEXT NOT NULL CHECK(status='WAITING_FOR_VERIFIED_EXECUTION'),
 note TEXT NOT NULL DEFAULT 'No real order submitted. Requires validated exchange execution and SL/TP.'
);
CREATE TABLE IF NOT EXISTS luxalgo_signal_receipts (
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 event_id TEXT NOT NULL,
 symbol TEXT NOT NULL,
 side TEXT NOT NULL,
 timeframe TEXT NOT NULL DEFAULT '15m'
);
CREATE INDEX IF NOT EXISTS idx_luxalgo_receipts_recent
 ON luxalgo_signal_receipts (id DESC);
CREATE TABLE IF NOT EXISTS luxalgo_execution_prechecks (
 event_id TEXT PRIMARY KEY,
 checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 symbol TEXT NOT NULL,
 side TEXT NOT NULL,
 timeframe TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('BLOCKED','SIMULATED')),
 risk_percent NUMERIC NOT NULL,
 details JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS luxalgo_simulated_orders (
 event_id TEXT PRIMARY KEY,
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 symbol TEXT NOT NULL,
 side TEXT NOT NULL,
 timeframe TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status='SIMULATED_ONLY'),
 details JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_luxalgo_sim_orders_recent ON luxalgo_simulated_orders(created_at DESC);
CREATE TABLE IF NOT EXISTS luxalgo_paper_tracking (
  event_id TEXT PRIMARY KEY REFERENCES luxalgo_simulated_orders(event_id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  checked_at TIMESTAMPTZ,
  state TEXT NOT NULL DEFAULT 'OPEN' CHECK(state IN ('OPEN','SL','TP','UNVERIFIABLE')),
  last_checked_candle BIGINT,
  exit_price NUMERIC,
  estimated_pnl_usdt NUMERIC,
  notes TEXT NOT NULL DEFAULT 'Hypothetical only; never an exchange fill'
);
CREATE TABLE IF NOT EXISTS luxalgo_user_avatar (
  id SMALLINT PRIMARY KEY CHECK (id=1),
  image_data BYTEA NOT NULL,
  image_type TEXT NOT NULL CHECK (image_type IN ('image/png','image/jpeg','image/webp')),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS luxalgo_bot_control (
 id SMALLINT PRIMARY KEY CHECK (id=1),
 enabled BOOLEAN NOT NULL DEFAULT FALSE,
 changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO luxalgo_bot_control(id,enabled) VALUES (1,FALSE) ON CONFLICT(id) DO NOTHING;
CREATE TABLE IF NOT EXISTS luxalgo_auto_paper_queue (
 event_id TEXT PRIMARY KEY,
 received_at TIMESTAMPTZ NOT NULL,
 state TEXT NOT NULL DEFAULT 'PENDING' CHECK(state IN ('PENDING','CREATED','REJECTED')),
 checked_at TIMESTAMPTZ,
 note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_auto_paper_pending ON luxalgo_auto_paper_queue(state,received_at);
-- Fail-closed live-order candidate audit. This table NEVER authorizes a live order.
CREATE TABLE IF NOT EXISTS luxalgo_live_order_guard_audit (
  event_id TEXT PRIMARY KEY,
  checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  decision TEXT NOT NULL DEFAULT 'BLOCKED' CHECK(decision='BLOCKED'),
  reason TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'DASHBOARD_GUARD_TEST'
);
-- Dashboard-only removal: audit and webhook receipts remain immutable.
CREATE TABLE IF NOT EXISTS luxalgo_hidden_signal_rows (
 event_id TEXT PRIMARY KEY,
 hidden_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS luxalgo_signal_order_types (
 event_id TEXT PRIMARY KEY,
 order_type TEXT NOT NULL CHECK(order_type IN ('MARKET','LIMIT'))
);
CREATE TABLE IF NOT EXISTS luxalgo_signal_levels (
 event_id TEXT PRIMARY KEY,
 checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 status TEXT NOT NULL CHECK(status IN ('VALID','MISSING','INVALID')),
 entry_price NUMERIC,
 stop_loss NUMERIC,
 take_profit NUMERIC,
 reason TEXT NOT NULL DEFAULT ''
);
-- DRY RUN reservation sandbox. Never consumed by an exchange order route.
CREATE TABLE IF NOT EXISTS luxalgo_risk_gate_simulations (
  event_id TEXT PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at TIMESTAMPTZ NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('SIMULATED','EXPIRED')),
  risk_usdt NUMERIC NOT NULL,
  leverage NUMERIC NOT NULL,
  details JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_luxalgo_risk_gate_sim_expiry ON luxalgo_risk_gate_simulations(expires_at);
-- Stage 5 simulated-only lifecycle ledger. This is NOT an exchange order ledger.
CREATE TABLE IF NOT EXISTS luxalgo_stage5_sim_order_ledger (
  event_id TEXT PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  state TEXT NOT NULL CHECK(state IN ('RESERVED','SIMULATED_ACK','CLOSED','BLOCKED')),
  source TEXT NOT NULL DEFAULT 'STAGE5_DIAGNOSTIC' CHECK(source='STAGE5_DIAGNOSTIC'),
  detail JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_stage5_sim_ledger_state ON luxalgo_stage5_sim_order_ledger(state,updated_at);
-- Stage 6 persistent future-execution emergency HALT, default locked OFF.
-- This is NOT an exchange-side position close or a live kill-switch integration.
CREATE TABLE IF NOT EXISTS luxalgo_stage6_execution_halt (
 id SMALLINT PRIMARY KEY CHECK(id=1),
 halted BOOLEAN NOT NULL DEFAULT TRUE,
 reason TEXT NOT NULL DEFAULT 'UNIMPLEMENTED_LIVE_EXECUTION',
 changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO luxalgo_stage6_execution_halt(id,halted,reason)
 VALUES (1,TRUE,'UNIMPLEMENTED_LIVE_EXECUTION') ON CONFLICT(id) DO NOTHING;
CREATE TABLE IF NOT EXISTS luxalgo_stage6_halt_diagnostics (
 event_id TEXT PRIMARY KEY,
 halted BOOLEAN NOT NULL DEFAULT TRUE,
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Stage 8: simulated unknown-acknowledgment quarantine, never wired to Gate.io.
CREATE TABLE IF NOT EXISTS luxalgo_stage8_ack_diagnostics (
  client_id TEXT PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  state TEXT NOT NULL CHECK(state IN ('PREPARED','ACK_UNKNOWN','REVIEW_REQUIRED')),
  source TEXT NOT NULL DEFAULT 'STAGE8_DIAGNOSTIC' CHECK(source='STAGE8_DIAGNOSTIC'),
  note TEXT NOT NULL DEFAULT 'NO_EXCHANGE_ORDER'
);
CREATE INDEX IF NOT EXISTS idx_stage8_diag_state ON luxalgo_stage8_ack_diagnostics(state,updated_at);
-- Stage 9 diagnostic reconciliation fixtures only; no exchange order rows.
CREATE TABLE IF NOT EXISTS luxalgo_stage9_reconcile_diagnostics (
  diagnostic_id TEXT PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  result TEXT NOT NULL CHECK(result IN ('MATCHED','MISSING','PARTIAL_FILL','CONFLICT','UNKNOWN')),
  source TEXT NOT NULL DEFAULT 'STAGE9_FIXTURE_ONLY' CHECK(source='STAGE9_FIXTURE_ONLY'),
  details JSONB NOT NULL DEFAULT '{}'::jsonb
);
-- Stage 10 fixture-only protective coverage audit. No exchange-native orders.
CREATE TABLE IF NOT EXISTS luxalgo_stage10_protection_diagnostics (
  diagnostic_id TEXT PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  result TEXT NOT NULL CHECK(result IN ('PROTECTED_FIXTURE','BLOCKED')),
  source TEXT NOT NULL DEFAULT 'STAGE10_FIXTURE_ONLY' CHECK(source='STAGE10_FIXTURE_ONLY'),
  details JSONB NOT NULL DEFAULT '{}'::jsonb
);
-- Stage 11 isolated atomic risk simulation: never used by exchange execution.
CREATE TABLE IF NOT EXISTS luxalgo_stage11_risk_diagnostics (
 run_id TEXT NOT NULL,
 event_id TEXT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 risk_usdt NUMERIC NOT NULL CHECK(risk_usdt>0),
 margin_usdt NUMERIC NOT NULL CHECK(margin_usdt>0),
 source TEXT NOT NULL DEFAULT 'STAGE11_FIXTURE_ONLY' CHECK(source='STAGE11_FIXTURE_ONLY'),
 PRIMARY KEY(run_id,event_id)
);
CREATE INDEX IF NOT EXISTS idx_stage11_run ON luxalgo_stage11_risk_diagnostics(run_id);
-- Stage 12: counts only from Gate.io authenticated READ-ONLY snapshot.
-- Never stores account credentials, amounts, order IDs or other private exchange data.
CREATE TABLE IF NOT EXISTS luxalgo_stage12_readonly_snapshots (
  snapshot_id TEXT PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  position_count INTEGER NOT NULL CHECK(position_count>=0),
  open_order_count INTEGER NOT NULL CHECK(open_order_count>=0),
  trigger_order_count INTEGER NOT NULL CHECK(trigger_order_count>=0),
  assessment TEXT NOT NULL CHECK(assessment IN ('EMPTY_SNAPSHOT','OBSERVED_UNRECONCILED')),
  source TEXT NOT NULL DEFAULT 'STAGE12_GATE_READONLY' CHECK(source='STAGE12_GATE_READONLY')
);
-- Stage 13: operational-resilience diagnostic audit. No exchange order lifecycle.
CREATE TABLE IF NOT EXISTS luxalgo_stage13_recovery_diagnostics (
  diagnostic_id TEXT PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  observed_at TIMESTAMPTZ NOT NULL,
  status TEXT NOT NULL CHECK(status='DIAGNOSTIC_ONLY'),
  source TEXT NOT NULL DEFAULT 'STAGE13_FAILURE_FIXTURES' CHECK(source='STAGE13_FAILURE_FIXTURES')
);
-- Stage 14 final synthetic integration audit. This is isolated diagnostic data,
-- never a real webhook receipt, bot position, Gate.io order or fill.
CREATE TABLE IF NOT EXISTS luxalgo_stage14_final_diagnostics (
  run_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  side TEXT NOT NULL CHECK(side IN ('LONG','SHORT')),
  state TEXT NOT NULL CHECK(state IN ('PAPER_OPEN','PAPER_TP','PAPER_SL')),
  risk_bps INTEGER NOT NULL CHECK(risk_bps BETWEEN 1 AND 50),
  source TEXT NOT NULL DEFAULT 'STAGE14_SYNTHETIC_ONLY' CHECK(source='STAGE14_SYNTHETIC_ONLY'),
  PRIMARY KEY(run_id,event_id)
);
CREATE INDEX IF NOT EXISTS idx_stage14_run ON luxalgo_stage14_final_diagnostics(run_id);
CREATE TABLE IF NOT EXISTS luxalgo_dry_run_intents (
 event_id TEXT PRIMARY KEY,
 received_at TIMESTAMPTZ NOT NULL,
 symbol TEXT NOT NULL,
 side TEXT NOT NULL,
 timeframe TEXT NOT NULL DEFAULT '15m',
 status TEXT NOT NULL DEFAULT 'DRY_RUN_ONLY'
);
'''

def _db_connect():
    url = os.getenv('DATABASE_URL', '').strip()
    if not url:
        raise RuntimeError('DATABASE_URL is missing')
    return psycopg.connect(url, connect_timeout=5, row_factory=dict_row)

_schema_lock=Lock()
_schema_ready=False

def _ensure_schema(connection):
    """Initialize schema once per worker process, committed before reuse.

    Replaying dozens of DDL statements on every dashboard read can starve
    Render's limited web workers and delay 5m LuxAlgo signal intake.
    Use a separate transaction so the ready flag is never set before COMMIT.
    A failed initialization leaves all callers fail-closed and retryable.
    """
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        url=os.getenv('DATABASE_URL','').strip()
        if not url:
            raise RuntimeError('DATABASE_URL missing for schema initialization')
        with psycopg.connect(url,connect_timeout=5) as schema_conn:
            schema_conn.execute(DB_SCHEMA)
        _schema_ready=True

# Every queue writer and toggle obtains the same DB transaction lock.
# Fail closed if PostgreSQL is unreachable.
BOT_CONTROL_LOCK = 65913042

def _bot_enabled(cur, lock=False):
    if lock:
        cur.execute('SELECT pg_advisory_xact_lock(%s)', (BOT_CONTROL_LOCK,))
    cur.execute('SELECT enabled FROM luxalgo_bot_control WHERE id=1')
    row=cur.fetchone()
    return bool(row and row['enabled'])

@app.get('/terminal/bot-control')
def terminal_bot_control_get():
    if not _terminal_permitted(): return _terminal_guard()
    try:
        with _db_connect() as con:
            _ensure_schema(con)
            with con.cursor() as cur:
                enabled=_bot_enabled(cur)
        return jsonify(bot_enabled=False,stored_historical_toggle=enabled,
                       scope='PRODUCTION_SIGNAL_ONLY', live_trading=False, orders_enabled=False)
    except (psycopg.Error,RuntimeError,OSError):
        return jsonify(error='Bot state unavailable; treat as OFF',bot_enabled=False,orders_enabled=False),503

@app.post('/terminal/bot-control')
def terminal_bot_control_post():
    if not _terminal_permitted(): return _terminal_guard()
    # Reject cross-origin browser submissions. JSON and Basic auth are required.
    origin=request.headers.get('Origin')
    # Render terminates TLS at its proxy; Flask may see http:// internally.
    # Match the browser Origin against our configured public HTTPS origin,
    # not request.host_url (which may use the proxy's internal scheme).
    if origin:
        from urllib.parse import urlsplit
        configured = os.getenv('PUBLIC_BASE_URL', '').strip().rstrip('/')
        if not configured:
            configured = 'https://' + request.host
        public_url = urlsplit(configured)
        origin_url = urlsplit(origin)
        if (public_url.scheme != 'https' or not public_url.hostname or
                public_url.username or public_url.password or public_url.path or
                public_url.query or public_url.fragment or
                origin_url.scheme != 'https' or
                (origin_url.scheme, origin_url.netloc) != (public_url.scheme, public_url.netloc) or
                origin_url.path or origin_url.query or origin_url.fragment):
            return jsonify(error='Origin mismatch'),403
    data=request.get_json(silent=True) if request.is_json else None
    if not isinstance(data,dict) or type(data.get('enabled')) is not bool:
        return jsonify(error='Boolean enabled is required'),400
    desired=data['enabled']
    if desired:
        return jsonify(error='Archived toggle cannot arm Testnet or real-money execution',
                       orders_enabled=False,live_trading=False),409
    try:
        with _db_connect() as con:
            _ensure_schema(con)
            with con.cursor() as cur:
                _bot_enabled(cur,lock=True)
                cur.execute('UPDATE luxalgo_bot_control SET enabled=%s, changed_at=NOW() WHERE id=1',(desired,))
                if not desired:
                    cur.execute("UPDATE luxalgo_auto_paper_queue SET state='REJECTED', checked_at=NOW(),note='Bot manually switched OFF' WHERE state='PENDING'")
        return jsonify(bot_enabled=False,scope='PRODUCTION_SIGNAL_ONLY',
                       orders_enabled=False,live_trading=False)
    except (psycopg.Error,RuntimeError,OSError):
        return jsonify(error='Unable to save bot state; treat as OFF',orders_enabled=False),503

def _db_recent():
    with _db_connect() as con:
        _ensure_schema(con)
        with con.cursor() as cur:
            cur.execute('SELECT COUNT(*) AS total FROM luxalgo_signal_receipts')
            count = cur.fetchone()['total']
            cur.execute('''SELECT received_at, event_id, symbol, side, timeframe FROM luxalgo_signal_receipts r
                           WHERE NOT EXISTS (SELECT 1 FROM luxalgo_hidden_signal_rows h WHERE h.event_id=r.event_id)
                           ORDER BY r.id DESC LIMIT 20''')
            rows = [dict(time=r['received_at'].isoformat(),time_ist=r['received_at'].astimezone(timezone(timedelta(hours=5, minutes=30))).strftime('%Y-%m-%d %H:%M:%S'),symbol=r['symbol'],side=r['side'],tf=r['timeframe'],event_id=r['event_id'])
                    for r in cur.fetchall()]
    return count, rows

def _db_status():
    with _db_connect() as con:
        _ensure_schema(con)
        with con.cursor() as cur:
            cur.execute('SELECT COUNT(*) AS total FROM luxalgo_signal_receipts')
            count = cur.fetchone()['total']
            cur.execute('''SELECT event_id, received_at, symbol, side, timeframe, status
                           FROM luxalgo_dry_run_intents ORDER BY received_at DESC LIMIT 15''')
            intents = [dict(event_id=r['event_id'], time=r['received_at'].isoformat(),
                            symbol=r['symbol'],side=r['side'],tf=r['timeframe'],
                            status=r['status'],orders_placed=0) for r in cur.fetchall()]
    return count, intents

def _db_record(record,event_id):
    # A single transaction both logs delivery and deduplicates a DRY RUN intent.
    with _db_connect() as con:
        _ensure_schema(con)
        with con.cursor() as cur:
            cur.execute('''INSERT INTO luxalgo_signal_receipts
                           (received_at, event_id, symbol, side, timeframe)
                           VALUES (%s, %s, %s, %s, %s)''',
                        (record['dt'],event_id,record['symbol'],record['side'],record['tf']))
            cur.execute('''INSERT INTO luxalgo_dry_run_intents
                           (event_id,received_at,symbol,side,timeframe,status)
                           VALUES (%s,%s,%s,%s,%s,'DRY_RUN_ONLY')
                           ON CONFLICT (event_id) DO NOTHING RETURNING event_id''',
                        (event_id,record['dt'],record['symbol'],record['side'],record['tf']))
            duplicate = cur.fetchone() is None
            # Persist paper intent only if the account owner has enabled the bot.
            if _bot_enabled(cur, lock=True):
                cur.execute('''INSERT INTO luxalgo_auto_paper_queue(event_id,received_at)
                            VALUES (%s,%s) ON CONFLICT(event_id) DO NOTHING''',
                            (event_id,record['dt']))
    return duplicate

PAGE = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>LUXALGO PRO · Trading Terminal</title>
<style>:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#08111f;color:#edf5ff;font:14px/1.5 system-ui,Segoe UI,Arial}.wrap{max-width:1180px;margin:auto;padding:24px}header,.panel,.metric{background:#122238;border:1px solid #29415e;border-radius:12px;padding:19px}header{display:flex;justify-content:space-between;gap:12px;align-items:center}h1{font-size:23px;margin:0}h2{font-size:15px;margin:0 0 15px}p{margin:5px 0;color:#a8bed6}.tag{color:#ffc47c;font-weight:700}.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:15px 0}.metric small{color:#9fb5cf}.metric strong{display:block;margin-top:8px;font-size:18px}.panel{margin-top:14px}.tablewrap{overflow:auto}table{border-collapse:collapse;width:100%;min-width:650px}th,td{padding:11px 9px;border-bottom:1px solid #2b435e;text-align:left}th{color:#a7c0dc;font-size:12px}td{font-size:13px}.light{color:#a8bed6}.ok{color:#47dfae}.off{color:#ffc47c}.btn{background:#203b5c;color:#eef6ff;border:1px solid #46749d;border-radius:7px;padding:8px 12px;cursor:pointer}.btn:disabled{opacity:.5}#planResults{white-space:pre-wrap;font:13px/1.7 ui-monospace,Consolas,monospace;background:#0b1a2b;border:1px solid #2b435e;border-radius:8px;padding:14px}.controls{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin:14px 0}.controls label{display:flex;flex-direction:column;color:#a8bed6;font-size:12px;gap:5px}.controls input,.controls select{background:#0b1a2b;color:#edf5ff;border:1px solid #46749d;border-radius:7px;padding:9px;font:inherit}.controls input:read-only{opacity:.7}.foot{padding:18px 2px;font-size:12px;color:#a8bed6}@media(max-width:800px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}header{display:block}.wrap{padding:12px}}.market-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px}.market-grid .panel{margin-top:0}.market-panel{min-width:0}.panel-heading{display:flex;justify-content:space-between;align-items:center;gap:9px}.panel-heading h2{margin:0}.market-tabs{display:flex;flex-wrap:wrap;gap:6px;margin:12px 0}.market-tabs .btn{font-size:11px;padding:7px}.market-tabs .selected{background:linear-gradient(140deg,#1c5377,#17416a);border-color:#49e3e9;color:#e6fdff;box-shadow:0 0 10px #00c9e740}.market-filters{display:flex;gap:8px;margin-bottom:9px}.market-filters input,.market-filters select{min-width:0;max-width:100%;background:#0b1a2b;border:1px solid #3b5b7c;color:#eaf7ff;border-radius:7px;padding:9px}.market-filters input{flex:1}.market-scroll{max-height:360px;overflow:auto}.market-scroll table{min-width:390px}.market-scroll th{position:sticky;top:0;background:#162c45;z-index:1}.pairlink{border:0;background:transparent;color:#a9e9ff;text-align:left;cursor:pointer;text-decoration:underline;font-weight:700}.gloss-gold,.gloss-cyan,.positive,.negative{font-weight:750}.gloss-gold{background:linear-gradient(#fff7c8,#ffd368 45%,#d68c3f);background-clip:text;-webkit-background-clip:text;color:transparent}.gloss-cyan{background:linear-gradient(#fff,#56eaff 40%,#16a5d9);background-clip:text;-webkit-background-clip:text;color:transparent}.positive{color:#57f6bd;text-shadow:0 0 9px #28dd9550}.negative{color:#ff829e;text-shadow:0 0 8px #f8527450}header h1,h2{background:linear-gradient(180deg,#ffffff 12%,#b9e7ff 56%,#78bcef 100%);-webkit-background-clip:text;background-clip:text;color:transparent}.tag{background:linear-gradient(180deg,#ffeac8,#ffbf58 65%,#c18134);-webkit-background-clip:text;background-clip:text;color:transparent}.metric strong.ok{color:#5bffd1;text-shadow:0 0 8px #3beacb55}@media(max-width:1080px){.market-grid{grid-template-columns:1fr}}

/* Desktop terminal redesign: preserves original JS bindings and backend routes. */
:root{--cyan:#59e5ff;--teal:#33efb5;--red:#ff769b;--gold:#ffd47e}
body{background:radial-gradient(ellipse at 50% -20%,#163b62 0%,#07101d 60%);font-size:14px}
.wrap{max-width:1740px;padding:19px 24px 36px}
header{border-color:#38618a;background:linear-gradient(120deg,#132e4d,#0d1c30);box-shadow:inset 0 1px 0 #8ccfff22,0 12px 28px #0004;padding:22px 24px}
header h1{font-size:26px;font-weight:850;letter-spacing:.3px}
header p{font-size:13px;margin-top:5px}
h2{font-size:15px;font-weight:850;letter-spacing:.3px}
.panel,.metric{background:linear-gradient(162deg,#102943 0%,#102039 62%,#102138 100%);border:1px solid #2a5178;box-shadow:inset 0 1px 0 #8dc7ed1f,0 8px 16px #0002;border-radius:12px}
.panel{padding:18px 19px}
.metric-grid{grid-template-columns:repeat(3,minmax(0,1fr));gap:11px;margin:13px 0}
.metric{padding:17px 14px;min-height:108px}
.metric small{display:block;font-size:11px;letter-spacing:.25px;color:#a4c2dd}
.metric strong{display:block;font-size:21px;margin:11px 0 4px;background:linear-gradient(#ffffff,#a6d8fb);background-clip:text;-webkit-background-clip:text;color:transparent}
.metric span{display:block;color:#a0b9ce;font-size:11px}
.metric .gloss-cyan{background:linear-gradient(#e7fdff,#58dcfc,#2698e3);background-clip:text;-webkit-background-clip:text;color:transparent}
.position-panel{margin-top:12px}
.position-panel table{min-width:950px}
.position-panel .panel-heading{margin-bottom:14px}
.empty-state{text-align:center;color:#a8bed6;padding:24px 15px}
.btn-danger{background:#482236;border-color:#97516b;color:#ffbdc8}
.btn-danger:disabled{opacity:.62;cursor:not-allowed}
#tradeChart{height:390px!important}
.market-grid{grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px}
.market-panel{min-height:470px}
.market-scroll{max-height:385px}
.bottom-grid{display:grid;grid-template-columns:minmax(0,1.65fr) minmax(315px,1fr);gap:12px;margin-top:14px}
.bottom-grid>.panel{margin-top:0}
.perf-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:12px 0}
.perf-grid>div{background:linear-gradient(130deg,#0e1c31,#14314b);border:1px solid #2b4d70;border-radius:8px;padding:12px}
.perf-grid span{display:block;color:#9dbbd6;font-size:11px}
.perf-grid strong{display:block;font-size:19px;margin-top:6px;color:#dcefff}
.label-muted{font-size:10px;color:#a2bbd6;letter-spacing:.4px}
.tag{font-weight:800}
.btn:not(:disabled):hover{border-color:#5ae6e9;box-shadow:0 0 15px #13c9e334}
.positive{background:linear-gradient(#dcfff0,#51edb8 60%,#25b990);background-clip:text;-webkit-background-clip:text;color:transparent;filter:drop-shadow(0 0 4px #24eab244)}
.negative{background:linear-gradient(#ffe5ec,#ff7596 60%,#dd3762);background-clip:text;-webkit-background-clip:text;color:transparent;filter:drop-shadow(0 0 4px #fa487a44)}
@media(max-width:1300px){.metric-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.bottom-grid{grid-template-columns:1fr}}
@media(max-width:900px){.market-grid{grid-template-columns:1fr}.wrap{padding:12px}.metric-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:540px){.metric-grid{grid-template-columns:1fr 1fr}.metric strong{font-size:17px}header h1{font-size:20px}.panel{padding:13px}.btn-danger{font-size:11px}}

/* Only the unused header center area: upward-scrolling, accessible news. */
.terminal-header{display:grid;grid-template-columns:minmax(220px,1fr) minmax(300px,1.65fr) auto;align-items:center;gap:18px}
.terminal-brand{min-width:0}.trading-status{white-space:nowrap;align-self:start;text-align:right}
.news-slot{min-width:0;border:1px solid #365d82;border-radius:9px;background:#07192c;padding:8px 12px;overflow:hidden}
.news-heading{display:flex;justify-content:space-between;gap:8px;color:#ffd584;font-size:10px;font-weight:800;letter-spacing:.6px;margin-bottom:5px}
#newsRefresh{border:1px solid #4383a8;border-radius:5px;background:#153650;color:#d5f6ff;cursor:pointer;font-size:14px;line-height:17px;padding:1px 6px}#newsRefresh:disabled{opacity:.45;cursor:wait}#newsFeedStatus{color:#99b5cd;font-size:10px;font-weight:500;white-space:nowrap}
.news-viewport{height:42px;overflow:hidden;position:relative;mask-image:linear-gradient(transparent 0%,#000 13%,#000 88%,transparent 100%)}
.news-stack{display:flex;flex-direction:column;will-change:transform}
.news-item{height:42px;min-height:42px;display:flex;flex-direction:column;justify-content:center;gap:2px;overflow:hidden}
.news-item a{display:block;color:#daf2ff;text-decoration:none;white-space:nowrap;text-overflow:ellipsis;overflow:hidden;font-size:12px;font-weight:700}
.news-item a:hover,.news-item a:focus-visible{color:#57e6ff;text-decoration:underline}
.news-meta{font-size:10px;color:#95afc6;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.news-empty{font-size:12px;color:#a8bed6;margin-top:10px}
.news-stack.news-animated{animation:news-scroll-up var(--news-duration,48s) linear infinite}
.news-viewport:hover .news-stack,.news-viewport:focus-within .news-stack{animation-play-state:paused}
@keyframes news-scroll-up{from{transform:translateY(0)}to{transform:translateY(-50%)}}
@media(max-width:1200px){.terminal-header{grid-template-columns:1fr auto}.news-slot{grid-column:1/-1;grid-row:2}}
@media(max-width:680px){.terminal-header{grid-template-columns:minmax(0,1fr)}.trading-status{grid-row:2;text-align:left}.news-slot{grid-column:1;grid-row:3}}
@media(prefers-reduced-motion:reduce){.news-stack.news-animated{animation:none}.news-stack .news-item:not(:first-child){display:none}}

/* Compact LuxAlgo signal controls: four actions across one desktop row. */
@media(min-width:1101px){
 .bottom-grid{grid-template-columns:minmax(0,4.3fr) minmax(225px,1fr);gap:10px}
}
.bottom-grid>section:first-child{min-width:0}
.bottom-grid>section:first-child .tablewrap{overflow-x:auto}
.bottom-grid>section:first-child table{min-width:790px;table-layout:auto}
.bottom-grid>section:first-child th:last-child,.bottom-grid>section:first-child td:last-child{min-width:352px;width:44%;white-space:normal}
.bottom-grid>section:first-child td:last-child .btn{display:inline-block;vertical-align:middle;white-space:nowrap;padding:5px 5px;margin:2px 2px 2px 0;font-size:10px;line-height:1.4;border-radius:5px;letter-spacing:0}
.performance-panel{min-width:0}
.performance-panel .panel-heading{flex-wrap:wrap}
.performance-panel .perf-grid{gap:6px}
.performance-panel .perf-grid>div{padding:9px 7px}
.performance-panel .perf-grid strong{font-size:16px}
@media(min-width:1101px) and (max-width:1250px){
 .bottom-grid>section:first-child td:last-child .btn{font-size:9.5px;padding:4px 5px;margin-right:1px}
}
@media(max-width:1100px){
 .bottom-grid{grid-template-columns:1fr}
}
.bot-control-panel{display:flex;justify-content:space-between;align-items:center;gap:12px;margin:12px 0;padding:13px 17px;background:#11253c;border:1px solid #335d82;border-radius:10px}
.bot-control-panel strong{color:#ffc47c;font-size:14px}.bot-control-panel p{font-size:12px;margin:4px 0 0}.bot-control-actions{display:flex;gap:8px;white-space:nowrap}.bot-control-actions .btn{font-weight:800}
@media(max-width:670px){.bot-control-panel{align-items:stretch;flex-direction:column}.bot-control-actions .btn{flex:1}}
/* Requested visual refinements only: avatar size, news top edge, glossy parrot-green letters. */
.profile-avatar{width:52px;height:52px;flex:0 0 52px;border-width:2px;box-shadow:inset 0 1px 2px #ffffff66,0 3px 15px #19c9ff66}
/* Extra height is added ABOVE the original news area; no side or bottom padding is changed. */
.news-slot{padding-top:22px;margin-top:-14px}
.news-heading>span:first-child,.news-item a{
 color:#abff36;
 background:linear-gradient(180deg,#f2ffc6 0%,#ccff67 25%,#a1ff14 50%,#66dc03 73%,#b8ff52 100%);
 -webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;
 filter:drop-shadow(0 0 4px #85ff2380);
}
.news-item a:hover,.news-item a:focus-visible{filter:brightness(1.2) drop-shadow(0 0 7px #92ff43b0);text-decoration:underline}
/* Profile greeting and high-gloss logout; keeps the news center intact. */
.profile-greeting{display:flex;align-items:center;gap:9px;margin-top:11px;min-width:0}
.profile-avatar{width:38px;height:38px;flex:0 0 38px;border-radius:50%;overflow:hidden;
  background:linear-gradient(140deg,#3b8fc8,#1b395c);border:2px solid #79dfff;
  box-shadow:0 2px 12px #19c9ff55;display:grid;place-items:center;font-weight:900;color:#e8fbff}
.profile-avatar img{width:100%;height:100%;object-fit:cover}
.profile-name{font-weight:850;color:#d8f4ff;letter-spacing:.25px;font-size:13px}
.profile-change{display:inline-block;cursor:pointer;color:#8fdfff;font-size:11px;text-decoration:underline;margin-left:2px}
.profile-right{display:flex;flex-direction:column;align-items:flex-end;gap:10px;min-width:140px}
.glossy-logout{border:1px solid #b9e5ff;border-radius:10px;cursor:pointer;padding:10px 21px;
 background:linear-gradient(155deg,#d5f5ff 0%,#8ad5ff 24%,#3d92dc 51%,#13538d 53%,#2176bc 95%);
 color:white;font-weight:900;letter-spacing:1px;text-shadow:0 1px 2px #04365b;
 box-shadow:inset 0 2px 2px #fff9,inset 0 -2px 4px #092b5088,0 5px 17px #0a8aff55}
.glossy-logout:hover{filter:brightness(1.14);box-shadow:inset 0 2px 3px #fff9,0 4px 22px #25b7ff90}
.glossy-logout:focus-visible,.profile-change:focus-visible{outline:3px solid #ffe39e;outline-offset:2px}
@media(max-width:680px){.profile-right{align-items:flex-start}.profile-name{font-size:12px}}

/* Glossy control upgrades — cosmetic only. Distinct from account LOGOUT. */
.btn.gloss-action,.btn.gloss-safe-disabled,.bot-control-actions .btn{
 position:relative;isolation:isolate;overflow:hidden;border-radius:10px;
 font-weight:850;letter-spacing:.22px;padding:10px 15px;
 text-shadow:0 1px 2px #00152bb0;
 transition:box-shadow .18s,transform .18s,filter .18s;
}
.btn.gloss-action::before,.bot-control-actions .btn::before,.btn.gloss-safe-disabled::before{
 content:"";position:absolute;inset:1px 2px 52%;border-radius:9px 9px 50% 50%;
 background:linear-gradient(180deg,#ffffff72,#ffffff0b);pointer-events:none;z-index:-1;
}
.btn.gloss-action{color:#faffff!important;border:1px solid #6ed4fd;
 background:linear-gradient(160deg,#47bdf5 0%,#1171b6 47%,#084c8d 49%,#157dc7 100%);
 box-shadow:inset 0 1px 0 #d0f7ff98,inset 0 -2px 2px #031d4c80,0 3px 11px #007bc95e,0 0 16px #38aeef22;
}
#radarRefresh{border-color:#4dfbd4;background:linear-gradient(160deg,#46dcca,#078c9a 48%,#075c82 50%,#0aafa9 100%);box-shadow:inset 0 1px 0 #cefffca6,0 3px 12px #14ddc74d}
#scannerRefresh{border-color:#d7a8ff;background:linear-gradient(160deg,#c292f8,#7848bc 48%,#48227c 50%,#8856ce 100%);box-shadow:inset 0 1px 0 #fcdeffa6,0 3px 12px #9559e85b}
#paperRefresh{border-color:#ffc573;background:linear-gradient(160deg,#ffde7a,#d18b22 48%,#a76b15 50%,#dfaa3c 100%);color:#161f31!important;text-shadow:0 1px 1px #ffffff78;box-shadow:inset 0 1px 0 #fff3ba9c,0 3px 12px #e9a02d5b}
#previewButton{border-color:#78f9b8;background:linear-gradient(160deg,#52eaba,#159d70 48%,#07624f 50%,#1ab785 100%);box-shadow:inset 0 1px 0 #d5ffefa6,0 3px 12px #17ca9257}
.btn.gloss-action:not(:disabled):hover,.bot-control-actions .btn:not(:disabled):hover{filter:brightness(1.19);transform:translateY(-1px);box-shadow:inset 0 1px 0 #ffffffa6,0 5px 15px #41b1ef8c,0 0 20px #5defff5a}
.btn.gloss-action:not(:disabled):active,.bot-control-actions .btn:not(:disabled):active{transform:translateY(1px)}
.btn.gloss-action:focus-visible,.bot-control-actions .btn:focus-visible{outline:3px solid #efffff;outline-offset:3px}
.btn.gloss-action:disabled{opacity:.54;filter:saturate(.55);cursor:not-allowed;box-shadow:none}
/* Exchange close is deliberately a visible but INOPERABLE safety control. */
.btn.gloss-safe-disabled:disabled{opacity:.68;cursor:not-allowed;color:#ffdfdf!important;
 background:linear-gradient(160deg,#845665,#57283c 48%,#391c2d 50%,#6c344b 100%);
 border:1px solid #b1788e;box-shadow:inset 0 1px 0 #efbdca55,0 2px 10px #5b183544}
.bot-control-actions #botTurnOn{color:#eefff5;border:1px solid #6effb6;background:linear-gradient(160deg,#5ff3ae,#16aa68 48%,#086c44 50%,#20c87f 100%);box-shadow:inset 0 1px 0 #dcffe1a0,0 3px 11px #18c77765}
.bot-control-actions #botTurnOff{color:#fff0f0;border:1px solid #ff8da3;background:linear-gradient(160deg,#ff8ba4,#de335e 48%,#921f3e 50%,#e84b72 100%);box-shadow:inset 0 1px 0 #ffe5ec8f,0 3px 11px #ee466570}
.bot-control-actions .btn:disabled{cursor:not-allowed;opacity:.37;filter:saturate(.55);box-shadow:none}
/* Emphasize available next action, not the already selected state. */
.bot-control-actions .btn.next-action:not(:disabled){filter:brightness(1.12);box-shadow:inset 0 1px 0 #ffffffb5,0 0 0 1px #ffffff52,0 4px 18px currentColor}
@media(max-width:650px){.btn.gloss-action,.btn.gloss-safe-disabled,.bot-control-actions .btn{padding:9px 11px;font-size:12px}}
@media(prefers-reduced-motion:reduce){.btn.gloss-action,.bot-control-actions .btn{transition:none}}

/* Live read-only balance indicators. Figures never use a manually-entered balance. */
.equity-metric{border-color:#3e8fb0;box-shadow:inset 0 1px 0 #a8eeff44,0 0 14px #29a8df20}
.equity-metric strong{font-size:25px}

/* Only the three Gate.io Futures balance cards are collapsible. */
.futures-liquidity{margin:13px 0 0;background:linear-gradient(130deg,#112b43,#102039);border:1px solid #376886;border-radius:12px;overflow:hidden}
.futures-liquidity>summary{cursor:pointer;display:flex;align-items:center;justify-content:space-between;gap:12px;list-style:none;padding:13px 17px;font-weight:850;letter-spacing:.25px;color:#a6f0ff;background:linear-gradient(180deg,#1c4463,#112a43);user-select:none}
.futures-liquidity>summary::-webkit-details-marker{display:none}
.futures-liquidity>summary:hover{filter:brightness(1.15)}
.futures-liquidity>summary:focus-visible{outline:3px solid #93ebff;outline-offset:-3px}
.futures-liquidity .liquidity-arrow{font-size:19px;line-height:1;transition:transform .2s ease;color:#8bebff}
.futures-liquidity[open] .liquidity-arrow{transform:rotate(180deg)}
.futures-liquidity .liquidity-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:11px;padding:13px}
.futures-liquidity .liquidity-grid .metric{min-width:0}
@media(max-width:670px){.futures-liquidity .liquidity-grid{grid-template-columns:1fr;gap:9px;padding:10px}}

/* Individual read-only position cards; each position is independently expandable. */
.position-card-list{display:grid;gap:13px;margin-top:13px}
.position-card{border:1px solid #345c83;border-radius:13px;background:linear-gradient(150deg,#142d49,#0b1c31);box-shadow:inset 0 1px 0 #ffffff12,0 8px 20px #0003;overflow:hidden}
.position-card>summary{list-style:none;cursor:pointer;padding:15px 17px;display:flex;justify-content:space-between;align-items:center;gap:10px}
.position-card>summary::-webkit-details-marker{display:none}
.position-card>summary:focus-visible{outline:2px solid #66e6ff;outline-offset:-3px}
.position-head{display:flex;align-items:center;gap:9px;flex-wrap:wrap;min-width:0}
.position-symbol{font-size:17px;font-weight:850;color:#e8f7ff}
.position-side{font-size:12px;font-weight:850;border-radius:5px;padding:4px 7px}
.position-side.long{background:#17765c;color:#d9fff1}.position-side.short{background:#9b3453;color:#fff0f3}
.position-leverage{font-size:12px;background:#223e5c;border:1px solid #42688c;color:#d7e9fa;padding:4px 8px;border-radius:5px}
.position-summary-pnl{font-weight:850;font-size:17px;white-space:nowrap}
.position-card-inner{padding:0 17px 17px}
.position-facts{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}
.position-fact{background:#0b2037;border:1px solid #234969;border-radius:8px;padding:10px;min-width:0}
.position-fact small{display:block;color:#9eb9d1;font-size:11px;margin-bottom:3px}
.position-fact strong{font-size:15px;overflow-wrap:anywhere}
.position-card-actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
.position-card-actions button:disabled{opacity:.44;cursor:not-allowed;filter:grayscale(.35)}
.position-chart-action{background:linear-gradient(150deg,#63d6ff,#227bb0 53%,#104d89 54%,#319bd0);border-color:#83deff;font-weight:750}
.position-readonly-hint{font-size:11px;color:#afc3d9;margin-top:10px}
@media(max-width:970px){.position-facts{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:500px){.position-card>summary{align-items:flex-start}.position-facts{grid-template-columns:repeat(2,minmax(0,1fr))}.position-symbol{font-size:14px}.position-summary-pnl{font-size:14px}}

/* Distinct high-gloss RED Delete action; isolated from other signal buttons. */
.bottom-grid>section:first-child td:last-child .btn.delete-recent-signal {
 color:#fff7fa!important;
 background:linear-gradient(160deg,#ffb0b9 0%,#f45068 39%,#9c193e 52%,#dc3558 100%)!important;
 border:1px solid #ff8098!important;
 font-weight:900;
 text-shadow:0 1px 2px #560c24;
 box-shadow:inset 0 1px 0 #ffe4e8b5,inset 0 -2px 2px #470a22a0,0 2px 9px #e7325875!important;
}
.bottom-grid>section:first-child td:last-child .btn.delete-recent-signal:not(:disabled):hover {
 filter:brightness(1.17);box-shadow:inset 0 1px 0 #fff,0 0 14px #ff507dc4!important;
}
.bottom-grid>section:first-child td:last-child .btn.delete-recent-signal:focus-visible {
 outline:2px solid #ffe0e8;outline-offset:2px;
}

/* Advanced scanner is a separate, read-only market intelligence panel. */
.advanced-panel{border-color:#37779b}.adv-controls{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin:10px 0}.adv-table{overflow-x:auto;max-height:480px;overflow-y:auto}.adv-table table{min-width:1030px}.adv-table th{position:sticky;top:0;background:#193955}.adv-summary{color:#b1d3e9;font-size:12px}.adv-signal{font-weight:850}.adv-signal.long{color:#60e9b6}.adv-signal.short{color:#ff8ca3}.adv-reason{font-size:11px;color:#f6cb92}.adv-blocked{color:#ffc994}
/* Paper-bot state indicator beside paper-simulation title. Never represents live trading. */
.bot-status-line{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.bot-state-indicator{font-size:12px;font-weight:900;letter-spacing:.25px;padding:3px 10px;border-radius:999px;border:1px solid #64748b;background:#273447;color:#ccd7e3;white-space:nowrap}
.bot-state-indicator.on{color:#d6ffe9;border-color:#29d982;background:#0a6145;box-shadow:0 0 10px #25e68c66}
.bot-state-indicator.off{color:#ffe7e9;border-color:#f66a79;background:#812d3b;box-shadow:0 0 10px #f03b5766}
.bot-state-indicator.unknown{color:#f5dfb0;border-color:#997a38;background:#494027}

/* V4: approved Binance-inspired position workspace, visual layer only */
.wrap{max-width:1510px!important;padding:18px 22px 36px!important}
header.terminal-header{border-radius:17px!important}
.metric-grid{display:grid!important;grid-template-columns:repeat(6,minmax(0,1fr))!important;gap:8px!important;margin:12px 0!important}
.metric-grid .metric{min-height:75px!important;padding:10px 12px!important;border-radius:9px!important}
.metric-grid .metric strong{font-size:17px!important;margin:4px 0!important}
.metric-grid .metric small,.metric-grid .metric span{font-size:10px!important}
#v4Workspace{background:linear-gradient(155deg,#122943,#0d2137);border:1px solid #335575;border-radius:16px;box-shadow:0 10px 24px #0003;margin-top:14px;overflow:hidden}
.v4-head{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;padding:18px 20px;border-bottom:1px solid #2e4864}
.v4-head strong{font-size:16px;letter-spacing:.2px}.v4-head small{color:#9ab5d0}
.v4-tabs{padding:14px 20px 9px;display:flex;gap:23px;color:#a5b8cc;font-size:17px;align-items:center}
.v4-tabs strong{border-bottom:4px solid #eacc53;color:#edf7ff;padding-bottom:8px}
#v4PaperCards{padding:0 20px 12px}
.v4-trade{padding:20px 2px 22px;border-top:1px solid #35506b}
.v4-trade-top{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.v4-token{border:0;background:transparent;color:#eff7ff;font-size:22px;font-weight:900;padding:3px 0;cursor:pointer;text-decoration:underline;text-decoration-color:#5abdfa;text-underline-offset:5px}
.v4-token:hover{color:#77d8ff}
.v4-side{padding:3px 8px;border-radius:5px;font-weight:900;background:#18885b;color:white}.v4-side.short{background:#ba4159}
.v4-tag{background:#283a50;color:#d4e4ef;border:1px solid #52657a;border-radius:5px;padding:3px 7px;font-size:12px}
.v4-numbers{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px 10px;margin-top:15px}
.v4-number-label{font-size:12px;color:#a7bacc;border-bottom:1px dotted #526b81;display:inline-block}
.v4-number-value{font-size:20px;margin-top:6px;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.v4-number-value.big{font-size:27px;font-weight:900}
.v4-pnl-good{color:#27d898}.v4-pnl-bad{color:#ff677c}
.v4-controls{display:grid;grid-template-columns:repeat(4,1fr);gap:11px;margin-top:19px}
.v4-controls button{border:1px solid #51657c;border-radius:9px;background:#35455b;color:#ecf4ff;padding:12px 8px;font-weight:700;cursor:not-allowed;opacity:.65}
.v4-prompt{padding:24px 20px;color:#abc4dc;font-size:14px}
#v4DemoToggle{border:1px solid #60abda;background:#163f60;color:#e8f7ff;border-radius:8px;padding:9px 13px;cursor:pointer}
.v4-demo-banner{background:#553821;border:1px solid #a67938;color:#ffe1a1;border-radius:9px;padding:10px 13px;margin:0 20px 12px}
#v4ChartModal{position:fixed;inset:0;z-index:10000;background:#020a16e8;display:none;align-items:center;justify-content:center;padding:20px}
#v4ChartModal.open{display:flex}
#v4ModalBox{background:#10253e;border:1px solid #4d7faa;width:min(1230px,96vw);max-height:95vh;overflow:auto;border-radius:16px;padding:14px 18px;box-shadow:0 20px 70px #000b}
#v4ModalHeader{display:flex;justify-content:space-between;align-items:center;gap:9px;padding:5px 0 10px}
#v4ModalHeader strong{font-size:21px}
#v4CloseChart{background:#57283b;color:#fff;border:1px solid #bb6481;padding:10px 14px;border-radius:8px;cursor:pointer}
#v4ChartMount .panel{margin:0}
#v4ChartMount #tradeChart{height:550px!important}
.v4-tools{font-size:12px;color:#b7cce2;padding:10px 0}
#v4LegacyPaperHost{display:none!important}
#v4LegacyGateHost{display:none!important}
#v4ChartOriginal{display:none!important}
.bottom-grid{grid-template-columns:1fr!important}
.bottom-grid>section:first-child{width:100%}
.bottom-grid>section:first-child .tablewrap{max-height:410px;overflow:auto}
#v4RecentWrapper:not([open]){padding:0!important}
#v4RecentWrapper>summary{list-style:none;padding:16px 18px;font-weight:900;cursor:pointer}
#v4RecentWrapper>summary::-webkit-details-marker{display:none}
#v4RecentWrapper[open]>summary{border-bottom:1px solid #33516a}
#v4RecentWrapper>.bottom-grid{margin:0;padding:9px}
#v4RecentWrapper .panel{margin:0}
@media(max-width:900px){.metric-grid{grid-template-columns:repeat(3,1fr)!important}.v4-numbers{grid-template-columns:repeat(2,minmax(0,1fr))}.v4-controls{grid-template-columns:repeat(2,1fr)}}
@media(max-width:570px){.metric-grid{grid-template-columns:repeat(2,1fr)!important}.v4-token{font-size:18px}.v4-number-value{font-size:16px}.v4-number-value.big{font-size:21px}#v4PaperCards{padding:0 11px 11px}.v4-head{padding:14px 12px}}

/* V4.1 compact two-up position monitor — visual change only, no trading logic changes. */
#v4PaperCards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;padding:12px 16px 17px;align-items:stretch}
#v4PaperCards>.v4-trade{min-width:0;margin:0;padding:13px 14px 14px;border:1px solid #355a7b;border-radius:12px;background:linear-gradient(145deg,#142d49 0%,#0a1d32 100%);box-shadow:inset 0 1px 0 #ffffff10,0 3px 10px #0003}
#v4PaperCards>.v4-prompt{grid-column:1/-1}
#v4PaperCards .v4-trade-top{gap:7px;align-items:flex-start}
#v4PaperCards .v4-trade-top>div:first-child{gap:6px!important}
#v4PaperCards .v4-token{font-size:17px;line-height:1.3}
#v4PaperCards .v4-tag{font-size:10px;padding:3px 5px}
#v4PaperCards .v4-side{font-size:11px;padding:3px 6px}
#v4PaperCards .v4-numbers{grid-template-columns:repeat(4,minmax(0,1fr));gap:9px 7px;margin-top:12px}
#v4PaperCards .v4-number-label{font-size:10px;line-height:1.3;letter-spacing:0}
#v4PaperCards .v4-number-value{font-size:14px;font-weight:700;margin-top:4px}
#v4PaperCards .v4-number-value.big{font-size:19px;font-weight:900}
#v4PaperCards .v4-controls{gap:6px;margin-top:13px}
#v4PaperCards .v4-controls button{font-size:11px;padding:9px 3px;border-radius:7px}
@media(max-width:1020px){ #v4PaperCards{grid-template-columns:1fr}#v4PaperCards .v4-token{font-size:19px}}
@media(max-width:570px){ #v4PaperCards{padding:10px;gap:9px}#v4PaperCards .v4-numbers{grid-template-columns:repeat(2,minmax(0,1fr))}#v4PaperCards .v4-controls{grid-template-columns:repeat(2,minmax(0,1fr))}}
/* V4.5 single-select card-width TradingView indicator widget */
/* LuxAlgo incoming monitor: retain 25 signals but scroll after about ten compact rows. */
.incoming-lux-scroll{max-height:535px;overflow-y:auto;overflow-x:auto;overscroll-behavior:contain;scrollbar-gutter:stable;border:1px solid #294b67;border-radius:8px}
.incoming-lux-scroll table{font-size:11px;line-height:1.27;border-collapse:separate;border-spacing:0}
.incoming-lux-scroll thead th{position:sticky;top:0;z-index:3;background:#12314d;box-shadow:0 1px 0 #53718a;padding:8px 7px}
.incoming-lux-scroll tbody td{padding:7px 7px;vertical-align:middle}
.incoming-lux-scroll tbody td .btn{padding:6px 9px;font-size:10px;line-height:1.2}
#incomingLuxInfo{font-size:11px}
@media(max-width:700px){.incoming-lux-scroll{max-height:505px}}
#v4PaperCards>.v4-trade{align-self:start}
.v45-tools{display:flex;gap:7px;align-items:center;flex-wrap:wrap}
.v45-refresh{width:30px;height:30px;display:grid;place-items:center;border:1px solid #64eaff;border-radius:50%;color:#f0feff;font-size:18px;font-weight:900;cursor:pointer;background:linear-gradient(145deg,#c8faff 0%,#46bfff 20%,#136da9 48%,#06385f 50%,#269ee3 100%);box-shadow:inset 0 1px 2px #fff9,0 0 11px #1ec8ff67;text-shadow:0 1px 1px #00416b}
.v45-refresh:disabled{opacity:.6;cursor:wait}
.v45-indicator-toggle{border:1px solid #6bc6fc;border-radius:7px;padding:5px 8px;font-size:10px;letter-spacing:.15px;font-weight:800;color:#eaffff;cursor:pointer;background:linear-gradient(155deg,#5acafa,#136aa7 45%,#103b77 50%,#207eae);box-shadow:inset 0 1px 2px #ffffff85,0 2px 8px #00a8ff40}
.v45-indicator-toggle[aria-expanded="true"]{background:linear-gradient(140deg,#296c90,#0c3e60)}
.v45-indicator{margin-top:12px;border:1px solid #35799c;border-radius:10px;background:#07182b;overflow:hidden}
.v45-indicator-head{padding:7px 9px;display:flex;justify-content:space-between;gap:8px;align-items:center;font-weight:800;font-size:11px}
.v45-widget{width:100%;height:305px;min-height:305px;background:#071628;overflow:hidden}
.v45-widget tv-advanced-chart{display:block;width:100%;height:100%}
.v45-indicator-foot{padding:6px 9px;color:#a4c6e2;font-size:10px;border-top:1px solid #285276}
.v45-entry{color:#ffda7c;font-weight:800}
.v45-refresh-state{font-size:10px;color:#99bbd4;margin-top:7px}
@media(max-width:550px){.v45-widget{height:265px;min-height:265px}}
/* Testnet-only terminal: retain hidden legacy paper and audit nodes for historical compatibility. */
#liveReadinessPanel,.legacy-paper-panel,#paperClosedHistoryPanel,#v4DemoToggle,#v4DemoWarning,#v4PaperCardsStatus{display:none!important}
.bot-control-panel{display:flex!important;gap:12px;align-items:center;flex-wrap:wrap;margin:10px 0}
.bot-control-panel .bot-control-actions .btn{font-size:11px;padding:6px 10px;min-width:75px}
.live-ops-banner{margin:14px 0;padding:16px 19px;border:1px solid #47779a;border-radius:12px;background:linear-gradient(110deg,#102b45,#081b2f);display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:10px}
.live-ops-banner strong{font-size:17px;color:#e1f5ff}.live-ops-banner p{margin:5px 0 0;color:#b0cee4;font-size:12px}
.live-ops-state{font-weight:900;color:#ffd18d;border:1px solid #9c7650;border-radius:8px;padding:9px 12px;background:#3b2b1b}
</style><script type="module" src="https://widgets.tradingview-widget.com/w/en/tv-advanced-chart.js"></script></head><body><div class="wrap"><header class="terminal-header"><div class="terminal-brand"><h1>LUXALGO PRO · TESTNET FUTURES TERMINAL</h1><p>Gate.io USDT Perpetual · LuxAlgo 5m / 15m · Coded by · SUBHASISH</p>
<div class="profile-greeting"><div class="profile-avatar" id="avatarDisplay"><span>SD</span></div>
<div><div class="profile-name">Hello SUBHASISH</div><label class="profile-change" for="profileImageInput" tabindex="0">Change Photo</label><input type="file" id="profileImageInput" accept="image/png,image/jpeg,image/webp" hidden></div></div></div><section class="news-slot" aria-label="Latest crypto news"><div class="news-heading"><span>● CRYPTO NEWS</span><span style="display:flex;align-items:center;gap:7px"><span id="newsFeedStatus">Loading headlines…</span><button type="button" id="newsRefresh" aria-label="Refresh crypto news" title="Refresh crypto news">↻</button></span></div><div class="news-viewport" id="newsViewport"><div class="news-stack" id="newsStack"><span class="news-empty">Retrieving publisher headlines…</span></div></div></section><div class="profile-right">
<form method="post" action="/logout"><input type="hidden" name="csrf_token" value="{{ csrf_token|e }}"><button class="glossy-logout" type="submit">⇥ LOGOUT</button></form><span id="signalLiveSync" role="status" aria-live="polite" style="font-size:11px;color:#9de7ff">● Signals auto-sync: connecting…</span></div></header>
 <section class="live-ops-banner" aria-label="Live trading execution status"><div><strong>GATE.IO FUTURES · TESTNET TRADING MODE</strong><p>Only Gate.io Futures TESTNET automatic orders are permitted. Entry and native protection must pass exchange validation. Real-money orders remain disabled.</p></div><span class="live-ops-state">TESTNET ONLY</span></section>
<section class="bot-control-panel" aria-label="Remote bot control">
 <div><div class="bot-status-line"><strong id="botControlLabel">BOT OFF · TESTNET SIGNALS PAUSED</strong><span id="botStateIndicator" class="bot-state-indicator unknown" role="status" aria-live="polite">● CHECKING</span></div><p id="botControlDetail">New LuxAlgo signals remain logged. No live orders are possible.</p></div>
 <div class="bot-control-actions"><button class="btn" id="botTurnOn" type="button" title="Controls fresh LuxAlgo testnet signals only; NEVER real-money orders">▶ TESTNET BOT ON</button><button class="btn" id="botTurnOff" type="button" title="Pauses new Testnet entries; does not close existing Testnet positions">■ TESTNET BOT OFF</button></div>
</section>

<details class="futures-liquidity" id="futuresLiquidityDetails">
<summary><span>◈ FUTURES LIQUIDITY DETAILS · USDT</span><span class="liquidity-arrow" aria-hidden="true">⌄</span></summary>
<div class="liquidity-grid">
<div class="metric equity-metric"><small>LIVE FUTURES EQUITY · USDT</small><strong class="gloss-cyan" id="futuresEquity">—</strong><span id="futuresEquityUpdated">Verifying Gate.io account…</span></div>
<div class="metric"><small>AVAILABLE FUTURES · USDT</small><strong id="futuresAvailable">—</strong><span>Exchange-reported trading availability</span></div>
<div class="metric"><small>FUTURES BALANCE · USDT</small><strong id="futuresBalance">—</strong><span>Account balance before unrealized P&amp;L</span></div>
</div></details>
<details class="panel" style="margin:12px 0" id="liveReadinessPanel"><summary style="font-weight:850;cursor:pointer;color:#ffcf89">LIVE EXECUTION READINESS · SAFETY CHECK (READ ONLY)</summary><p class="light">This panel checks safeguards only. It cannot start trading or enable Gate.io API writes.</p><button class="btn" type="button" id="readinessRefresh">Check readiness</button><button class="btn gloss-action" type="button" id="guardTestButton" style="margin-left:8px">Test Duplicate Order Guard</button><button class="btn gloss-action" type="button" id="riskCheckButton" style="margin-left:8px">Check Account Risk Limits</button><button class="btn gloss-action" type="button" id="stage2GateTestButton" style="margin-left:8px">Test Stage 2 Risk Gate</button><button class="btn gloss-action" type="button" id="stage3ContractTestButton" style="margin-left:8px">Test Stage 3 Contract Sizing</button><button class="btn gloss-action" type="button" id="stage4ProtectiveTestButton" style="margin-left:8px">Test Stage 4 SL/TP</button><button class="btn gloss-action" type="button" id="stage5LedgerTestButton" style="margin-left:8px">Test Stage 5 Ledger</button><button class="btn gloss-action" type="button" id="stage6HaltTestButton" style="margin-left:8px">Test Stage 6 Halt &amp; Recovery</button><button class="btn gloss-action" type="button" id="stage7AuditButton" style="margin-left:8px">Test Stage 7 Readiness Audit</button><button class="btn gloss-action" type="button" id="stage8AckTestButton" style="margin-left:8px">Test Stage 8 Ack Recovery</button><button class="btn gloss-action" type="button" id="stage9ReconcileTestButton" style="margin-left:8px">Test Stage 9 Reconciliation</button><button class="btn gloss-action" type="button" id="stage10ProtectionTestButton" style="margin-left:8px">Test Stage 10 SL/TP Coverage</button><button class="btn gloss-action" type="button" id="stage11RiskTestButton" style="margin-left:8px">Test Stage 11 Atomic Risk</button><button class="btn gloss-action" type="button" id="stage12ReadOnlyTestButton" style="margin-left:8px">Test Stage 12 Gate.io Read-Only</button><button class="btn gloss-action" type="button" id="stage13RecoveryTestButton" style="margin-left:8px">Test Stage 13 Recovery &amp; Failures</button><button class="btn gloss-action" type="button" id="stage14FinalTestButton" style="margin-left:8px">Test Stage 14 Final Paper Audit</button><button class="btn gloss-action" type="button" id="stage15IntegrationTestButton" style="margin-left:8px">Test Stage 15 Real Alert Paper Audit</button><button class="btn gloss-action" type="button" id="stage15InspectButton" style="margin-left:8px">Inspect Stage 15 Record Issues</button><button class="btn btn-danger" type="button" id="stage6EmergencyHalt" style="margin-left:8px">EMERGENCY HALT · PAPER &amp; FUTURE EXECUTION</button><p class="light" id="riskCheckResult" role="status" aria-live="polite">Read-only risk inspection available. Never authorizes a trade.</p><p class="light" id="guardTestResult">Test only: all candidates are BLOCKED; no exchange orders.</p><p class="light" id="stage2GateTestResult" role="status" aria-live="polite">Stage 2 PostgreSQL concurrency test not yet run.</p><p class="light" id="stage3ContractTestResult" role="status" aria-live="polite">Stage 3 read-only sizing diagnostic not yet run.</p><p class="light" id="stage4ProtectiveTestResult" role="status" aria-live="polite">Stage 4 protective-price validation not yet run. No protective orders exist.</p><p class="light" id="stage5LedgerTestResult" role="status" aria-live="polite">Stage 5 PostgreSQL simulated lifecycle test not yet run. No exchange writes.</p><p class="light" id="stage6HaltTestResult" role="status" aria-live="polite">Stage 6 persistent halt diagnostic not yet run. No exchange writes.</p><p class="light" id="stage7AuditResult" role="status" aria-live="polite">Stage 7 read-only readiness audit not yet run. All exchange execution blocked.</p><p class="light" id="stage8AckTestResult" role="status" aria-live="polite">Stage 8 unknown-acknowledgment quarantine not yet tested. No exchange write.</p><p class="light" id="stage9ReconcileTestResult" role="status" aria-live="polite">Stage 9 fixture-only reconciliation test not yet run. Real Gate.io orders are not reconciled.</p><p class="light" id="stage10ProtectionTestResult" role="status" aria-live="polite">Stage 10 protective-order fixture audit not yet run. No exchange protections verified.</p><p class="light" id="stage11RiskTestResult" role="status" aria-live="polite">Stage 11 account-wide risk fixture not yet tested. Exchange orders disabled.</p><p class="light" id="stage12ReadOnlyTestResult" role="status" aria-live="polite">Stage 12 read-only exchange snapshot not yet run. Live order reconciliation is NOT implemented.</p><p class="light" id="stage13RecoveryTestResult" role="status" aria-live="polite">Stage 13 restart/failure diagnostic not yet run. No exchange writes.</p><p class="light" id="stage14FinalTestResult" role="status" aria-live="polite">Stage 14 final simulated paper-trading audit not yet run. Live execution remains blocked.</p><p class="light" id="stage15IntegrationTestResult" role="status" aria-live="polite">Stage 15 real-alert paper audit not yet run. Requires genuine TradingView alerts; no test order placed.</p><pre id="stage15InspectResult" role="status" aria-live="polite" style="display:none;white-space:pre-wrap;overflow-wrap:anywhere;background:#081b2e;border:1px solid #345c83;border-radius:9px;padding:12px;color:#d9ecff;max-height:440px;overflow:auto">No read-only breakdown requested.</pre><p class="light" id="stage6HaltState" role="status" aria-live="polite">Future execution stays HALTED; checking database status…</p><div id="readinessOutput" role="status" aria-live="polite" style="margin-top:10px;color:#fdbfbf">Live execution BLOCKED until all critical safeguards are implemented and tested.</div></details>
<div class="grid metric-grid">
<div class="metric"><small>GATE.IO ACCOUNT MONITOR</small><strong class="gloss-cyan">CONNECTED · READ MODE</strong><span id="gatePositionStatus">Checking position feed…</span></div>
<div class="metric"><small>OPEN POSITIONS</small><strong id="openPositionCount">—</strong><span>Verified Gate.io positions only</span></div>
<div class="metric"><small>UNREALIZED P&amp;L (USDT)</small><strong id="unrealizedPositionPnl">—</strong><span>Read-only, refreshed every 30s</span></div>
<div class="metric"><small>TOTAL REALIZED P&amp;L</small><strong>—</strong><span>Verified fills not available</span></div>
<div class="metric"><small>WIN RATE</small><strong>—</strong><span>Requires completed trades</span></div>
<div class="metric"><small>DATABASE SIGNALS</small><strong>{{ count }}</strong><span>Webhook: {{ 'configured' if configured else 'not configured' }}</span></div>
</div><section class="panel position-panel"><div class="panel-heading"><h2>OPEN POSITIONS <span class="gloss-cyan">· GATE.IO FUTURES</span></h2><button class="btn gloss-action" id="positionRefresh" type="button">↻ Refresh Positions</button></div>
<div id="gatePositionsBody" class="position-card-list" aria-live="polite"><p class="light">Checking read-only Gate.io open positions…</p></div><p id="gatePositionsUpdated" class="light">Read-only Gate.io account data; manual close disabled. SL/TP order status is not synchronized.</p></section>
<section class="panel" id="v4Workspace" aria-label="Gate.io Testnet individual position cards">
<div class="v4-head"><div><strong>LUXALGO PRO · TESTNET POSITION CARDS</strong><p class="light">Gate.io Testnet exchange positions only. Unrealized P&amp;L comes from the exchange; real-money trading is OFF.</p></div>
<button id="v4DemoToggle" type="button">SHOW SAMPLE LAYOUT</button></div>
<div class="v4-tabs"><strong id="v4TabCount">Loading Gate.io Testnet positions…</strong><span>TESTNET EXCHANGE ONLY · REAL MONEY OFF</span></div>
<div id="v4DemoWarning" hidden class="v4-demo-banner">SAMPLE LAYOUT ONLY — Sample positions, balances and profit figures are fabricated for visual review, never actual trades.</div>
<div style="display:flex;align-items:center;justify-content:space-between;gap:8px;padding:12px 18px 0;flex-wrap:wrap"><strong>GATE.IO TESTNET · EXCHANGE POSITION CARDS</strong><button class="btn gloss-action" type="button" id="v4TestnetRefresh">↻ Refresh Testnet</button></div>
<p id="v4TestnetCardsStatus" class="light" role="status" aria-live="polite" style="padding:3px 18px">Checking Gate.io Testnet positions and native stop/target protection…</p>
<div id="v4PaperCards" role="status" aria-live="polite"><div class="v4-prompt">Loading Gate.io Testnet positions from the exchange…</div></div>
<p id="v4PaperCardsStatus" class="light" role="status" style="padding:0 18px 14px">Paper trade cards will update automatically. Real-money trading stays OFF.</p>
</section>
<section class="panel" id="testnetReadinessPanel" style="width:100%;margin:14px 0;max-width:none" aria-label="Gate.io Testnet execution readiness">
<div class="panel-heading"><h2>TESTNET EXECUTION READINESS <span class="gloss-cyan">· GATE.IO</span></h2>
<button class="btn gloss-action" id="testnetReadinessRefresh" type="button">↻ Check Testnet Status</button></div>
<p class="light">Actual Testnet API readback, not paper-trade status. Real-money execution remains OFF.</p>
<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(150px,1fr))">
  <div class="metric"><small>TESTNET CONNECTION</small><strong id="tnReadConnection">Checking…</strong></div>
  <div class="metric"><small>HEDGE MODE</small><strong id="tnReadHedge">Checking…</strong></div>
  <div class="metric"><small>ACCOUNT LEVERAGE</small><strong id="tnReadLeverage">Checking…</strong></div>
  <div class="metric"><small>PROTECTION API ACCESS</small><strong id="tnReadProtection">Checking…</strong></div>
  <div class="metric"><small>TRIAL READINESS</small><strong id="tnReadStatus">Checking…</strong></div>
  <div class="metric"><small>REAL-MONEY ORDERS</small><strong class="off">OFF</strong></div>
</div>
<p id="tnReadBlockers" class="light" role="status">Checking exchange readiness…</p>
<p id="tnReadChecked" class="light">Last API check: —</p>
<p id="tnReadJournal" class="light">Testnet order journal: checking…</p>
<p id="tnReadLatestSignal" class="light" role="status">Latest LuxAlgo signal: checking Testnet exchange execution…</p>
</section>
<details id="gateTestnetPositionPanel" class="panel" open style="width:100%;margin:14px 0;max-width:none">
<summary style="cursor:pointer;font-size:16px;font-weight:850;padding:2px 0 12px">OPEN POSITIONS · GATE.IO FUTURES TESTNET — Click to minimize / maximize</summary>
<p class="light">Separate Testnet account readback · Not production · No automatic order submission enabled</p>
<div class="panel-heading"><span id="gateTestnetPositionStatus" class="light">Checking Testnet positions…</span><button class="btn gloss-action" id="gateTestnetPositionRefresh" type="button">↻ Refresh Testnet</button></div>
<div class="tablewrap"><table style="width:100%;min-width:700px"><thead><tr><th>CONTRACT</th><th>SIDE</th><th>SIZE (CONTRACTS)</th><th>ENTRY</th><th>MARK</th><th>UNREALIZED PNL</th><th>LEVERAGE</th></tr></thead><tbody id="gateTestnetPositionRows"><tr><td colspan="7">Loading…</td></tr></tbody></table></div></details>
<details id="testnetOrdersPanel" class="panel" style="width:100%;margin:14px 0;max-width:none">
<summary style="cursor:pointer;font-size:16px;font-weight:850">OPEN ORDERS · GATE.IO FUTURES TESTNET — Click to expand / minimize</summary>
<p class="light">Actual unfilled Testnet exchange orders only · No simulated signals</p>
<div class="panel-heading"><span class="light" id="testnetOrdersStatus">Not checked</span><button class="btn gloss-action" id="testnetOrdersRefresh" type="button">↻ Refresh Testnet Orders</button></div>
<div class="tablewrap"><table style="width:100%;min-width:600px"><thead><tr><th>CONTRACT</th><th>ORDER ID</th><th>SIZE</th><th>PRICE</th><th>REMAINING</th><th>TIME IN FORCE</th><th>STATUS</th></tr></thead><tbody id="testnetOrdersRows"><tr><td colspan="7">Expand to load</td></tr></tbody></table></div></details>
<details id="incomingLuxPanel" class="panel" open style="width:100%;margin:14px 0;max-width:none">
<summary style="cursor:pointer;font-size:16px;font-weight:850;padding:2px 0 12px">INCOMING LUXALGO ORDERS · WEBHOOK SIGNALS <span class="light" style="font-size:12px">— Click to minimize / maximize</span></summary>
<p class="light">Fresh LuxAlgo signals only, after your saved dashboard reset. LIVE requires an open Gate.io Testnet position with reverified exchange-side TP/SL; CLOSED requires a verified closing journal. Unexecuted or uncertain calls are never labeled LIVE. Real-money trading OFF.</p>
<div class="panel-heading"><span id="incomingLuxInfo" class="light" role="status">Loading fresh signals from zero…</span><div style="display:flex;gap:8px;flex-wrap:wrap"><button class="btn" id="incomingLuxClear" type="button" title="Clear displayed signals, not exchange orders or safety logs">Clear Visible History</button><button class="btn gloss-action" id="incomingLuxRefresh" type="button">↻ Refresh</button></div></div>
<div class="market-tabs" id="incomingLuxFilters" aria-label="Filter incoming LuxAlgo signals by verified status"><button class="btn selected" type="button" data-lux-filter="ALL">ALL</button><button class="btn" type="button" data-lux-filter="LIVE">● LIVE</button><button class="btn" type="button" data-lux-filter="CLOSED">✓ CLOSED</button><button class="btn" type="button" data-lux-filter="WAITING FOR EXECUTION">◷ WAITING</button><button class="btn" type="button" data-lux-filter="NOT EXECUTED">NOT EXECUTED</button><button class="btn" type="button" data-lux-filter="UNSUPPORTED ON TESTNET">UNSUPPORTED MARKET</button><button class="btn" type="button" data-lux-filter="VERIFY EXCHANGE">VERIFY EXCHANGE</button></div>
<div class="tablewrap incoming-lux-scroll" id="incomingLuxScroll" tabindex="0" role="region" aria-label="Incoming LuxAlgo signals, latest 25, scroll vertically after row 10"><table style="min-width:1000px;width:100%"><thead><tr><th>S/N</th><th>RECEIVED (IST)</th><th>COIN</th><th>SIGNAL</th><th>TF</th><th>ORDER TYPE</th><th>ENTRY</th><th>STOP LOSS</th><th>TAKE PROFIT</th><th>CURRENT STATUS · CLICK TO REFRESH</th><th>DETAIL</th></tr></thead><tbody id="incomingLuxRows"><tr><td colspan="11">Loading…</td></tr></tbody></table></div>
</details><div class="market-grid">
<section class="panel market-panel"><div class="panel-heading"><h2>GATE.IO FUTURES RADAR <span class="gloss-gold">● MARKET LIVE</span></h2><button class="btn gloss-action" id="radarRefresh" type="button">↻ Refresh</button></div>
<p class="light">All available Gate.io USDT perpetual tickers · 24h rankings · public market data.</p>
<div class="market-tabs" id="radarTabs"><button class="btn selected" data-radar="gainers">TOP GAINERS</button><button class="btn" data-radar="losers">TOP LOSERS</button><button class="btn" data-radar="volume">TOP VOLUME</button><button class="btn" data-radar="all">ALL PAIRS</button></div>
<div class="market-filters"><input id="radarSearch" placeholder="Search pair (e.g. SOL)" maxlength="30"><select id="radarLimit"><option value="24">Top 24</option><option value="50">Top 50</option><option value="all">Show all</option></select></div>
<p class="light" id="radarStatus">Loading Gate.io perpetual market data…</p>
<div class="tablewrap market-scroll"><table><thead><tr><th>PAIR · CHART ↗</th><th>PRICE</th><th>24H %</th><th>24H VOL (USDT)</th></tr></thead><tbody id="radarBody"><tr><td colspan="4">Loading…</td></tr></tbody></table></div>
</section>
<section class="panel market-panel"><div class="panel-heading"><h2>AI FUTURES OPPORTUNITY SCANNER <span class="gloss-cyan">● TECHNICAL</span></h2><button class="btn gloss-action" id="scannerRefresh" type="button">↻ Scan</button></div>
<p class="light">Rule-based opportunity watchlist · 36 highest-volume Gate.io USDT perpetual pairs; completed candles; not predictive AI.</p>
<div class="market-tabs" id="scannerTabs"><button class="btn selected" data-scan="bullish">BULLISH BREAKOUT</button><button class="btn" data-scan="mixed">EARLY / MIXED</button><button class="btn" data-scan="bearish">BEARISH BREAKDOWN</button><button class="btn" data-scan="all">ALL RESULTS</button></div>
<div class="market-filters"><select id="scannerTf"><option value="15m">15m</option><option value="5m">5m</option></select><input id="scannerSearch" placeholder="Search pair" maxlength="30"></div>
<p class="light" id="scannerStatus">Loading scanner…</p>
<div class="tablewrap market-scroll"><table><thead><tr><th>PAIR · CHART ↗</th><th>BIAS</th><th>SCORE</th><th>RSI</th><th>VOL RATIO</th></tr></thead><tbody id="scannerBody"><tr><td colspan="5">Scanning…</td></tr></tbody></table></div>
<p class="light">Technical labels reflect EMA trend, RSI, recent price-range break and candle volume. They are not LuxAlgo orders.</p>
</section></div>
<section class="panel advanced-panel" id="advancedScannerPanel"><div class="panel-heading"><h2>LUXALGO PRO · ADVANCED FUTURES OPPORTUNITY SCANNER <span class="gloss-cyan">● MARKET INTELLIGENCE</span></h2><button class="btn gloss-action" id="advancedRefresh" type="button">↻ Scan All Pairs</button></div>
<p class="light">Gate.io USDT perpetuals · 5m + 15m completed candles · volume / trend / structure screening · up to five watchlist candidates. ATR-based technical levels are estimates only; only genuine stored LuxAlgo alerts confirm SL1/TP1. NEVER PLACES ORDERS.</p>
<div class="adv-controls"><strong id="advancedScanStatus" class="adv-summary">Ready to inspect markets…</strong><span id="advancedEquityInfo" class="light">Equity risk sizing: verifying read-only account</span></div>
<div class="adv-table"><table><thead><tr><th>PAIR</th><th>DIRECTION</th><th>TIMEFRAME</th><th>LUXALGO</th><th>ENTRY REF.</th><th>SL1</th><th>TP1</th><th>R:R</th><th>0.5% RISK / SIZE</th><th>STATUS / REASON</th></tr></thead><tbody id="advancedScanBody"><tr><td colspan="10" class="light">Waiting for scan results.</td></tr></tbody></table></div>
<p class="light">Technical watchlist is NOT a LuxAlgo signal or recommendation. Technical entry/SL1/TP1 are non-executable previews. No verified LuxAlgo SL1/TP1 = NO TRADE. Capital figures are account-read-only; sizes are indicative BASE-asset quantities, not validated Gate.io contract quantities. Live execution remains BLOCKED.</p>
</section>
<div class="bottom-grid"><section class="panel performance-panel"><div class="panel-heading"><h2>PERFORMANCE ANALYTICS</h2><span class="label-muted">VERIFIED FILLS ONLY</span></div><div class="perf-grid"><div><span>Total Closed Trades</span><strong>—</strong></div><div><span>Winning Trades</span><strong>—</strong></div><div><span>Losing Trades</span><strong>—</strong></div><div><span>Win Rate</span><strong>—</strong></div><div><span>Total Realized P&amp;L</span><strong>—</strong></div><div><span>Unrealized P&amp;L</span><strong>—</strong></div></div><p class="light">Waiting for authenticated position and fill synchronization. No synthetic P&amp;L or win statistics.</p></section></div><section class="panel"><div class="panel-heading"><h2>COMPLETED TRADES · EXIT RECORD</h2><span class="label-muted">PENDING ACCOUNT HISTORY INTEGRATION</span></div><div class="tablewrap"><table><thead><tr><th>TIME (IST)</th><th>PAIR</th><th>DIRECTION</th><th>ENTRY FILL</th><th>EXIT FILL</th><th>NET REALIZED P&amp;L</th><th>RESULT</th></tr></thead><tbody><tr><td colspan="7" class="empty-state">No verified exchange fills have been synchronized. Signal receipts are not executed trades.</td></tr></tbody></table></div></section><section class="panel legacy-paper-panel"><div class="panel-heading"><h2>PAPER TRADE MONITOR <span class="tag">SIMULATION ONLY</span></h2><button class="btn gloss-action" id="paperRefresh" type="button">↻ Check Paper Trades</button></div><p class="light" id="paperStatus" role="status" aria-live="polite">Checks queued simulated orders when the dashboard is open; historical outcomes remain estimates, not fills.</p><div class="tablewrap"><table><thead><tr><th>CREATED (IST)</th><th>PAIR</th><th>SIDE</th><th>SIM ENTRY</th><th>SIM SL</th><th>SIM TP</th><th>STATE</th><th>LAST PRICE</th><th>UNREALIZED P&amp;L</th><th>EST. CLOSED P&amp;L</th></tr></thead><tbody id="paperBody"><tr><td colspan="10">No simulated positions loaded.</td></tr></tbody></table></div><p id="paperOpenPnlTotal" class="light" role="status" aria-live="polite">Open paper unrealized P&amp;L: not yet available.</p><p class="light">Simulation outcomes assume the reference entry was achievable and exclude fees, slippage and funding. If both SL and TP are touched in one candle, SL is assumed first. Historical-data gaps are not guessed. Simulated P&amp;L is never included in verified real-trade analytics.</p></section><div class="foot">LUXALGO PRO · Private terminal · Market data is public Gate.io data. Scanners are technical watchlists, not LuxAlgo entry signals. No account positions, fills, profit or loss are available until authenticated exchange sync is implemented. LIVE TRADING OFF; no orders are placed.</div></div><script>

// Read-only advanced watchlist: uses completed 5m/15m public candles and validated LuxAlgo receipt records.
(function(){
 const body=document.getElementById('advancedScanBody'),status=document.getElementById('advancedScanStatus'),risk=document.getElementById('advancedEquityInfo'),btn=document.getElementById('advancedRefresh');
 let busy=false;
 function cell(tr,val,css){const t=tr.insertCell();t.textContent=String(val??'—');if(css)t.className=css;return t;}
 async function refresh(force=false){if(busy)return;busy=true;btn.disabled=true;
 try{const url='/terminal/advanced-scanner'+(force?'?refresh=1':'');const r=await fetch(url,{cache:'no-store'});const data=await r.json();if(!r.ok)throw Error(data.error||'Scanner unavailable');
  let x=data.scan||{};status.textContent='5m '+(x['5m']?.scanned||0)+'/'+(x['5m']?.total||0)+' · 15m '+(x['15m']?.scanned||0)+'/'+(x['15m']?.total||0)+(x.running?' · scanning progressively':' · current snapshot')+' · '+(data.updated_at?new Date(data.updated_at*1000).toLocaleTimeString('en-IN',{timeZone:'Asia/Kolkata',hour12:true})+' IST':'pending');
  risk.textContent=data.equity_usdt==null?'Risk sizing unavailable (equity not verified)':'Verified equity: '+data.equity_usdt.toFixed(2)+' USDT · 0.5% preview budget '+data.risk_budget_usdt.toFixed(2)+' USDT';
  body.replaceChildren();if(!data.candidates?.length){const tr=body.insertRow();cell(tr,'NO TRADE · '+(x.running?'Scans are still running':'No verified opportunities meeting screening conditions'), 'adv-blocked').colSpan=10;}
  for(const p of (data.candidates||[])){const tr=body.insertRow();{const td=cell(tr,p.symbol+' ↗');td.className='pairlink';td.style.cursor='pointer';td.title='Open chart in TradingView (new tab)';td.addEventListener('click',()=>window.openGateTradingView(p.symbol));}cell(tr,p.side,p.side==='LONG'?'adv-signal long':'adv-signal short');cell(tr,p.timeframe);cell(tr,p.confirmation);cell(tr,p.entry==null?'—':Number(p.entry).toPrecision(7));cell(tr,p.sl1==null?'—':Number(p.sl1).toPrecision(7));cell(tr,p.tp1==null?'—':Number(p.tp1).toPrecision(7));cell(tr,p.rr);cell(tr,p.size_hint);cell(tr,p.status+' · '+(p.level_source||'UNAVAILABLE')+' · '+p.reason,'adv-reason');}
 }catch(e){status.textContent='Advanced scan unavailable: '+e.message;body.replaceChildren();const tr=body.insertRow();cell(tr,'NO TRADE · market confirmation unavailable').colSpan=10;}
 finally{busy=false;btn.disabled=false;}}
 btn.addEventListener('click',()=>refresh(true));refresh();setInterval(()=>{if(!document.hidden)refresh();},20000);
})();

// Optional editable profile image. Uploaded securely to PostgreSQL; never touches Gate.io.
(function(){
 const input=document.getElementById('profileImageInput'),display=document.getElementById('avatarDisplay');
 function showPhoto(){const im=document.createElement('img');im.src='/terminal/avatar?version='+Date.now();im.alt='SUBHASISH profile picture';im.addEventListener('error',()=>{display.replaceChildren();const t=document.createElement('span');t.textContent='SD';display.appendChild(t);});display.replaceChildren(im);}
 showPhoto();
 const changer=document.querySelector('.profile-change');
 changer.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();input.click();}});
 input.addEventListener('change',async()=>{
   const file=input.files&&input.files[0];if(!file)return;
   if(file.size>200000||!['image/jpeg','image/png','image/webp'].includes(file.type)){alert('Choose a JPEG, PNG or WebP image under 200 KB.');input.value='';return;}
   const fd=new FormData();fd.append('photo',file);fd.append('csrf_token',{{ csrf_token|tojson }});
   try{const r=await fetch('/terminal/avatar',{method:'POST',body:fd,credentials:'same-origin'});if(!r.ok)throw Error('Upload rejected');showPhoto();}
   catch(e){alert('Photo could not be saved. Use a smaller JPEG/PNG/WebP image.');}
   input.value='';
 });
})();

// Read-only live readiness, intentionally never provides an execute button.
(function(){
  const b=document.getElementById('readinessRefresh'),o=document.getElementById('readinessOutput');
  b.addEventListener('click',async()=>{
    b.disabled=true;o.textContent='Checking safety gates…';
    try {
      const r=await fetch('/terminal/live-readiness',{cache:'no-store'});
      const d=await r.json();if(!r.ok)throw Error(d.error||'Readiness unavailable');
      o.replaceChildren();
      const title=document.createElement('p');title.textContent='LIVE TRADING '+d.status+' · NO ORDERS ENABLED';title.style.color='#ffa6a6';title.style.fontWeight='bold';o.appendChild(title);
      for(const c of d.checks){const line=document.createElement('div');line.style.margin='6px 0';line.textContent=(c.ready?'✓ ':'✕ ')+c.name+' — '+c.detail;o.appendChild(line);}
    } catch(e){o.textContent='Readiness unavailable; live trading remains BLOCKED.';}finally{b.disabled=false;}
  });
})();


// Stage 7: inspect safety prerequisites and durable future-execution halt; no order route.
(function(){
  const b=document.getElementById('stage7AuditButton');
  const o=document.getElementById('stage7AuditResult');
  if(!b||!o)return;
  b.addEventListener('click',async()=>{
    b.disabled=true;o.textContent='Reading live-execution blockers and PostgreSQL halt…';
    try{
      const r=await fetch('/terminal/stage7-readiness-audit',{cache:'no-store',credentials:'same-origin'});
      const d=await r.json();
      o.replaceChildren();
      const heading=document.createElement('strong');
      heading.textContent='Stage 7 '+String(d.status||'BLOCKED')+' · '+String(d.summary||'Readiness unavailable')+' · Exchange orders: 0';
      o.appendChild(heading);
      for(const check of (Array.isArray(d.checks)?d.checks:[])){
        const line=document.createElement('div');line.style.margin='5px 0';
        line.textContent=(check.passed?'✓ ':'✕ ')+String(check.name)+' — '+String(check.detail);
        o.appendChild(line);
      }
      if(!r.ok)o.style.color='#ffb6b6';
    }catch(e){o.textContent='Stage 7 BLOCKED · Could not confirm readiness. Exchange orders: 0';}
    finally{b.disabled=false;}
  });
})();

// Stage 8: durable client-ID / ambiguous acknowledgment simulation only.
(function(){
  const b=document.getElementById('stage8AckTestButton');
  const out=document.getElementById('stage8AckTestResult');
  if(!b||!out)return;
  b.addEventListener('click',async()=>{
    b.disabled=true;out.textContent='Testing Stage 8 PostgreSQL simulated acknowledgment recovery…';
    try{
      const r=await fetch('/terminal/stage8-ack-recovery-test',{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},body:'{}'});
      const d=await r.json();
      out.textContent='Stage 8 '+String(d.status||'BLOCKED')+' · '+String(d.detail||d.error||'Unavailable')+' · Exchange orders: 0';
      out.style.color=r.ok&&d.status==='PASS'?'#63f0c3':(r.ok&&d.status==='PASS_WITH_GAPS'?'#ffcf89':'#ffb6b6');
    }catch(e){out.textContent='Stage 8 BLOCKED · Diagnostic unavailable; no exchange orders.';out.style.color='#ffb6b6';}
    finally{b.disabled=false;}
  });
})();




// Stage 15 detailed record inspection: read-only. No database writes or trade actions.
(function(){
 const b=document.getElementById('stage15InspectButton'),out=document.getElementById('stage15InspectResult');
 if(!b||!out)return;
 b.addEventListener('click',async()=>{
  b.disabled=true;out.style.display='block';out.textContent='Inspecting stored paper record relationships (read-only)…';
  try{
   const r=await fetch('/terminal/stage15-record-issues',{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json','X-Terminal-CSRF':{{ csrf_token|tojson }}},body:'{}'});
   const d=await r.json();
   if(!r.ok){out.textContent='Stage 15 inspection BLOCKED · '+String(d.detail||d.error||'Unavailable');return;}
   const x=['STAGE 15 · READ-ONLY RECORD BREAKDOWN','Intents inspected: '+d.intents_inspected,'Flagged by original Stage 15 rules: '+d.flagged_by_original_rules,'Records with core lifecycle conflicts: '+d.core_lifecycle_conflicts,'Records with missing levels only: '+d.missing_levels_only,'', 'REASON COUNTS'];
   for(const [k,v] of Object.entries(d.reason_counts||{}))x.push('  '+k+': '+v);
   x.push('','FLAGGED RECORDS (recent first)');
   for(const z of d.records||[])x.push('• '+z.event_id+' | '+z.symbol+' '+z.side+' '+z.timeframe+' | levels='+z.levels_status+' queue='+z.queue_state+' order='+z.simulated_status+' tracking='+z.tracking_state+' | '+z.reasons.join('; '));
   if(!(d.records||[]).length)x.push('None flagged.');
   x.push('','READ ONLY · No historical records changed · No exchange orders placed.');
   out.textContent=x.join('\\n');
  }catch(e){out.textContent='Stage 15 inspection unavailable; no records modified.';}
  finally{b.disabled=false;}
 });
})();

// Stage 15: inspect actual stored webhook-to-paper records, read-only.
(function(){
 const b=document.getElementById('stage15IntegrationTestButton'),out=document.getElementById('stage15IntegrationTestResult');
 if(!b||!out)return;
 b.addEventListener('click',async()=>{
  b.disabled=true;out.textContent='Reading real signal receipts, levels, paper queue and database HALT…';
  try{
   const r=await fetch('/terminal/stage15-real-paper-audit', {method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json','X-Terminal-CSRF':{{ csrf_token|tojson }}},body:'{}'});
   const d=await r.json();
   out.textContent='Stage 15 '+String(d.status||'BLOCKED')+' · '+String(d.detail||d.error||'Unavailable')+' · Exchange orders: 0';
   out.style.color=r.ok&&d.status==='PASS'?'#63f0c3':'#ffb6b6';
  }catch(e){out.textContent='Stage 15 BLOCKED · Read-only audit unavailable; exchange orders: 0';out.style.color='#ffb6b6';}
  finally{b.disabled=false;}
 });
})();

// Stage 14: FINAL isolated paper-workflow audit; does not submit a trade.
(function(){
 const b=document.getElementById('stage14FinalTestButton'),out=document.getElementById('stage14FinalTestResult');
 if(!b||!out)return;
 b.addEventListener('click',async()=>{
  b.disabled=true;out.textContent='Running isolated PostgreSQL paper workflow and safety audit…';
  try{
   const r=await fetch('/terminal/stage14-final-paper-test',{
    method:'POST',credentials:'same-origin',cache:'no-store',
    headers:{'Content-Type':'application/json','X-Terminal-CSRF':{{ csrf_token|tojson }}},body:'{}'});
   const d=await r.json();
   out.textContent='Stage 14 '+String(d.status||'BLOCKED')+' · '+String(d.detail||d.error||'Unavailable')+' · Exchange orders: 0';
   out.style.color=r.ok&&d.status==='PASS'?'#63f0c3':'#ffb6b6';
  }catch(e){out.textContent='Stage 14 BLOCKED · Diagnostic unavailable. Exchange orders: 0';out.style.color='#ffb6b6';}
  finally{b.disabled=false;}
 });
})();

// Stage 13: isolated operational failure fixtures; no live exchange submission.
(function(){
 const b=document.getElementById('stage13RecoveryTestButton'),out=document.getElementById('stage13RecoveryTestResult');
 if(!b||!out)return;
 b.addEventListener('click',async()=>{
  b.disabled=true;out.textContent='Checking persistent HALT, recovery and API/stale-data failure fixtures…';
  try{
   const r=await fetch('/terminal/stage13-recovery-test',{
    method:'POST',credentials:'same-origin',cache:'no-store',
    headers:{'Content-Type':'application/json','X-Terminal-CSRF':{{ csrf_token|tojson }}},body:'{}'});
   const d=await r.json();
   out.textContent='Stage 13 '+String(d.status||'BLOCKED')+' · '+String(d.detail||d.error||'Unavailable')+' · Exchange orders: 0';
   out.style.color=r.ok&&d.status==='PASS'?'#63f0c3':'#ffb6b6';
  }catch(e){out.textContent='Stage 13 BLOCKED · Database/recovery audit unavailable; no exchange orders.';out.style.color='#ffb6b6';}
  finally{b.disabled=false;}
 });
})();

// Stage 12: explicitly user-triggered authenticated Gate.io GET-only snapshot.
(function(){
 const b=document.getElementById('stage12ReadOnlyTestButton'),out=document.getElementById('stage12ReadOnlyTestResult');
 if(!b||!out)return;
 b.addEventListener('click',async()=>{
  b.disabled=true;out.textContent='Reading Gate.io USDT futures positions and open orders (GET only)…';
  try{
   const r=await fetch('/terminal/stage12-gate-readonly-test',{
    method:'POST',credentials:'same-origin',cache:'no-store',
    headers:{'Content-Type':'application/json','X-Terminal-CSRF':{{ csrf_token|tojson }}},body:'{}'});
   const d=await r.json();
   out.textContent='Stage 12 '+String(d.status||'BLOCKED')+' · '+String(d.detail||d.error||'Unavailable')+' · Exchange orders placed: 0';
   out.style.color=r.ok&&d.status==='PASS'?'#63f0c3':'#ffb6b6';
  }catch(e){out.textContent='Stage 12 BLOCKED · Gate.io/DB snapshot unavailable; no exchange writes.';out.style.color='#ffb6b6';}
  finally{b.disabled=false;}
 });
})();

// Stage 11 diagnostic: six simultaneous simulated reservations under a DB-wide run lock.
(function(){
 const b=document.getElementById('stage11RiskTestButton'), out=document.getElementById('stage11RiskTestResult');
 if(!b||!out)return;
 b.addEventListener('click',async()=>{
  b.disabled=true;out.textContent='Testing concurrent simulated risk reservations in PostgreSQL…';
  try{
   const r=await fetch('/terminal/stage11-atomic-risk-test',{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json','X-Terminal-CSRF':{{ csrf_token|tojson }}},body:'{}'});
   const d=await r.json();out.textContent='Stage 11 '+String(d.status||'BLOCKED')+' · '+String(d.detail||d.error||'Unavailable')+' · Exchange orders: 0';
   out.style.color=r.ok&&d.status==='PASS'?'#63f0c3':'#ffb6b6';
  }catch(e){out.textContent='Stage 11 BLOCKED · Diagnostic unavailable; no exchange orders.';out.style.color='#ffb6b6';}
  finally{b.disabled=false;}
 });
})();

// Stage 10: isolated protective-coverage fixtures, never exchange order placement.
(function(){
  const b=document.getElementById('stage10ProtectionTestButton');
  const out=document.getElementById('stage10ProtectionTestResult');
  if(!b||!out)return;
  b.addEventListener('click',async()=>{
    b.disabled=true;out.textContent='Testing Stage 10 protective coverage fixtures and persistent HALT…';
    try{
      const r=await fetch('/terminal/stage10-protection-test',{
        method:'POST',credentials:'same-origin',cache:'no-store',
        headers:{'Content-Type':'application/json','X-Terminal-CSRF':{{ csrf_token|tojson }}},body:'{}'
      });
      const d=await r.json();
      out.textContent='Stage 10 '+String(d.status||'BLOCKED')+' · '+String(d.detail||d.error||'Unavailable')+' · Exchange orders: 0';
      out.style.color=r.ok&&d.status==='PASS'?'#63f0c3':'#ffb6b6';
    }catch(e){out.textContent='Stage 10 BLOCKED · Protective coverage diagnostic unavailable; no exchange writes.';out.style.color='#ffb6b6';}
    finally{b.disabled=false;}
  });
})();

// Stage 9: locally simulated read-only reconciliation; NO Gate.io order writes.
(function(){
  const b=document.getElementById('stage9ReconcileTestButton');
  const out=document.getElementById('stage9ReconcileTestResult');
  if(!b||!out)return;
  b.addEventListener('click',async()=>{
    b.disabled=true;out.textContent='Checking Stage 9 isolated reconciliation cases and durable recovery…';
    try{
      const r=await fetch('/terminal/stage9-reconciliation-test',{
        method:'POST',credentials:'same-origin',cache:'no-store',
        headers:{'Content-Type':'application/json','X-Terminal-CSRF':{{ csrf_token|tojson }}},body:'{}'
      });
      const d=await r.json();
      out.textContent='Stage 9 '+String(d.status||'BLOCKED')+' · '+String(d.detail||d.error||'Unavailable')+' · Exchange orders: 0';
      out.style.color=r.ok&&d.status==='PASS'?'#63f0c3':'#ffb6b6';
    }catch(e){out.textContent='Stage 9 BLOCKED · Reconciliation diagnostic unavailable; no exchange writes.';out.style.color='#ffb6b6';}
    finally{b.disabled=false;}
  });
})();

 // Live-order guard test: audit first stored signal, never execute an order.
 (function(){
 const b=document.getElementById('guardTestButton'),out=document.getElementById('guardTestResult');
 const eventId={{ (records[0].event_id if records else '')|tojson }};
 b.disabled=!eventId;
 if(!eventId){out.textContent='No received signals available for a guard test.';return;}
 b.addEventListener('click',async()=>{
  b.disabled=true;out.textContent='Checking PostgreSQL duplicate guard (no exchange writes)…';
  try{
   const r=await fetch('/terminal/live-order-guard-test',{method:'POST',headers:{'Content-Type':'application/json','X-Terminal-CSRF':{{ csrf_token|tojson }}},body:JSON.stringify({event_id:eventId}),cache:'no-store'});
   const d=await r.json();if(!r.ok)throw Error(d.error||'Guard unavailable');
   out.textContent='BLOCKED · '+(d.duplicate?'Duplicate event recognized. ':'Guard record saved. ')+'Exchange orders: 0. '+d.reason;
  }catch(e){out.textContent='Guard test failed CLOSED: '+e.message+' · Orders: 0.';}
  finally{b.disabled=false;}
 });
 })();

// Stage 2 independently verifies simulation-only PostgreSQL locking.
(function(){
 const b=document.getElementById('stage2GateTestButton'),out=document.getElementById('stage2GateTestResult');
 if(!b||!out)return;
 b.addEventListener('click',async()=>{
  b.disabled=true;out.textContent='Testing simulated duplicate and concurrent reservations (no exchange writes)…';
  try{
   const r=await fetch('/terminal/risk-gate-self-test',{method:'POST',headers:{'Content-Type':'application/json','X-Terminal-CSRF':{{ csrf_token|tojson }}},body:'{}',cache:'no-store'});
   const d=await r.json();
   out.textContent='Stage 2 '+(d.status||'UNAVAILABLE')+' · '+(d.detail||d.error||'No details')+' · Exchange orders: 0';
  }catch(e){out.textContent='Stage 2 test unavailable (fail closed): '+e.message+' · Orders: 0';}
  finally{b.disabled=false;}
 });
})();

// Stage 3 read-only public contract metadata + deterministic sizing diagnostic.
(function(){
 const b=document.getElementById('stage3ContractTestButton'),out=document.getElementById('stage3ContractTestResult');
 if(!b||!out)return;
 b.addEventListener('click',async()=>{
  b.disabled=true;out.textContent='Validating Gate.io public BTC contract metadata and illustrative 5x risk sizing…';
  try{
   const r=await fetch('/terminal/stage3-contract-self-test',{method:'POST',headers:{'Content-Type':'application/json','X-Terminal-CSRF':{{ csrf_token|tojson }}},body:'{}',cache:'no-store'});
   const d=await r.json();
   out.textContent='Stage 3 '+(d.status||'UNAVAILABLE')+' · '+(d.detail||d.error||'No details')+' · Exchange orders: 0';
  }catch(e){out.textContent='Stage 3 test unavailable (fail closed): '+e.message+' · Orders: 0';}
  finally{b.disabled=false;}
 });
})();

// Stage 4: fail-closed price and side verification; no order submissions.
(function(){
 const b=document.getElementById('stage4ProtectiveTestButton'),out=document.getElementById('stage4ProtectiveTestResult');
 if(!b||!out)return;
 b.addEventListener('click',async()=>{
  b.disabled=true;out.textContent='Checking read-only SL/TP prices and LONG/SHORT closing sides…';
  try{
   const r=await fetch('/terminal/stage4-protection-self-test',{method:'POST',headers:{'Content-Type':'application/json','X-Terminal-CSRF':{{ csrf_token|tojson }}},body:'{}',cache:'no-store'});
   const d=await r.json();
   out.textContent='Stage 4 '+(d.status||'UNAVAILABLE')+' · '+(d.detail||d.error||'No details')+' · Exchange orders: 0';
  }catch(e){out.textContent='Stage 4 unavailable (fail closed): '+e.message+' · Orders: 0';}
  finally{b.disabled=false;}
 });
})();

// Stage 5: simulated-only database order-lifecycle and recovery diagnostic.
(function(){
 const b=document.getElementById('stage5LedgerTestButton'),out=document.getElementById('stage5LedgerTestResult');
 if(!b||!out)return;
 b.addEventListener('click',async()=>{
  b.disabled=true;out.textContent='Testing PostgreSQL lifecycle, replay rejection, and simulated recovery…';
  try{
   const r=await fetch('/terminal/stage5-ledger-self-test',{method:'POST',headers:{'Content-Type':'application/json','X-Terminal-CSRF':{{ csrf_token|tojson }}},body:'{}',cache:'no-store'});
   const d=await r.json();
   out.textContent='Stage 5 '+(d.status||'UNAVAILABLE')+' · '+(d.detail||d.error||'No details')+' · Exchange orders: 0';
  }catch(e){out.textContent='Stage 5 unavailable (fail closed): '+e.message+' · Orders: 0';}
  finally{b.disabled=false;}
 });
})();


// Stage 6: persistent emergency HALT and test. This NEVER closes exchange positions.
(function(){
 const test=document.getElementById('stage6HaltTestButton'),stop=document.getElementById('stage6EmergencyHalt'),out=document.getElementById('stage6HaltTestResult'),state=document.getElementById('stage6HaltState');
 if(!test||!stop||!out||!state)return;
 const headers={'Content-Type':'application/json','X-Terminal-CSRF':{{ csrf_token|tojson }}};
 async function getState(){try{const r=await fetch('/terminal/stage6-halt-status',{cache:'no-store'});const d=await r.json();state.textContent=d.halted===true?'● Future execution HALTED · '+(d.reason||''):'⚠ Halt status unavailable — treat as HALTED';}catch(e){state.textContent='● Halt status unavailable — treat as HALTED';}}
 test.addEventListener('click',async()=>{test.disabled=true;out.textContent='Checking durable simulated halt and restart recovery…';try{const r=await fetch('/terminal/stage6-halt-self-test',{method:'POST',headers,body:'{}',cache:'no-store'});const d=await r.json();out.textContent='Stage 6 '+(d.status||'INCONCLUSIVE')+' · '+(d.detail||d.error||'No details')+' · Exchange orders: 0';}catch(e){out.textContent='Stage 6 INCONCLUSIVE · Database unavailable · Orders: 0';}finally{test.disabled=false;getState();}});
 stop.addEventListener('click',async()=>{if(!confirm('Activate permanent future-execution HALT and switch PAPER bot OFF? This cannot close Gate.io positions.'))return;stop.disabled=true;try{const r=await fetch('/terminal/stage6-emergency-halt',{method:'POST',headers,body:'{}',cache:'no-store'});const d=await r.json();out.textContent=d.halted===true&&r.ok?'Emergency HALT saved; PAPER bot OFF. Existing Gate.io positions are NOT closed.':'Emergency halt could not be verified; do not trade. '+(d.error||'');}catch(e){out.textContent='Emergency halt status UNKNOWN. Do not trade.';}finally{stop.disabled=false;getState();}});
 getState();
})();

// Step 415: observe account risk conditions, but never authorize any exchange order.
(function(){
 const b=document.getElementById('riskCheckButton'),out=document.getElementById('riskCheckResult');
 if(!b||!out)return;
 b.addEventListener('click',async()=>{
  b.disabled=true;out.textContent='Reading Gate.io account and position exposure…';
  try{
   const r=await fetch('/terminal/live-risk-check',{cache:'no-store'});
   const d=await r.json();if(!r.ok)throw Error(d.error||'Risk inspection unavailable');
   const snap=d.snapshot||{},fmt=x=>x===null||x===undefined?'UNAVAILABLE':String(x);
   out.replaceChildren();
   const intro=document.createElement('p');intro.textContent='LIVE ENTRY BLOCKED · 0 orders · 1 position maximum · 3% intended risk · 5× ceiling';intro.style.color='#ffadad';out.appendChild(intro);
   const amounts=document.createElement('p');amounts.textContent='Verified equity: '+fmt(snap.verified_equity_usdt)+' USDT | Available: '+fmt(snap.verified_available_usdt)+' USDT | Intended risk budget: '+fmt(snap.risk_budget_usdt)+' USDT | Open positions: '+fmt(snap.open_positions);out.appendChild(amounts);
   for(const c of d.checks||[]){const line=document.createElement('div');line.textContent=(c.passed?'✓ ':'✕ ')+c.name+' — '+c.detail;line.style.margin='5px 0';out.appendChild(line);}
  }catch(e){out.textContent='Risk inspection unavailable: BLOCKED. No orders possible.';}
  finally{b.disabled=false;}
 });
})();

// Remote bot control: only new Gate Testnet signals, never real-money execution.


(function(){
 const label=document.getElementById('botControlLabel'),detail=document.getElementById('botControlDetail');
 const on=document.getElementById('botTurnOn'),off=document.getElementById('botTurnOff'),indicator=document.getElementById('botStateIndicator');
  // Production removes Testnet buttons. Do not halt market-data UI startup.
  if(!on||!off||!label||!detail)return;
 function render(enabled,armed){const active=enabled&&armed;if(indicator){indicator.className='bot-state-indicator '+(active?'on':'off');indicator.textContent=active?'● TESTNET ON':'● TESTNET PAUSED';}on.classList.toggle('next-action',!enabled);off.classList.toggle('next-action',enabled);label.textContent=active?'BOT ON · TESTNET EXECUTION ACTIVE':(enabled?'BOT ON · TESTNET SAFETY PAUSE':'BOT OFF · TESTNET SIGNALS PAUSED');label.style.color=active?'#6cf3ba':'#ffc47c';detail.textContent=active?'Only eligible fresh LuxAlgo signals can submit Gate.io Testnet protected orders. Real-money OFF.':(enabled?'Testnet entry permissions are disabled for safety. No new exchange orders; recovery monitoring remains independent.':'New signals are logged; new Testnet entries paused. Existing positions remain monitored.');on.disabled=enabled;off.disabled=!enabled;}
 async function refresh(){try{const r=await fetch('/terminal/bot-control',{cache:'no-store'});const d=await r.json();if(!r.ok)throw Error();render(d.bot_enabled,d.testnet_automation_armed===true);}catch(e){render(false,false);if(indicator){indicator.className='bot-state-indicator unknown';indicator.textContent='● STATUS UNKNOWN · SAFE OFF';}detail.textContent='Cannot verify saved bot state: treat as OFF.';on.disabled=true;}}
 async function change(enabled){on.disabled=true;off.disabled=true;try{const r=await fetch('/terminal/bot-control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled}),cache:'no-store'});const d=await r.json();if(!r.ok)throw Error(d.error||'Update rejected');render(d.bot_enabled,d.testnet_automation_armed===true);}catch(e){detail.textContent='Cannot change bot state: '+e.message;await refresh();}}
 on.addEventListener('click',()=>change(true));off.addEventListener('click',()=>change(false));refresh();setInterval(()=>{if(!document.hidden)refresh();},30000);
})();

const entry=document.getElementById('entry'), sl=document.getElementById('sl'), tp1=document.getElementById('tp1');
// Removed on-demand signal panel controls and manual dry-run preview UI.
// Webhook receipts, paper simulation, and backend safety checks remain intact.

function openGateTradingView(symbol){
 const pair=String(symbol||'').toUpperCase().replace('_','').trim();
 if(!/^[A-Z0-9]{2,25}USDT$/.test(pair))return;
 window.open('https://www.tradingview.com/chart/?symbol='+encodeURIComponent('GATEIO:'+pair+'.P'),'_blank','noopener,noreferrer');
}
window.openGateTradingView=openGateTradingView;;



// Private read-only Gate.io position cards. NO account-mutating actions.
(function(){
'use strict';
const body=document.getElementById('gatePositionsBody'),updated=document.getElementById('gatePositionsUpdated');
const count=document.getElementById('openPositionCount'),pnl=document.getElementById('unrealizedPositionPnl');
const connection=document.getElementById('gatePositionStatus'),button=document.getElementById('positionRefresh');
let busy=false,equityBusy=false;
const expanded=new Set();
function money(value){if(value===null||value===undefined)return '—';const v=Number(value);return Number.isFinite(v)?v.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:4}):'—';}
function price(value){if(value===null||value===undefined)return '—';const v=Number(value);if(!Number.isFinite(v)||v<=0)return '—';return v.toLocaleString(undefined,{maximumSignificantDigits:10});}
function element(tag,cl,text){const n=document.createElement(tag);if(cl)n.className=cl;if(text!==undefined)n.textContent=text;return n;}
function clearEquity(message){
 document.getElementById('futuresEquity').textContent='—';
 document.getElementById('futuresAvailable').textContent='—';
 document.getElementById('futuresBalance').textContent='—';
 document.getElementById('futuresEquityUpdated').textContent=message||'Account figures unavailable';
}
async function loadFuturesEquity(){
 if(equityBusy)return;equityBusy=true;
 try{
  const r=await fetch('/terminal/futures-equity',{cache:'no-store'}),d=await r.json();
  if(!r.ok)throw Error(d.error||'Futures balance not available');
  document.getElementById('futuresEquity').textContent=money(d.equity_usdt);
  document.getElementById('futuresAvailable').textContent=money(d.available_usdt);
  document.getElementById('futuresBalance').textContent=money(d.balance_usdt);
  document.getElementById('futuresEquityUpdated').textContent='Gate.io verified · '+new Date(d.fetched_at*1000).toLocaleTimeString('en-IN',{timeZone:'Asia/Kolkata',hour12:true})+' IST';
 }catch(e){clearEquity('Cannot verify: '+e.message);}finally{equityBusy=false;}
}
function field(grid,label,value,colorClass){const x=element('div','position-fact');x.append(element('small','',label),element('strong',colorClass||'',value));grid.append(x);}
function renderPositions(positions){
 body.replaceChildren();
 if(!positions.length){body.append(element('p','light','Gate.io reports no open USDT perpetual positions. This terminal remains READ ONLY.'));return;}
 const seen=new Set();
 positions.forEach((p,i)=>{
  const id=p.contract+'|'+p.position_mode;seen.add(id);
  const card=element('details','position-card');card.open=expanded.has(id)||!window.__positionCardsInitialized;
  card.addEventListener('toggle',()=>{if(card.open)expanded.add(id);else expanded.delete(id);});
  const heading=element('summary');const left=element('div','position-head');
  left.append(element('span','position-side '+(p.side==='LONG'?'long':'short'),p.side==='LONG'?'L':'S'),
              element('span','position-symbol',p.contract.replace('_','/')),
              element('span','position-leverage',p.leverage_label));
  const right=element('span','position-summary-pnl '+(p.unrealized_pnl>=0?'positive':'negative'),(p.unrealized_pnl>=0?'+':'')+money(p.unrealized_pnl)+' USDT ⌄');
  heading.append(left,right);card.append(heading);
  const inner=element('div','position-card-inner'),grid=element('div','position-facts');
  field(grid,'Unrealized P&L (USDT)',(p.unrealized_pnl>=0?'+':'')+money(p.unrealized_pnl),p.unrealized_pnl>=0?'positive':'negative');
  field(grid,'ROI (based on reported margin)',p.roi_percent===null?'—':(p.roi_percent>=0?'+':'')+p.roi_percent.toFixed(2)+'%',p.roi_percent===null?'':p.roi_percent>=0?'positive':'negative');
  field(grid,'Position size (USDT)',p.position_value_usdt===null?'—':money(p.position_value_usdt));
  field(grid,'Margin (USDT)',p.margin_usdt===null?'—':money(p.margin_usdt));
  field(grid,'Contracts',String(p.size_contracts));
  field(grid,'Entry price',price(p.entry_price));
  field(grid,'Mark price',price(p.mark_price));
  field(grid,'Liquidation price',price(p.liquidation_price));
  grid && inner.append(grid);
  const actions=element('div','position-card-actions');
  const chart=element('button','btn position-chart-action','View Chart');chart.type='button';
  chart.addEventListener('click',()=>{
   window.openGateTradingView(p.contract);
  });actions.append(chart);
  for(const name of ['Set TP/SL','Reverse','Market Close','Close Position']){
   const action=element('button','btn btn-danger',name+' · LOCKED');action.type='button';action.disabled=true;
   action.title='Disabled: live order execution and exchange-side risk protection have not been implemented';actions.append(action);
  }
  inner.append(actions,element('p','position-readonly-hint','Read-only Gate.io account snapshot · Actions remain locked until secure order management is implemented. ROI excludes fees and funding. SL/TP status not verified.'));
  card.append(inner);body.append(card);
 });
 for(const key of expanded){if(!seen.has(key))expanded.delete(key);}
 window.__positionCardsInitialized=true;
}
async function loadPositions(){
 if(busy)return;busy=true;button.disabled=true;
 try{
  const r=await fetch('/terminal/positions',{cache:'no-store'}),d=await r.json();
  if(!r.ok)throw Error(d.error||'Gate.io account temporarily unavailable');
  count.textContent=String(d.count);pnl.textContent=money(d.unrealized_pnl_usdt);
  pnl.classList.toggle('positive',d.unrealized_pnl_usdt>0);pnl.classList.toggle('negative',d.unrealized_pnl_usdt<0);
  connection.textContent='Position feed connected · READ ONLY';renderPositions(d.positions);
  updated.textContent='Gate.io snapshot · '+new Date(d.fetched_at*1000).toLocaleTimeString('en-IN',{timeZone:'Asia/Kolkata',hour12:true})+' IST'+' · refresh every 30s · no trading orders enabled.';
 }catch(e){count.textContent='—';pnl.textContent='—';pnl.classList.remove('positive','negative');connection.textContent='Feed unavailable: '+String(e.message).slice(0,110);body.replaceChildren(element('p','light','Unable to verify positions: '+e.message));updated.textContent='Position data unavailable; no empty positions assumed.';}
 finally{busy=false;button.disabled=false;}
}
button.addEventListener('click',()=>{loadPositions();loadFuturesEquity();});
loadPositions();loadFuturesEquity();setInterval(()=>{if(!document.hidden){loadPositions();loadFuturesEquity();}},30000);
})();



(function(){
'use strict';
const radarBody=document.getElementById('radarBody'), radarStatus=document.getElementById('radarStatus');
const scanBody=document.getElementById('scannerBody'), scanStatus=document.getElementById('scannerStatus');
let tickerData=[],radarMode='gainers',scanMode='bullish',scanData=[],scanTf='15m';
const fmtPrice=n=>Number(n).toLocaleString(undefined,{maximumSignificantDigits:7});
const fmtVol=n=>n>=1e9?(n/1e9).toFixed(2)+'B':n>=1e6?(n/1e6).toFixed(2)+'M':n>=1e3?(n/1e3).toFixed(1)+'K':n.toFixed(0);
function addCell(tr,value,className){const td=document.createElement('td');td.textContent=value;if(className)td.className=className;tr.appendChild(td);return td;}
function addPair(tr,sym,tf){const td=tr.insertCell(),link=document.createElement('a');link.className='pairlink';link.textContent=sym.replace('_','/')+' ↗';const pair=sym.replace('_','').toUpperCase();link.href='https://www.tradingview.com/chart/?symbol='+encodeURIComponent('GATEIO:'+pair+'.P');link.target='_blank';link.rel='noopener noreferrer';link.title='Open '+pair+' perpetual chart in TradingView (new tab)';td.appendChild(link);}
function renderRadar(){let list=tickerData.slice();const q=document.getElementById('radarSearch').value.trim().toUpperCase();if(q)list=list.filter(x=>x.contract.includes(q));list.sort((a,b)=>radarMode==='gainers'?b.change-a.change:radarMode==='losers'?a.change-b.change:radarMode==='volume'?b.volume-a.volume:a.contract.localeCompare(b.contract));const lim=document.getElementById('radarLimit').value;if(lim!=='all')list=list.slice(0,Number(lim));radarBody.replaceChildren();for(const x of list){const tr=document.createElement('tr');addPair(tr,x.contract,'15m');addCell(tr,fmtPrice(x.last));addCell(tr,(x.change>0?'+':'')+x.change.toFixed(2)+'%',x.change>=0?'positive':'negative');addCell(tr,fmtVol(x.volume));radarBody.appendChild(tr);}if(!list.length){const tr=radarBody.insertRow();addCell(tr,'No pairs match').colSpan=4;}}
async function loadRadar(){const b=document.getElementById('radarRefresh');b.disabled=true;radarStatus.textContent='Updating public Gate.io tickers…';try{const r=await fetch('/terminal/radar',{cache:'no-store'}),d=await r.json();if(!r.ok)throw Error(d.error||'Radar error');tickerData=d.results;radarStatus.textContent=d.total+' USDT perpetual pairs · updated '+new Date(d.generated_at*1000).toLocaleTimeString('en-IN',{timeZone:'Asia/Kolkata',hour12:true})+' IST'+' · refresh every 60s';renderRadar();}catch(e){radarStatus.textContent='Radar unavailable: '+e.message;}finally{b.disabled=false}}
function renderScanner(){let list=scanData.filter(x=>scanMode==='all'||x.bias===scanMode);const q=document.getElementById('scannerSearch').value.trim().toUpperCase();if(q)list=list.filter(x=>x.contract.includes(q));list.sort((a,b)=>Math.abs(b.score)-Math.abs(a.score)||a.contract.localeCompare(b.contract));scanBody.replaceChildren();for(const x of list.slice(0,150)){const tr=document.createElement('tr');addPair(tr,x.contract,scanTf);addCell(tr,x.bias.toUpperCase(),x.bias==='bullish'?'positive':x.bias==='bearish'?'negative':'');addCell(tr,String(x.score));addCell(tr,x.rsi.toFixed(1));addCell(tr,x.volume_ratio.toFixed(2)+'x');scanBody.appendChild(tr);}if(!list.length){const tr=scanBody.insertRow();addCell(tr,'No matching scans yet').colSpan=5;}}
async function loadScanner(force=false){const b=document.getElementById('scannerRefresh');b.disabled=true;try{const r=await fetch('/terminal/scanner?tf='+scanTf+(force?'&refresh=1':''),{cache:'no-store'}),d=await r.json();if(!r.ok)throw Error(d.error||'Scanner error');scanData=d.results||[];scanStatus.textContent=(d.running?'Scanning… ':'Scan complete · ')+d.scanned+'/'+d.total+' pairs · '+d.failed+' unavailable · '+(d.generated_at?new Date(d.generated_at*1000).toLocaleTimeString('en-IN',{timeZone:'Asia/Kolkata',hour12:true})+' IST':'pending')+' · technical watchlist';renderScanner();}catch(e){scanStatus.textContent='Scanner unavailable: '+e.message;}finally{b.disabled=false}}
for(const b of document.querySelectorAll('#radarTabs button'))b.addEventListener('click',()=>{radarMode=b.dataset.radar;document.querySelectorAll('#radarTabs button').forEach(x=>x.classList.toggle('selected',x===b));renderRadar();});
for(const b of document.querySelectorAll('#scannerTabs button'))b.addEventListener('click',()=>{scanMode=b.dataset.scan;document.querySelectorAll('#scannerTabs button').forEach(x=>x.classList.toggle('selected',x===b));renderScanner();});
document.getElementById('radarRefresh').addEventListener('click',loadRadar);document.getElementById('radarSearch').addEventListener('input',renderRadar);document.getElementById('radarLimit').addEventListener('change',renderRadar);document.getElementById('scannerSearch').addEventListener('input',renderScanner);document.getElementById('scannerRefresh').addEventListener('click',()=>loadScanner(true));document.getElementById('scannerTf').addEventListener('change',e=>{scanTf=e.target.value;scanData=[];loadScanner();});
loadRadar();loadScanner();setInterval(()=>{if(!document.hidden)loadRadar()},60000);setInterval(()=>{if(!document.hidden)loadScanner()},15000);
})();

// Read-only market-outcome badges, scoped to existing signal buttons.
(function(){
 const detail=document.getElementById('callStatusDetail');
 const buttons=Array.from(document.querySelectorAll('button.call-status'));
 async function update(button){
  if(button.disabled)return;
  button.disabled=true;
  button.textContent='↻ Checking';
  try{
   const res=await fetch('/terminal/call-status/'+encodeURIComponent(button.dataset.eventId),{cache:'no-store'});
   const obj=await res.json();if(!res.ok)throw Error(obj.error||'Review failed');
   const state=obj.state||'UNVERIFIABLE';
   button.textContent=state==='LIVE'?'● LIVE':state==='TP HIT'?'✓ TP HIT':state==='SL HIT'?'✕ SL HIT':state==='AWAITING ENTRY'?'◷ ENTRY':state==='AWAITING DATA'?'◷ DATA':'? UNVERIFIABLE';
   button.style.color=state==='TP HIT'?'#57f6bd':state==='SL HIT'?'#ff829e':state==='LIVE'?'#9feaff':'#ffd28a';
   button.title=obj.detail||state;
   detail.textContent=(obj.symbol||'Signal')+' · '+state+' · '+(obj.detail||'')+' · Public candles only, not an exchange fill.';
  }catch(e){button.textContent='? UNAVAILABLE';detail.textContent='Call status check unavailable: '+e.message;}
  finally{button.disabled=false;}
 }
 buttons.forEach(b=>b.addEventListener('click',()=>update(b)));
 // Check a few recent signals at load; user can refresh any individual row.
 buttons.slice(0,3).forEach((b,i)=>setTimeout(()=>update(b),i*900));
 setInterval(()=>{if(!document.hidden)buttons.slice(0,3).forEach((b,i)=>setTimeout(()=>update(b),i*900))},120000);
})();

// News is display-only; has no access to trading functions or signals.
(function(){
 'use strict';
 const viewport=document.getElementById('newsViewport');
 const stack=document.getElementById('newsStack');
 const status=document.getElementById('newsFeedStatus');
 const refreshButton=document.getElementById('newsRefresh');
 const meta=x=>String(x||'').slice(0,90);
 const linkSafe=u=>{try{const x=new URL(u);return x.protocol==='https:'?x.href:null}catch(e){return null}};
 function render(items,updated){
   stack.replaceChildren();
   const valid=(items||[]).filter(x=>linkSafe(x.url)&&x.title).slice(0,12);
   if(!valid.length){const e=document.createElement('span');e.className='news-empty';e.textContent='No recent verified-source headlines available';stack.appendChild(e);return;}
   const add=(x)=>{
     const wrap=document.createElement('div');wrap.className='news-item';
     const a=document.createElement('a');a.href=linkSafe(x.url);a.target='_blank';a.rel='noopener noreferrer';a.textContent=x.title;a.title=x.title;wrap.appendChild(a);
     const info=document.createElement('span');info.className='news-meta';info.textContent=meta(x.source)+(x.published?' · '+meta(x.published):'');wrap.appendChild(info);stack.appendChild(wrap);
   };
   valid.forEach(add);
   if(valid.length>1){valid.forEach(add);stack.style.setProperty('--news-duration',String(Math.max(36,valid.length*6))+'s');stack.classList.add('news-animated');}
   status.textContent=String(valid.length)+' headlines · direct publisher links';
 }
 async function load(force=false){
   if(refreshButton)refreshButton.disabled=true;
   status.textContent=force?'Refreshing headlines…':'Updating headlines…';
   try{
     const ctl=new AbortController();const t=setTimeout(()=>ctl.abort(),15500);
     let res;
     try{res=await fetch('/terminal/news'+(force?'?refresh=1':''),{cache:'no-store',signal:ctl.signal})}finally{clearTimeout(t)}
     if(!res.ok)throw Error('News unavailable');
     const data=await res.json();render(data.items||[],data.fetched_at);
   }catch(e){status.textContent='News temporarily unavailable';if(!stack.querySelector('.news-item'))stack.textContent='News unavailable · trading terminal unaffected';}
   finally{if(refreshButton)refreshButton.disabled=false;}
 }
 if(refreshButton)refreshButton.addEventListener('click',()=>load(true));
 load();setInterval(()=>{if(!document.hidden)load()},300000);
})();

// Stage 3: manual, read-only, completed-candle paper review; no trading API writes.
(function(){
 const btn=document.getElementById('paperRefresh'), body=document.getElementById('paperBody'), label=document.getElementById('paperStatus'), total=document.getElementById('paperOpenPnlTotal');
 let quoteBusy=false; let refreshBusy=false;
 if(!btn||!body)return;
 async function refresh(){
  if(refreshBusy)return;refreshBusy=true;
  btn.disabled=true;label.textContent='Checking completed candles for hypothetical trades…';
  try{
   const res=await fetch('/terminal/paper-trades',{cache:'no-store'});const data=await res.json();
   if(!res.ok)throw Error(data.error||'Paper review unavailable');
   body.replaceChildren();
   const rows=data.trades||[];
   if(!rows.length){const r=body.insertRow(),c=r.insertCell();c.colSpan=10;c.textContent='No simulated orders stored yet.';}
   for(const x of rows){
    const r=body.insertRow();r.dataset.paperEventId=x.event_id;r.dataset.paperState=x.state;
    for(const v of [(x.created_at?new Date(x.created_at).toLocaleString('en-IN',{timeZone:'Asia/Kolkata',hour12:true})+' IST':'—'),x.symbol,x.side,x.entry,x.stop,x.take,x.state,x.state==='OPEN'?(x.mark_price??'—'):'—',x.state==='OPEN'?(x.unrealized_pnl_usdt??'—'):'—',x.state!=='OPEN'?(x.estimated_pnl_usdt??'—'):'—']){
     const c=r.insertCell();c.textContent=String(v===null?'—':v);
    }
   }
   label.textContent=rows.length+' stored simulated intents checked · '+(data.errors||0)+' unverified; no orders or exchange fills.';
   if(total){const available=rows.filter(x=>x.state==='OPEN' && x.unrealized_pnl_usdt!==null && x.unrealized_pnl_usdt!==undefined).length;
    total.textContent='OPEN PAPER UNREALIZED P&L (USDT): '+(data.open_unrealized_pnl_usdt===null?'Unavailable / incomplete market data':data.open_unrealized_pnl_usdt)+' · Priced '+available+'/'+rows.filter(x=>x.state==='OPEN').length+' open simulations · Public Gate.io last prices, estimated only; excludes fees, funding and slippage.';}
  }catch(e){label.textContent='Paper review unavailable: '+e.message+' (real trading remains OFF)';}
  finally{btn.disabled=false;refreshBusy=false;}
 }
 btn.addEventListener('click',refresh);
 // Fast read-only mark-to-market updates never invoke the lifecycle processor.
 async function refreshQuotes(){
  if(quoteBusy||document.hidden)return;quoteBusy=true;
  try{
   const controller=new AbortController();const timeout=setTimeout(()=>controller.abort(),6500);
   let res;
   try{res=await fetch('/terminal/paper-live-pnl',{cache:'no-store',signal:controller.signal});}
   finally{clearTimeout(timeout);}
   if(!res.ok)throw Error('Quote feed unavailable');
   const d=await res.json();const marks=d.marks||{};
   for(const r of body.querySelectorAll('tr[data-paper-event-id]')){
    if(r.dataset.paperState!=='OPEN')continue;
    const cells=r.cells;if(cells.length<10)continue;
    const mark=marks[r.dataset.paperEventId];
    cells[7].textContent=mark?.mark_price??'—';
    cells[8].textContent=mark?.unrealized_pnl_usdt??'—';
    const v=Number(mark?.unrealized_pnl_usdt);
    cells[8].classList.toggle('positive',mark&&Number.isFinite(v)&&v>0);
    cells[8].classList.toggle('negative',mark&&Number.isFinite(v)&&v<0);
   }
   if(total){
    const complete=d.open_count===d.priced && d.open_unrealized_pnl_usdt!==null;
    total.textContent='OPEN PAPER UNREALIZED P&L (USDT): '+(complete?d.open_unrealized_pnl_usdt:'Unavailable / incomplete prices')+' · Priced '+d.priced+'/'+d.open_count+' open simulations · Auto-updates about every 3 seconds · Last public prices (not exchange fills).';
    const v=Number(d.open_unrealized_pnl_usdt);
    total.classList.toggle('positive',complete&&Number.isFinite(v)&&v>0);
    total.classList.toggle('negative',complete&&Number.isFinite(v)&&v<0);
   }
  }catch(e){if(total)total.textContent='Paper live quotes temporarily unavailable; automatic retrying. No actual exchange P&L.';}
  finally{quoteBusy=false;}
 }
 // Lifecycle review runs slowly; quotes refresh independently without rebuilding the table.
 setInterval(()=>{if(!document.hidden)refresh();},60000);
 setInterval(refreshQuotes,3000);
 document.addEventListener('visibilitychange',()=>{if(!document.hidden){refresh();refreshQuotes();}});
 refresh().then(refreshQuotes);
})();

// Auto-update LuxAlgo signal receipts without manual page refresh.
// Lightweight database cursor check; full reload only after a NEW stored receipt.
(function(){
 'use strict';
 const status=document.getElementById('signalLiveSync');
 let lastSeen={{ latest_receipt_id|tojson }};
 let inFlight=false;
 let failures=0;
 async function check(){
  if(inFlight||document.hidden)return;
  inFlight=true;
  try{
   const response=await fetch('/terminal/receipt-cursor',{
    method:'GET',cache:'no-store',credentials:'same-origin',
    headers:{'Accept':'application/json'}
   });
   if(!response.ok)throw Error('HTTP '+response.status);
   const data=await response.json();
   if(!Number.isSafeInteger(data.latest_id)||data.latest_id<0)throw Error('Invalid cursor');
   failures=0;
   if(data.latest_id>lastSeen){
    status.textContent='● New LuxAlgo signal received — updating…';
    lastSeen=data.latest_id;
    window.location.reload();
    return;
   }
   status.textContent='● Signals auto-sync: ON';
  }catch(e){
   failures++;
   status.textContent='● Signal sync unavailable — retrying';
  }finally{inFlight=false;}
 }
 document.addEventListener('visibilitychange',()=>{if(!document.hidden)check();});
 check();
 setInterval(()=>{if(failures<3||!document.hidden)check();},4000);
})();

// Stage 16 V4 presentation. Reads existing paper table only; does not mutate trades.
(function(){
'use strict';
const table=document.getElementById('paperBody');
const btn=document.getElementById('paperRefresh');
const workspace=document.getElementById('v4Workspace');
const cardStatus=document.getElementById('v4PaperCardsStatus');
const legacyPaper=table&&table.closest('.panel');
if(!workspace||!document.getElementById('v4PaperCards'))return;
if(legacyPaper)legacyPaper.id='v4LegacyPaperHost';
// DO NOT hide the separate Gate.io real position cards.
// V4 card UI exists in the page; no obsolete chartShell/metric-grid dependency.
let demo=false;
const openIndicators=new Set(); // Only one chart is open at a time.
const examples=[['IRYSUSDT','LONG',20,.04,15.12,6,.29,.0734006,.07396,.063115],['BIRBUSDT','SHORT',50,-.03,-11.78,9.92,.19,.08079,.080634,.087],['INITUSDT','LONG',20,.00,3.26,5.98,.29,.153,.1533165,.1301081],['AKEUSDT','LONG',10,.01,2,5.5,.54,.039726,.039815,.0330489],['SOLUSDT','SHORT',5,.87,1.45,300,60,166.5,166.02,199.0],['AVAXUSDT','LONG',5,-.42,-.70,300,60,9.092,9.079,7.2]];
const esc=v=>String(v??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function numCell(k,v,extra='',field=''){return '<div><span class="v4-number-label">'+k+'</span><div class="v4-number-value '+extra+'"'+(field?' data-v45-field="'+field+'"':'')+'>'+esc(v)+'</div></div>'}
function chartMarkup(d){
 const sym=String(d.symbol||'').toUpperCase();
 if(!/^[A-Z0-9]{2,26}USDT$/.test(sym))return '<p class="light">Chart symbol unavailable.</p>';
 // TradingView's Binance perp public reference market; it is not an authenticated Gate.io fill feed.
 const token=sym.slice(0,-4), market='BINANCE:'+token+'USDT.P';
 return '<section class="v45-indicator" data-v45-chart="'+esc(sym)+'"><div class="v45-indicator-head"><span>'+esc(sym)+' · TradingView</span><span>15m · Public market</span></div><div class="v45-widget"><tv-advanced-chart symbol="'+esc(market)+'" interval="15" theme="dark" timezone="Asia/Kolkata" chart-type="candles"></tv-advanced-chart></div><div class="v45-indicator-foot"><span class="v45-entry">ENTRY: '+esc(d.entry)+'</span> · Reference level only (not plotted in widget). TradingView refresh rate depends on its feed. Paid LuxAlgo not embedded.</div></section>';
}
function card(d){
 const testnet=d.source==='GATE_FUTURES_TESTNET';
 const v=Number(d.pnl),valid=d.pnl!==null&&d.pnl!==undefined&&String(d.pnl).trim()!==''&&Number.isFinite(v);
 const gain=valid&&v>=0,cl=gain?'v4-pnl-good':'v4-pnl-bad';
 const pnl=valid?((v>0?'+':'')+d.pnl):'—',sym=String(d.symbol||'').toUpperCase(),open=openIndicators.has(sym);
 const tag=testnet?'TESTNET · EXCHANGE':(demo?'DEMO':'PAPER');
 const caption=testnet?'EXCHANGE UNREALIZED P&L (USDT)':'EST. PAPER UNREALIZED P&L (USDT)';
 const priceLabel=testnet?'GATE MARK PRICE':'PUBLIC LAST PRICE';
 const sizeLabel=testnet?'SIZE (GATE CONTRACTS)':'SIZE (CONTRACTS · PAPER)';
 const roiLabel=testnet?'ROI (exchange-derived)':'ROI (estimated)';
 const state=testnet?(d.feedStale?'STALE FEED · P&L NOT CURRENT':('Native SL/TP: '+String(d.protection||'UNVERIFIED')+' · '+String(d.origin||'TESTNET'))):(demo?'DEMO — simulated values':'Paper quote refresh runs automatically');
 const canClose=testnet&&d.protection==='EXCHANGE_VERIFIED'&&d.signalId&&/^[0-9]+$/.test(String(d.entryOrderId||''));
 const controls=testnet?('<button type="button" data-v45-inspect="1">VIEW TP/SL</button>'+
   '<button disabled title="Reversal requires a new opposite protected order with user-approved SL/TP">Reverse · LOCKED</button>'+
   '<button type="button" data-v45-close="market" '+(canClose?'':'disabled title="Verified exchange-linked position required"')+'>Market Close</button>'+
   '<button type="button" data-v45-close="close" '+(canClose?'':'disabled title="Verified exchange-linked position required"')+'>Close</button>'):
   '<button disabled>TP/SL</button><button disabled>Reverse</button><button disabled>Market Close</button><button disabled>Close</button>';
 return '<article class="v4-trade" data-v45-card="'+esc(d.event_id||sym)+'"><div class="v4-trade-top"><div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap"><span class="v4-side '+(d.side==='SHORT'?'short':'')+'">'+(d.side==='SHORT'?'S':'L')+'</span><button class="v4-token" type="button" data-v45-toggle="'+esc(sym)+'">'+esc(sym)+'</button><span class="v4-tag">Perp</span><span class="v4-tag">'+esc(d.leverage)+'</span><span class="v4-tag">'+esc(tag)+'</span><div class="v45-tools"><button class="v45-refresh" type="button" data-v45-refresh="'+esc(sym)+'" title="Refresh position" aria-label="Refresh '+esc(sym)+' position">↻</button><button class="v45-indicator-toggle" type="button" data-v45-toggle="'+esc(sym)+'" aria-expanded="'+String(open)+'">'+(open?'▲ CLOSE':'▼ INDICATOR')+'</button></div></div><div style="text-align:right"><div class="v4-number-label">'+esc(roiLabel)+'</div><div class="v4-number-value big '+cl+'" data-v45-field="roi">'+esc(d.roi)+'</div></div></div><div class="v4-numbers">'+numCell(caption,pnl,cl+' big','pnl')+numCell(sizeLabel,d.size)+numCell('MARGIN (USDT)',d.margin)+numCell('ENTRY PRICE',d.entry)+numCell(priceLabel,d.mark,'','mark')+numCell('LIQ. PRICE',d.liq)+numCell('STOP LOSS',d.sl)+numCell('TAKE PROFIT (TP1)',d.tp)+'</div><div class="v4-controls">'+controls+'</div><div class="v45-refresh-state" data-v45-state="1">'+esc(state)+(testnet&&d.protection==='EXCHANGE_VERIFIED'?' · SL #'+esc(d.stopOrderId)+' · TP #'+esc(d.takeOrderId):'')+'</div>'+ (open?chartMarkup(d):'')+'</article>';
}
function fromTable(){const rows=[...table.querySelectorAll('tr[data-paper-event-id]')].filter(r=>r.dataset.paperState==='OPEN');return rows.map(r=>{const c=[...r.cells].map(x=>x.textContent.trim());return {symbol:c[1],side:c[2],leverage:'Not verified',pnl:c[8],roi:'—',size:'Not verified',margin:'Not verified',entry:c[3],mark:c[7],liq:'Not available',sl:c[4],tp:c[5]}})}
let directPaperOpen=null;
let testnetOpen=null;
function currentData(){
 const papers=demo?examples.map(x=>({symbol:x[0],side:x[1],leverage:'Cross '+x[2]+'x',pnl:x[3],roi:x[4]+'%',size:x[5],margin:x[6],entry:x[7],mark:x[8],liq:x[9],sl:'Sample only',tp:'Sample only'})):(directPaperOpen===null?fromTable():directPaperOpen);
 return testnetOpen||[];
}
async function syncTestnetCards(){
 if(document.hidden||demo)return;
 const refresh=document.getElementById('v4TestnetRefresh');
 const status=document.getElementById('v4TestnetCardsStatus');
 if(refresh.disabled)return;
 refresh.disabled=true;
 try{
  const ctrl=new AbortController(),timer=setTimeout(()=>ctrl.abort(),13000);
  let r;
  try{r=await fetch('/terminal/testnet-position-cards',{cache:'no-store',signal:ctrl.signal});}
  finally{clearTimeout(timer);}
  const feed=await r.json();
  if(!r.ok||feed.environment!=='GATE_FUTURES_TESTNET'||!Array.isArray(feed.positions))throw Error(feed.error||'Unverified Testnet response');
  testnetOpen=feed.positions.map(p=>{
   const safe=p.protection_state==='EXCHANGE_VERIFIED';
   return {
    event_id:'TESTNET:'+p.contract+':'+p.side,source:'GATE_FUTURES_TESTNET',
    contract:p.contract,signalId:p.signal_id??null,entryOrderId:p.entry_order_id??null,
    symbol:p.symbol,side:p.side,leverage:p.leverage_label??'—',
    pnl:p.unrealized_pnl??'—',roi:p.roi_percent===null?'—':p.roi_percent+'%',
    size:p.size_contracts??'—',margin:p.margin_usdt??'—',
    entry:p.entry_price??'—',mark:p.mark_price??'—',liq:p.liquidation_price??'—',
    sl:safe?p.stop_loss:'NOT VERIFIED',tp:safe?p.take_profit:'NOT VERIFIED',
    protection:p.protection_state,origin:p.origin,
    stopOrderId:safe?p.stop_order_id:null,takeOrderId:safe?p.take_order_id:null
   };
  });
  status.textContent=testnetOpen.length+' GATE.IO TESTNET exchange position(s) · '+new Date(feed.fetched_at*1000).toLocaleTimeString('en-IN',{timeZone:'Asia/Kolkata',hour12:true})+' IST · Native protection rechecked on refresh · READ ONLY';
  draw();
 }catch(err){
  // Keep the position visible but never display stale mark/PnL or protection as current.
  if(testnetOpen)testnetOpen=testnetOpen.map(p=>({...p,feedStale:true,pnl:'—',roi:'—',mark:'—',sl:'NOT VERIFIED',tp:'NOT VERIFIED',protection:'READBACK_UNAVAILABLE'}));
  status.textContent='TESTNET FEED UNAVAILABLE — position may still be OPEN at Gate.io; current P&L and SL/TP cannot be verified: '+String(err.message).slice(0,100);
  draw(true);
 }finally{refresh.disabled=false;}
}
document.getElementById('v4TestnetRefresh').addEventListener('click',syncTestnetCards);

async function syncDirectPaperCards(){
 if(document.hidden||demo)return;
 try{
  const ctl=new AbortController(),timer=setTimeout(()=>ctl.abort(),7000);
  let response;
  try{response=await fetch('/terminal/paper-open-direct',{cache:'no-store',signal:ctl.signal});}
  finally{clearTimeout(timer);}
  const payload=await response.json();
  if(!response.ok||!Array.isArray(payload.positions))throw Error('Paper records unavailable');
  const previous=new Map((directPaperOpen||[]).map(p=>[p.event_id,p]));
  directPaperOpen=payload.positions.map(p=>({
   event_id:p.event_id,symbol:p.symbol,side:p.side,
   leverage:'PAPER · no exchange leverage',
   pnl:previous.get(p.event_id)?.pnl??'—',roi:'—',
   size:p.size_contracts??'Not verified',margin:'Not verified',
   entry:p.entry??'—',mark:previous.get(p.event_id)?.mark??'—',
   liq:'Not available',sl:p.stop_loss??'—',tp:p.take_profit??'—'
  }));
  draw();
  if(cardStatus)cardStatus.textContent=directPaperOpen.length+' OPEN LuxAlgo PAPER position cards · Data from PostgreSQL · Not Gate.io exchange fills';
  syncDirectPaperQuotes();
 }catch(error){
  if(cardStatus)cardStatus.textContent='Open paper positions temporarily unavailable: '+error.message+' · Existing cards retained';
  // Preserve existing cards if the independent read-only position feed is unavailable.
 }
}
let priceBusy=false;
async function syncDirectPaperQuotes(){
 if(document.hidden||demo||!directPaperOpen||priceBusy)return;
 priceBusy=true;
 try{
  const ctl=new AbortController(),timer=setTimeout(()=>ctl.abort(),6500);
  let response;
  try{response=await fetch('/terminal/paper-live-pnl',{cache:'no-store',signal:ctl.signal});}
  finally{clearTimeout(timer);}
  const quotes=await response.json();
  if(!response.ok||quotes.source!=='GATE_IO_PUBLIC_LAST_PRICE'||!quotes.marks)
    throw Error('Paper public quotes unavailable');
  for(const d of directPaperOpen){
   const mark=quotes.marks[d.event_id];
   d.mark=mark?.mark_price??'—';
   d.pnl=mark?.unrealized_pnl_usdt??'—';
  }
  draw();
  if(cardStatus)cardStatus.textContent=directPaperOpen.length+' OPEN PAPER position cards · Public quote refresh checked · Unrealized P&L is estimated, NOT an exchange fill';
 }catch(error){
  // Never turn a missing quote into zero profit; leave unpriced placeholders.
  for(const d of directPaperOpen){d.mark='—';d.pnl='—';}
  draw();
  if(cardStatus)cardStatus.textContent='Paper cards visible · Public quotes unavailable ('+error.message+') · P&L not verified; NOT exchange P&L';
 }finally{priceBusy=false;}
}
let lastStructure='';
function draw(force=false){
 const data=currentData();const cards=document.getElementById('v4PaperCards');
 const structure='TESTNET|' + data.map(d=>String(d.event_id||d.symbol)+'/'+d.side+'/'+String(d.protection||'')+'/'+String(d.feedStale||'')+'/'+openIndicators.has(String(d.symbol))).join('|');
 document.getElementById('v4TabCount').textContent='Gate.io Testnet Positions ('+data.length+')';
 if(force||structure!==lastStructure){
  cards.innerHTML=data.length?data.map(card).join(''):'<div class="v4-prompt">No open Gate.io Testnet positions. This does not imply paper or real-money orders.</div>';
  lastStructure=structure;
 }else{
  // Update prices/P&L without destroying open TradingView web components.
  for(const d of data){const c=[...cards.querySelectorAll('[data-v45-card]')].find(x=>x.dataset.v45Card===(d.event_id||d.symbol));if(!c)continue;
   const pnl=Number.isFinite(Number(d.pnl))?((Number(d.pnl)>0?'+':'')+d.pnl):'—';
   for(const [field,val] of [['pnl',pnl],['roi',d.roi],['mark',d.mark]]){const e=c.querySelector('[data-v45-field="'+field+'"]');if(e&&e.textContent!==String(val))e.textContent=val;}
  }
 }
}
workspace.querySelector('#v4DemoToggle').onclick=()=>{demo=!demo;openIndicators.clear();lastStructure='';document.getElementById('v4DemoWarning').hidden=!demo;document.getElementById('v4DemoToggle').textContent=demo?'RETURN TO REAL PAPER DATA':'SHOW SAMPLE LAYOUT';draw(true)};
workspace.addEventListener('click',async e=>{
 const action=e.target.closest('[data-v45-inspect],[data-v45-close]');
 if(action){
  const container=action.closest('[data-v45-card]'),id=container?.dataset.v45Card;
  const position=(testnetOpen||[]).find(p=>p.event_id===id);
  const state=container?.querySelector('[data-v45-state]');
  if(!position||position.source!=='GATE_FUTURES_TESTNET')return;
  if(action.hasAttribute('data-v45-inspect')){
   if(state)state.textContent='GATE TESTNET SL: '+position.sl+' [#'+(position.stopOrderId||'—')+'] · TP: '+position.tp+' [#'+(position.takeOrderId||'—')+'] · '+position.protection;
   return;
  }
  if(!position.signalId||!position.entryOrderId||position.protection!=='EXCHANGE_VERIFIED')return;
  if(!window.confirm('TESTNET ONLY: send ONE reduce-only market order to close '+position.contract+' '+position.side+'? This does not affect real-money positions.'))return;
  const csrf=document.querySelector('input[name="csrf_token"]')?.value;
  if(!csrf){if(state)state.textContent='No valid terminal CSRF token. Refresh and sign in.';return;}
  const buttons=container.querySelectorAll('[data-v45-close]');
  buttons.forEach(b=>b.disabled=true);
  if(state)state.textContent='Submitting ONE protected Testnet close; do not repeat if network response is uncertain…';
  try{
   const res=await fetch('/terminal/testnet-manual-close',{
    method:'POST',cache:'no-store',
    headers:{'Content-Type':'application/json','X-Terminal-CSRF':csrf},
    body:JSON.stringify({signal_id:position.signalId,contract:position.contract,
      side:position.side,entry_order_id:String(position.entryOrderId)})
   });
   const result=await res.json();
   if(!res.ok||result.state!=='CLOSED_VERIFIED'){
    if(state)state.textContent='CLOSE NOT VERIFIED: '+String(result.state||result.error||res.status)+' · Inspect Gate Testnet directly; no automatic retry.';
   }else{
    if(state)state.textContent='TESTNET CLOSE VERIFIED · Refreshing exchange position feed';
    syncTestnetCards();
   }
  }catch(err){if(state)state.textContent='TESTNET CLOSE OUTCOME UNKNOWN · Check exchange before any retry.';}
  return;
 }
 const r=e.target.closest('[data-v45-refresh]');
 if(r){
  const c=r.closest('[data-v45-card]');const state=c?.querySelector('[data-v45-state]');
  if(demo){if(state)state.textContent='DEMO — values are illustrative; no exchange refresh';return;}
  // Refresh the correct read-only source; never issues an exchange order.
  r.disabled=true;if(state)state.textContent='Refreshing position…';
  if(c?.dataset.v45Card?.startsWith('TESTNET:'))syncTestnetCards();
  else btn?.click();
  setTimeout(()=>{r.disabled=false;if(state&&state.isConnected)state.textContent='Refresh requested · '+new Date().toLocaleTimeString('en-IN',{timeZone:'Asia/Kolkata',hour12:true})+' IST';},1400);
  return;
 }
 const b=e.target.closest('[data-v45-toggle]');if(!b)return;
 const sym=b.dataset.v45Toggle;if(!/^[A-Z0-9]{2,26}USDT$/.test(sym))return;
 if(openIndicators.has(sym)){openIndicators.clear();}else{openIndicators.clear();openIndicators.add(sym);}
 draw(true);
});
if(table)new MutationObserver(()=>{if(!demo&&directPaperOpen===null)draw()}).observe(table,{childList:true,subtree:true,characterData:true});
draw(true);
syncTestnetCards();
setInterval(syncTestnetCards,20000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)syncTestnetCards();});
})();
</script>
<script>
/* PAPER HISTORY v1 — isolated read-only panel; never modifies live/paper positions. */
(function(){
 'use strict';
 const workspace=document.getElementById('v4Workspace');
 if(!workspace)return;
 const panel=document.createElement('section');
 panel.className='panel'; panel.id='paperClosedHistoryPanel';
 panel.innerHTML='<div class="panel-heading"><h2>PAPER TRADE HISTORY · COMPLETED</h2><span class="label-muted">SIMULATED RESULTS ONLY · NO EXCHANGE FILLS</span></div>'+
 '<div class="perf-grid" id="paperHistoryMetrics"><div><span>Closed Paper Trades</span><strong id="paperHistoryClosed">—</strong></div><div><span>TP</span><strong id="paperHistoryWins">—</strong></div><div><span>SL</span><strong id="paperHistoryLosses">—</strong></div><div><span>Win Rate (Paper)</span><strong id="paperHistoryRate">—</strong></div><div><span>Estimated Closed P&amp;L (USDT)</span><strong id="paperHistoryPnl">—</strong></div></div>'+
 '<p class="light" id="paperHistoryStatus" role="status">Loading closed paper trade records…</p>'+
 '<div class="tablewrap"><table><thead><tr><th>CREATED (IST)</th><th>PAIR</th><th>SIDE</th><th>SIM ENTRY</th><th>SIM EXIT</th><th>RESULT</th><th>EST. P&amp;L (USDT)</th></tr></thead><tbody id="paperHistoryBody"><tr><td colspan="7">Loading…</td></tr></tbody></table></div>'+
 '<p class="light">Historical candle-based outcomes only. Excludes fees, funding and slippage. Not actual exchange fills or verified realized P&amp;L. Latest 100 closed paper trades.</p>';
 workspace.after(panel);
 let busy=false;
 const put=(id,value)=>{const el=document.getElementById(id);if(el)el.textContent=String(value??'—')};
 async function refresh(){
  if(busy||document.hidden)return;busy=true;
  try{
   const ctl=new AbortController();const timer=setTimeout(()=>ctl.abort(),9000);
   let response;
   try{response=await fetch('/terminal/paper-history',{cache:'no-store',signal:ctl.signal})}
   finally{clearTimeout(timer)}
   if(!response.ok)throw Error('History endpoint unavailable');
   const d=await response.json();const trades=Array.isArray(d.trades)?d.trades:[];const a=d.summary||{};
   put('paperHistoryClosed',a.closed??0);put('paperHistoryWins',a.tp??0);put('paperHistoryLosses',a.sl??0);
   put('paperHistoryRate',a.win_rate_pct===null||a.win_rate_pct===undefined?'—':a.win_rate_pct+'%');
   put('paperHistoryPnl',a.estimated_pnl_usdt??'—');
   const body=document.getElementById('paperHistoryBody');body.replaceChildren();
   if(!trades.length){const r=body.insertRow();const c=r.insertCell();c.colSpan=7;c.textContent='No closed simulated trades yet.'}
   for(const x of trades){
    const row=body.insertRow();
    for(const value of [(x.created_at?new Date(x.created_at).toLocaleString('en-IN',{timeZone:'Asia/Kolkata',hour12:true})+' IST':'—'),x.symbol,x.side,x.entry,x.exit_price,x.state,x.estimated_pnl_usdt]){
     const cell=row.insertCell();cell.textContent=String(value??'—');
    }
    row.cells[5].classList.add(x.state==='TP'?'positive':'negative');
    const pnl=Number(x.estimated_pnl_usdt);
    if(x.estimated_pnl_usdt!==null&&Number.isFinite(pnl))row.cells[6].classList.add(pnl>=0?'positive':'negative');
   }
   put('paperHistoryStatus',trades.length+' completed paper records shown · figures are estimates, not Gate.io fills.');
  }catch(err){put('paperHistoryStatus','Paper history temporarily unavailable; existing trades and cards are unaffected.');}
  finally{busy=false;}
 }
 refresh();setInterval(refresh,60000);
 document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh()});
})();
</script>
<script>
(function(){'use strict';
 const panel=document.getElementById('incomingLuxPanel');
 const tbody=document.getElementById('incomingLuxRows');
 const info=document.getElementById('incomingLuxInfo');
 const refreshButton=document.getElementById('incomingLuxRefresh');
 const clearButton=document.getElementById('incomingLuxClear');
 const filters=document.getElementById('incomingLuxFilters');
 const scrollArea=document.getElementById('incomingLuxScroll');
 if(!panel||!tbody||!info||!refreshButton||!clearButton||!filters||!scrollArea)return;
 let allSignals=[],chosen='ALL',pending=false;
 const addCell=(tr,value)=>{
   const td=document.createElement('td');
   td.textContent=value===null||value===undefined||value===''?'—':String(value);
   tr.appendChild(td);return td;
 };
 function draw(){
   tbody.replaceChildren();
   const matches=chosen==='ALL'?allSignals:allSignals.filter(x=>x.status_label===chosen);
   for(const [index,s] of matches.entries()){
     const row=document.createElement('tr');
     let time='—';try{time=new Date(s.time).toLocaleString('en-IN',{timeZone:'Asia/Kolkata',hour12:true})+' IST'}catch(e){}
     for(const value of [index+1,time,s.symbol,s.side,s.timeframe,s.order_type,
       s.entry,s.stop_loss,s.take_profit])addCell(row,value);
     const slot=addCell(row,'');
     const statusButton=document.createElement('button');
     statusButton.type='button';statusButton.className='btn';
     statusButton.textContent=s.status_label||'VERIFY EXCHANGE';
     statusButton.title='Click to refresh the latest exchange-backed status';
     statusButton.style.fontWeight='800';
     statusButton.style.color=s.status_label==='LIVE'?'#6cf3ba':
       s.status_label==='CLOSED'?'#8ed7ff':s.status_label==='WAITING FOR EXECUTION'?'#ffd28a':'#ffadad';
     statusButton.addEventListener('click',()=>refresh());
     slot.appendChild(statusButton);
     addCell(row,s.reason);tbody.appendChild(row);
   }
   if(!matches.length){
     const tr=document.createElement('tr'),td=document.createElement('td');
     td.colSpan=11;td.textContent=allSignals.length?
       'No '+chosen+' signals after reset.':'0 incoming signals since reset · waiting for a fresh LuxAlgo alert';
     tr.appendChild(td);tbody.appendChild(tr);
   }
   info.textContent='Latest '+allSignals.length+'/25 visible signals · '+matches.length+
       ' in '+chosen+' view · first 10 rows; scroll down for the rest · safety audit retained';
 }
 async function refresh(){
   if(pending)return;
   pending=true;refreshButton.disabled=true;
   try{
     const response=await fetch('/terminal/incoming-signals',{cache:'no-store'});
     const data=await response.json();
     if(!response.ok||!Array.isArray(data.signals))throw Error(data.error||'Status monitor unavailable');
     allSignals=data.signals;draw();
   }catch(e){info.textContent='Live signal status unavailable: '+e.message+' · previous statuses not refreshed';}
   finally{pending=false;refreshButton.disabled=false;}
 }
 filters.addEventListener('click',e=>{
   const button=e.target.closest('[data-lux-filter]');
   if(!button)return;
   chosen=button.dataset.luxFilter;
   scrollArea.scrollTop=0;
   for(const b of filters.querySelectorAll('[data-lux-filter]')){
      b.classList.toggle('selected',b.dataset.luxFilter===chosen);
   }
   draw();
 });
 clearButton.addEventListener('click',async()=>{
   if(pending||clearButton.disabled)return;
   if(!window.confirm('Reset incoming LuxAlgo signals to 0 on this dashboard? Past exchange orders, audit logs and duplicate protection will NOT be deleted.'))return;
   clearButton.disabled=true;info.textContent='Saving visible-history reset…';
   try{
     const response=await fetch('/terminal/incoming-signals/reset',{
       method:'POST',cache:'no-store',
       headers:{'Content-Type':'application/json','X-Terminal-CSRF':{{ csrf_token|tojson }}},
       body:'{}'});
     const result=await response.json();
     if(!response.ok||result.status!=='CLEARED')throw Error(result.error||'Reset not confirmed');
     allSignals=[];chosen='ALL';
     for(const b of filters.querySelectorAll('[data-lux-filter]')){
       b.classList.toggle('selected',b.dataset.luxFilter==='ALL');
     }
     draw();await refresh();
   }catch(e){info.textContent='Visible-history reset failed: '+e.message;}
   finally{clearButton.disabled=false;}
 });
 refreshButton.addEventListener('click',refresh);
 panel.addEventListener('toggle',()=>{if(panel.open)refresh()});
 refresh();setInterval(()=>{if(panel.open&&!document.hidden)refresh()},30000);
})();
</script>
<script>
(function(){'use strict';
 const pane=document.getElementById('gateTestnetPositionPanel'),body=document.getElementById('gateTestnetPositionRows'),status=document.getElementById('gateTestnetPositionStatus'),button=document.getElementById('gateTestnetPositionRefresh');
 if(!pane||!body||!status||!button)return;
 function cell(tr,value){const td=document.createElement('td');td.textContent=value===null||value===undefined?'—':String(value);tr.appendChild(td);}
 async function refresh(){if(button.disabled)return;button.disabled=true;
  try{const response=await fetch('/terminal/testnet-positions',{cache:'no-store'}),data=await response.json();if(!response.ok)throw Error(data.error||'Testnet unavailable');
   body.replaceChildren();for(const pos of data.positions){const tr=document.createElement('tr');[pos.contract,pos.side,pos.size_contracts,pos.entry_price,pos.mark_price,pos.unrealized_pnl,pos.leverage_label].forEach(v=>cell(tr,v));body.appendChild(tr);}
   if(!data.positions.length){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=7;td.textContent='No OPEN TESTNET positions reported by Gate.io';tr.appendChild(td);body.appendChild(tr);}
   status.textContent=data.count+' verified Testnet positions · '+new Date(data.fetched_at*1000).toLocaleTimeString('en-IN',{timeZone:'Asia/Kolkata'})+' IST';
  }catch(error){status.textContent='Testnet position verification failed: '+error.message;body.replaceChildren();}
  finally{button.disabled=false;}}
 button.addEventListener('click',refresh);pane.addEventListener('toggle',()=>{if(pane.open)refresh()});refresh();
 setInterval(()=>{if(pane.open&&!document.hidden)refresh()},60000);
})();
</script>
<script>
(function(){'use strict';
const pane=document.getElementById('testnetOrdersPanel'),rows=document.getElementById('testnetOrdersRows'),status=document.getElementById('testnetOrdersStatus'),btn=document.getElementById('testnetOrdersRefresh');
if(!pane||!rows||!status||!btn)return;
async function refresh(){if(btn.disabled)return;btn.disabled=true;
try{const res=await fetch('/terminal/testnet-open-orders',{cache:'no-store'}),data=await res.json();if(!res.ok)throw Error(data.error||'Unavailable');
rows.replaceChildren();for(const o of data.orders){const tr=document.createElement('tr');for(const key of ['contract','order_id','size','price','remaining','tif','status']){const td=document.createElement('td');td.textContent=o[key]||'—';tr.appendChild(td);}rows.appendChild(tr);}
if(!data.orders.length){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=7;td.textContent='Gate.io reports no unfilled Testnet orders';tr.appendChild(td);rows.appendChild(tr);}status.textContent=data.count+' actual open Testnet orders';}
catch(err){status.textContent='Unable to verify: '+err.message;rows.replaceChildren();}finally{btn.disabled=false;}}
btn.addEventListener('click',refresh);pane.addEventListener('toggle',()=>{if(pane.open)refresh()});
})();
</script>
<script>
(function(){'use strict';
const get=id=>document.getElementById(id);
const button=get('testnetReadinessRefresh');
if(!button)return;
const set=(id,value)=>{const target=get(id);if(target)target.textContent=String(value);};
let refreshing=false;
async function refresh(){
  if(refreshing)return;
  refreshing=true;button.disabled=true;
  set('tnReadStatus','Checking…');
  try{
    const response=await fetch('/terminal/testnet-live-readiness',{cache:'no-store'});
    const data=await response.json();
    if(!response.ok)throw Error(data.error||'Exchange readiness unavailable');
    set('tnReadConnection',data.account_connected===true?'CONNECTED':'NOT VERIFIED');
    set('tnReadHedge',data.dual_mode===true?'ON · Dual':'OFF / UNVERIFIED');
    set('tnReadLeverage',data.configured_leverage?String(data.configured_leverage)+'×':'UNVERIFIED');
    set('tnReadProtection',data.native_protection_readback===true?'READABLE':'NOT VERIFIED');
    set('tnReadStatus',data.ready===true?'PREFLIGHT PASS':'BLOCKED');
    const reasons=Array.isArray(data.blockers)?data.blockers:[];
    set('tnReadBlockers',reasons.length?'Blocking checks: '+reasons.join(' · '):'Read-only checks passed; actual protected Testnet trade still unverified.');
    set('tnReadChecked','Last API check: '+new Date(data.checked_at).toLocaleString('en-IN',{timeZone:'Asia/Kolkata'})+' IST');
  }catch(error){
    set('tnReadStatus','UNAVAILABLE');
    set('tnReadBlockers','Cannot verify Testnet readiness: '+error.message);
    set('tnReadChecked','Readiness data unavailable');
  }finally{refreshing=false;button.disabled=false;}
  try{
    const response=await fetch('/terminal/testnet-protected-journal',{cache:'no-store'});
    if(!response.ok)throw Error('journal unreachable');
    const data=await response.json();
    const rows=Array.isArray(data.journal)?data.journal:[];
    set('tnReadJournal','Actual Testnet execution journal: '+rows.length+' record(s); automatic Testnet entries '+(data.testnet_automation_enabled?'ENABLED':'OFF')+'. '+(rows.length?'Latest state: '+rows[0].state:'No Testnet entries confirmed.'));
  }catch(error){set('tnReadJournal','Execution journal unavailable: '+error.message);}
  try{
    const response=await fetch('/terminal/testnet-signal-trace',{cache:'no-store'});
    if(!response.ok)throw Error('signal trace unavailable');
    const data=await response.json(),r=data.latest_signal;
    if(!r){set('tnReadLatestSignal','No LuxAlgo webhook signals have been received.');}
    else{
      const time=new Date(r.received_at).toLocaleString('en-IN',{timeZone:'Asia/Kolkata'});
      const paper=r.paper_status==='SIMULATED_ONLY'?'PAPER ONLY (no exchange fill)':(r.paper_status||'NOT RECORDED');
      const exchange=r.testnet_state||'NO TESTNET ORDER RECORDED';
      const why=r.handoff_reason?' · Testnet handoff: '+r.handoff_reason:'';
      set('tnReadLatestSignal','Latest webhook: '+r.symbol+' '+r.side+' ('+r.timeframe+') · '+time+' IST · '+paper+' · '+exchange+why);
    }
  }catch(error){set('tnReadLatestSignal','Latest signal trace unavailable: '+error.message);}
}
button.addEventListener('click',refresh);
refresh();
setInterval(()=>{if(!document.hidden)refresh();},120000);
})();
</script>

</body></html>'''


# Curated public publisher feeds. This UI is editorial news, not verified trade signals.
NEWS_FEEDS = (
    ('CoinDesk', 'https://www.coindesk.com/arc/outboundfeeds/rss/', 'www.coindesk.com'),
    ('The Block', 'https://www.theblock.co/rss.xml', 'www.theblock.co'),
    ('Decrypt', 'https://decrypt.co/feed', 'decrypt.co'),
    ('Cointelegraph', 'https://cointelegraph.com/rss', 'cointelegraph.com'),
)
_news_cache = {'expires_at': 0, 'items': [], 'fetched_at': None}
_news_lock = Lock()

def _news_fetch_source(source):
    label, url, host = source
    req = Request(url, headers={'User-Agent':'LuxAlgoPRO-NewsTicker/1.0 (+RSS reader)', 'Accept':'application/rss+xml,application/atom+xml,application/xml,text/xml'})
    with urlopen(req, timeout=7) as res:
        xml = res.read(450000)
    root = ET.fromstring(xml)
    entries = root.findall('.//item')
    if not entries:
        entries = root.findall('.//{http://www.w3.org/2005/Atom}entry')
    items = []
    for item in entries[:12]:
        def field(name):
            element = item.find(name)
            return (element.text or '').strip() if element is not None and element.text else ''
        title = field('title') or field('{http://www.w3.org/2005/Atom}title')
        link = field('link')
        if not link:
            el = item.find('{http://www.w3.org/2005/Atom}link')
            link = el.get('href','') if el is not None else ''
        parts=urlsplit(link)
        if parts.scheme!='https' or (parts.hostname or '').lower() not in (host,host.removeprefix('www.')):
            continue
        date_str = field('pubDate') or field('{http://www.w3.org/2005/Atom}published') or field('{http://www.w3.org/2005/Atom}updated')
        date_ts = 0
        if date_str:
            try:
                dt=parsedate_to_datetime(date_str)
            except (TypeError,ValueError,IndexError):
                try: dt=datetime.fromisoformat(date_str.replace('Z','+00:00'))
                except ValueError: dt=None
            if dt:
                if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
                date_ts=dt.timestamp()
        if not title or not date_ts or date_ts>time.time()+600 or date_ts<time.time()-3*86400:
            continue
        items.append({'title': title[:200], 'url': link, 'source': label,
                      'published': datetime.fromtimestamp(date_ts,timezone(timedelta(hours=5,minutes=30))).strftime('%d %b %H:%M IST'),
                      '_timestamp':date_ts})
    return items

@app.get('/terminal/news')
def terminal_news():
    if not _terminal_permitted():
        return _terminal_guard()
    now=time.time()
    with _news_lock:
        cached=dict(_news_cache)
    force=request.args.get('refresh')=='1'
    # Manual refresh bypasses the 10-minute cache but is throttled to once/minute.
    if cached['expires_at']>now and (not force or now-cached.get('fetched_at',0)<60):
        response=jsonify(items=cached['items'],fetched_at=cached['fetched_at'],status='cached')
        response.headers['Cache-Control']='private, no-store'
        return response
    found=[]
    # Independent timeouts; publisher outages must never break the trading dashboard.
    with ThreadPoolExecutor(max_workers=len(NEWS_FEEDS)) as pool:
        jobs=[pool.submit(_news_fetch_source, source) for source in NEWS_FEEDS]
        for job in jobs:
            try: found.extend(job.result(timeout=9))
            except Exception as exc:
                app.logger.warning('Crypto news provider unavailable: %s', type(exc).__name__)
    found.sort(key=lambda x:x['_timestamp'],reverse=True)
    seen=set(); articles=[]
    for article in found:
        if article['url'] in seen: continue
        seen.add(article['url']);article.pop('_timestamp',None);articles.append(article)
        if len(articles)>=12: break
    with _news_lock:
        if articles:
            _news_cache.update(items=articles, expires_at=now+600, fetched_at=int(now))
        else:
            # Do not clear fresh-enough news during a temporary feed outage.
            existing=_news_cache.get('items',[])
            if _news_cache.get('fetched_at') and now-_news_cache['fetched_at']<3*3600:
                articles=existing
            else:
                articles=[]
            _news_cache.update(expires_at=now+120,items=articles)
    response=jsonify(items=articles,fetched_at=_news_cache.get('fetched_at'),status='ok' if articles else 'unavailable')
    response.headers['Cache-Control']='private, no-store'
    return response

def expected_webhook_token():
    return os.getenv('LUXALGO_WEBHOOK_TOKEN', '')


def authorized(data):
    expected = expected_webhook_token()
    if len(expected) < 24:
        return False
    supplied = data.get('token', '')
    return isinstance(supplied, str) and hmac.compare_digest(supplied, expected)


def _terminal_permitted():
    # Do NOT accept cached HTTP Basic credentials after logout.
    # A signed, HttpOnly session cookie is needed for a reliable logout.
    return (len(os.getenv('TERMINAL_PASSWORD','')) >= 24 and
            session.get('authenticated') is True and
            session.get('user') == os.getenv('TERMINAL_USER','admin'))


def _terminal_guard():
    if len(os.getenv('TERMINAL_PASSWORD', '')) < 24:
        return Response('Private terminal unavailable. Set a password of at least 24 characters in Render.',status=503)
    if request.path.startswith('/terminal/'):
        return jsonify(error='Session expired; sign in again',login_url='/login'),401
    return redirect(url_for('terminal_login', next=request.path),code=303)


LOGIN_PAGE = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>LUXALGO PRO · Sign In</title>
<style>body{background:radial-gradient(ellipse at top,#173f65,#08111f 70%);color:#e8f7ff;font:15px system-ui,Arial;min-height:100vh;display:grid;place-items:center;margin:0}.panel{box-sizing:border-box;width:min(420px,94vw);background:linear-gradient(160deg,#163553,#102034);padding:32px;border:1px solid #3d688c;border-radius:16px;box-shadow:0 20px 45px #0008}h1{font-size:22px;margin:0 0 8px}p{color:#abc9df;margin:0 0 23px}label{display:block;margin:13px 0 6px;color:#aed7f0}input{width:100%;box-sizing:border-box;padding:12px;background:#08192b;border:1px solid #46759b;border-radius:8px;color:white;font:inherit}button{width:100%;cursor:pointer;margin-top:24px;padding:13px;border-radius:9px;border:1px solid #bcf0ff;font-weight:900;color:#fff;background:linear-gradient(#8cdcff,#278bd1 50%,#124875 52%,#2980c3);box-shadow:inset 0 2px 3px #fff7,0 5px 17px #0a8aff55}.error{color:#ffc2cd;margin-top:15px}</style></head><body><main class="panel"><h1>LUXALGO PRO</h1><p>Private Trading Terminal · Secure Sign In</p><form action="/login" method="post"><input type="hidden" name="next" value="{{ next_path|e }}"><label for="username">Username</label><input id="username" name="username" autocomplete="username" required autofocus><label for="password">Password</label><input id="password" type="password" name="password" autocomplete="current-password" required><button type="submit">SIGN IN</button></form>{% if error %}<p class="error">Incorrect username/password or too many attempts. Please retry.</p>{% endif %}</main></body></html>'''


def _safe_next(path):
    return path if isinstance(path,str) and path.startswith('/') and not path.startswith('//') and not path.startswith('/\\') else '/'


@app.route('/login',methods=['GET','POST'])
def terminal_login():
    if len(os.getenv('TERMINAL_PASSWORD','')) < 24:
        return Response('Configure TERMINAL_PASSWORD (minimum 24 characters) in Render first.',status=503)
    if request.method=='GET':
        return Response(render_template_string(LOGIN_PAGE,error=False,next_path=_safe_next(request.args.get('next','/'))),headers={'Cache-Control':'no-store'})
    # Minimal in-memory login attempt throttle; independent of exchange credentials.
    ip=(request.remote_addr or 'unknown')[:80];now=time.monotonic()
    with _login_lock:
        failures=[t for t in _login_failures.get(ip,[]) if now-t<900]
        _login_failures[ip]=failures
        limited=len(failures)>=8
    name=request.form.get('username','');password=request.form.get('password','')
    valid=(not limited and hmac.compare_digest(name,os.getenv('TERMINAL_USER','admin'))
           and hmac.compare_digest(password,os.getenv('TERMINAL_PASSWORD','')))
    if not valid:
        with _login_lock:
            _login_failures.setdefault(ip,[]).append(now)
        return Response(render_template_string(LOGIN_PAGE,error=True,next_path='/'),status=429 if limited else 401,headers={'Cache-Control':'no-store'})
    with _login_lock: _login_failures.pop(ip,None)
    session.clear();session.permanent=True
    session['authenticated']=True;session['user']=name;session['csrf_token']=secrets.token_urlsafe(32)
    return redirect(_safe_next(request.form.get('next','/')),code=303)


def _csrf_valid():
    submitted=request.form.get('csrf_token','') or request.headers.get('X-Terminal-CSRF','')
    expected=session.get('csrf_token','')
    return bool(expected and submitted and hmac.compare_digest(submitted,expected))


@app.post('/logout')
def terminal_logout():
    if not _terminal_permitted(): return redirect('/login',code=303)
    if not _csrf_valid(): return Response('Invalid logout request',status=403)
    session.clear()
    response=redirect('/login',code=303)
    response.headers['Cache-Control']='no-store'
    return response


@app.get('/terminal/avatar')
def terminal_avatar_get():
    if not _terminal_permitted():return _terminal_guard()
    try:
        with _db_connect() as con:
            _ensure_schema(con)
            with con.cursor() as cur:
                cur.execute('SELECT image_data,image_type FROM luxalgo_user_avatar WHERE id=1')
                row=cur.fetchone()
        if not row: return Response(status=404)
        return Response(bytes(row['image_data']),mimetype=row['image_type'],headers={'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'})
    except (psycopg.Error,RuntimeError,OSError):return Response(status=503)


@app.post('/terminal/avatar')
def terminal_avatar_post():
    if not _terminal_permitted():return _terminal_guard()
    if not _csrf_valid():return jsonify(error='Invalid request'),403
    photo=request.files.get('photo')
    if not photo:return jsonify(error='Image missing'),400
    raw=photo.read(200001)
    if len(raw)>200000 or len(raw)<40:return jsonify(error='Image must be 40 bytes to 200 KB'),413
    # Only accept recognized image signatures; SVG is not accepted.
    if raw.startswith(b'\x89PNG\r\n\x1a\n'):kind='image/png'
    elif raw.startswith(b'\xff\xd8\xff'):kind='image/jpeg'
    elif raw.startswith(b'RIFF') and raw[8:12]==b'WEBP':kind='image/webp'
    else:return jsonify(error='Unsupported image file'),415
    try:
        with _db_connect() as con:
            _ensure_schema(con)
            with con.cursor() as cur:
                cur.execute('''INSERT INTO luxalgo_user_avatar(id,image_data,image_type)
                    VALUES(1,%s,%s) ON CONFLICT(id) DO UPDATE
                    SET image_data=EXCLUDED.image_data,image_type=EXCLUDED.image_type,updated_at=NOW()''',(raw,kind))
        return jsonify(status='saved')
    except (psycopg.Error,RuntimeError,OSError):return jsonify(error='Photo storage unavailable'),503


@app.get('/terminal/receipt-cursor')
def terminal_receipt_cursor():
    """Authenticated, low-cost cursor for near-real-time signal UI refresh."""
    if not _terminal_permitted():
        return _terminal_guard()
    try:
        with _db_connect() as con:
            with con.cursor() as cur:
                cur.execute('SELECT COALESCE(MAX(id), 0) AS latest_id FROM luxalgo_signal_receipts')
                latest = int(cur.fetchone()['latest_id'])
        response = jsonify(latest_id=latest, orders_enabled=False)
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response
    except (psycopg.Error, RuntimeError, OSError):
        return jsonify(error='Signal receipt cursor temporarily unavailable'), 503


@app.get('/')
def home():
    """Keep the original complete LuxAlgo PRO dashboard at its familiar URL."""
    if not _terminal_permitted():
        return _terminal_guard()
    return _render_legacy_home(production_view=(
        os.getenv('PRODUCTION_TERMINAL_VIEW','false').strip().lower()=='true'))


@app.get('/terminal/legacy-testnet')
def terminal_legacy_testnet():
    """Backward-compatible old-dashboard link; browser login, never raw JSON."""
    if not _terminal_permitted():
        return redirect(url_for('terminal_login', next=request.path),code=303)
    # Keep historical Testnet monitoring available only behind private login
    # until the exchange is truly flat; never dispatch a new Testnet order.
    return _render_legacy_home(production_view=False)


@app.get('/terminal/live-preparation')
def terminal_live_preparation():
    """Optional read-only production preparation view, separate from main UI."""
    if not _terminal_permitted():
        return redirect(url_for('terminal_login', next=request.path),code=303)
    from live_preparation_terminal import LIVE_PREPARATION_PAGE
    response=Response(render_template_string(
        LIVE_PREPARATION_PAGE,csrf_token=session.get('csrf_token','')),
        mimetype='text/html')
    response.headers['Cache-Control']='private, no-store'
    response.headers['X-Frame-Options']='DENY'
    response.headers['X-Content-Type-Options']='nosniff'
    return response


def _render_legacy_home(*,production_view=False):
    try:
        count, records = _db_recent()
        with _db_connect() as con:
            with con.cursor() as cur:
                cur.execute('SELECT COALESCE(MAX(id), 0) AS latest_id FROM luxalgo_signal_receipts')
                latest_receipt_id = int(cur.fetchone()['latest_id'])
    except (psycopg.Error, RuntimeError, OSError):
        app.logger.exception('Persistent signal storage unavailable')
        return Response('Persistent signal database temporarily unavailable. No trading is active.', status=503)
    page=PAGE
    if production_view:
        from production_cutover_view import production_monitor_page
        page=production_monitor_page(PAGE)
    response = Response(render_template_string(page, configured=len(expected_webhook_token()) >= 24,
                                               count=count, records=records,latest_receipt_id=latest_receipt_id,csrf_token=session.get('csrf_token','')), mimetype='text/html')
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@app.get('/terminal/live-preparation/status')
def terminal_live_preparation_status():
    """Production account READ-ONLY readiness; never submits futures orders."""
    if not _terminal_permitted():
        return _terminal_guard()
    from live_preparation_terminal import live_preparation_checks
    account_ok=False; positions_ok=False; receipts_ok=False
    equity=None;available=None;positions=[];received=None;legacy_unresolved=None
    try:
        acc=_gate_futures_account_summary()
        if (acc.get('currency')=='USDT' and acc.get('source')=='GATE_IO_FUTURES_CLASSIC_ACCOUNT'
            and float(acc['equity_usdt'])>0 and float(acc['available_usdt'])>=0):
            account_ok=True
            equity=acc['equity_usdt']
            available=acc['available_usdt']
    except (RuntimeError,HTTPError,URLError,TimeoutError,ValueError,KeyError,TypeError,OSError):
        pass
    try:
        positions=_gate_read_only_positions()
        positions_ok=True
    except (RuntimeError,HTTPError,URLError,TimeoutError,ValueError,KeyError,TypeError,OSError):
        positions=[]
    try:
        with _db_connect() as con:
            with con.cursor() as cur:
                cur.execute('SELECT COUNT(*) AS count FROM luxalgo_signal_receipts')
                received=int(cur.fetchone()['count'])
                receipts_ok=True
                # Production readiness depends on live account and signal records.
                # Legacy Testnet audit is an archive, not a live market-data gate.
    except (psycopg.Error,RuntimeError,OSError,ValueError,KeyError,TypeError):
        legacy_unresolved=None
    prepared=live_preparation_checks(
        account_ok=account_ok,positions_ok=positions_ok,db_ok=receipts_ok,
        webhook_ok=len(expected_webhook_token())>=24,
        legacy_count=legacy_unresolved,
        prod_write_requested=(
            os.getenv('LIVE_TRADING_ENABLED','false').strip().lower()!='false'
            or os.getenv('ORDERS_ENABLED','false').strip().lower()!='false'))
    response=jsonify(**prepared,account_verified=account_ok,
        positions_verified=positions_ok,receipts_verified=receipts_ok,
        equity_usdt=equity,available_usdt=available,
        production_position_count=len(positions) if positions_ok else None,
        production_positions=[dict(contract=p['contract'],side=p['side'],
            size_contracts=p['size_contracts'],entry_price=p['entry_price'],
            mark_price=p['mark_price'],unrealized_pnl=p['unrealized_pnl'])
            for p in positions],
        received_signals=received,
        legacy_testnet_unresolved=legacy_unresolved,
        timestamp=datetime.now(timezone.utc).isoformat())
    response.headers['Cache-Control']='private, no-store'
    response.headers['X-Content-Type-Options']='nosniff'
    return response


@app.post('/terminal/plan')
def terminal_plan():
    if not _terminal_permitted():
        return _terminal_guard()
    if not request.is_json:
        return jsonify(error='JSON required'),400
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error='Invalid request'),400
    symbol = str(data.get('symbol', '')).upper()
    side = str(data.get('side', '')).upper()
    tf = str(data.get('tf', '15m')).strip().lower()
    if tf not in ('5m','15m'):
        return jsonify(error='Unsupported timeframe'),422
    if not re.fullmatch(r'[A-Z0-9]{3,25}USDT',symbol) or side not in ('LONG','SHORT'):
        return jsonify(error='Unsupported pair or direction'),422
    try:
        plan = _gate_atr_plan(symbol,side,tf)
        response = jsonify(plan=plan, orders_enabled=False, live_trading=False)
        response.headers['Cache-Control']='no-store'
        return response
    except (HTTPError,URLError,TimeoutError,ValueError,KeyError,TypeError,OSError):
        return jsonify(error='ATR plan temporarily unavailable'),502


@app.get('/terminal/chart')
def terminal_chart():
    # Private page chart based on public Gate.io candles. No exchange trading API used.
    if not _terminal_permitted():
        return _terminal_guard()
    symbol = request.args.get('symbol','BTCUSDT').strip().upper()
    tf = request.args.get('tf','15m')
    if not re.fullmatch(r'[A-Z0-9]{3,25}USDT',symbol) or tf not in ('5m','15m'):
        return jsonify(error='Unsupported symbol or timeframe'),422
    contract = symbol[:-4] + '_USDT'
    seconds = 300 if tf == '5m' else 900
    url = 'https://api.gateio.ws/api/v4/futures/usdt/candlesticks?' + urlencode(
        ({'contract':contract,'interval':tf,'from':max(0, int(request.args['before'])-400*seconds),'to':int(request.args['before'])-1} if request.args.get('before','').isdigit() else {'contract':contract,'interval':tf,'limit':400}))
    try:
        req = Request(url, headers={'Accept':'application/json','User-Agent':'LuxAlgo-PRO-Terminal/1.0'},method='GET')
        with urlopen(req,timeout=10) as res:
            raw=json.loads(res.read(650000))
        now=int(time.time())
        candles=[]
        if not isinstance(raw,list): raise ValueError('Invalid candle payload')
        for bar in raw:
            t=int(bar['t'])
            if t+seconds>now:continue
            o,h,l,c=(float(bar[k]) for k in ('o','h','l','c'))
            if not all(math.isfinite(v) and v>0 for v in (o,h,l,c)) or h<max(o,c,l) or l>min(o,c):
                raise ValueError('Invalid candle OHLC')
            candles.append(dict(t=t,o=o,h=h,l=l,c=c))
        candles=sorted({b['t']:b for b in candles}.values(), key=lambda x:x['t'])[-400:]
        response=jsonify(symbol=symbol,timeframe=tf,candles=candles,orders_enabled=False)
        response.headers['Cache-Control']='private, max-age=10'
        return response
    except (HTTPError,URLError,TimeoutError,ValueError,KeyError,TypeError,OSError):
        return jsonify(error='Public candle data temporarily unavailable'),502


@app.get('/terminal/production-bot-control')
def terminal_production_bot_state():
    """Read actual database/source-lock/authorization state, never Testnet toggle."""
    if not _terminal_permitted():return _terminal_guard()
    try:
        from production_bot_control import state
        with _db_connect() as con:
            result=state(con)
        response=jsonify(result)
        response.headers['Cache-Control']='private, no-store'
        return response
    except (psycopg.Error,RuntimeError,OSError,ValueError,TypeError,KeyError):
        return jsonify(bot_on=False,orders_enabled=False,
                       error='Server-side execution state unavailable',
                       reason='UNVERIFIED_HALT',safety_gates=['STATE_UNAVAILABLE']),503


@app.post('/terminal/production-bot-control')
def terminal_production_bot_update():
    """Operator OFF halts future entries; ON requires independently released gates."""
    if not _terminal_permitted():return _terminal_guard()
    if not _csrf_valid():return jsonify(error='Invalid CSRF',bot_on=False),403
    request_data=request.get_json(silent=True) if request.is_json else None
    if not isinstance(request_data,dict) or type(request_data.get('enabled')) is not bool:
        return jsonify(error='Boolean enabled required',bot_on=False),400
    try:
        from production_bot_control import turn_off,turn_on
        desired=request_data['enabled']
        with _db_connect() as con:
            result=turn_on(con) if desired else turn_off(con)
        code=200 if (not desired or result['bot_on']) else 409
        response=jsonify(result)
        response.status_code=code
        response.headers['Cache-Control']='private, no-store'
        return response
    except (psycopg.Error,RuntimeError,OSError,ValueError,TypeError,KeyError):
        app.logger.warning('Production operator switch failed closed',exc_info=False)
        return jsonify(bot_on=False,orders_enabled=False,
                       error='Operator state could not be persisted; treating as HALT'),503


@app.get('/terminal/live-execution/snapshot')
def terminal_live_execution_snapshot():
    """Authenticated production GET reconciliation; absolutely no order writes."""
    if not _terminal_permitted():
        return _terminal_guard()
    try:
        from live_execution_coordinator import reconcile_and_halt_if_needed
        with _db_connect() as con:
            status=reconcile_and_halt_if_needed(con)
        response=jsonify(status)
        response.headers['Cache-Control']='private, no-store'
        return response
    except (psycopg.Error,RuntimeError,OSError,ValueError,TypeError,KeyError):
        app.logger.warning('Production exchange reconciliation unavailable',exc_info=False)
        from live_execution_coordinator import status_fallback
        return jsonify(status_fallback('ACCOUNT_OR_DB_READBACK_FAILED')),503


@app.post('/terminal/live-execution/emergency-halt')
def terminal_live_execution_halt():
    """Durable halt only. Does not pretend to close exchange positions."""
    if not _terminal_permitted():return _terminal_guard()
    if not _csrf_valid():return jsonify(error='Invalid session CSRF'),403
    try:
        from live_execution_coordinator import install,halt
        with _db_connect() as con:
            install(con)
            result=halt(con,'OPERATOR_PERSISTENT_HALT')
        return jsonify(result),200
    except (psycopg.Error,RuntimeError,OSError,ValueError,TypeError):
        app.logger.error('Production emergency halt database unavailable')
        return jsonify(error='HALT_CONFIRMATION_UNKNOWN',orders_enabled=False),503


@app.post('/terminal/live-execution/plan')
def terminal_live_execution_plan():
    """Validated real signal + current exchange inventory -> PREVIEW, not order."""
    if not _terminal_permitted():return _terminal_guard()
    if not _csrf_valid():return jsonify(error='Invalid session CSRF'),403
    body=request.get_json(silent=True) if request.is_json else None
    event_id=body.get('event_id') if isinstance(body,dict) else None
    if not isinstance(event_id,str) or not re.fullmatch(r'[a-f0-9]{20}',event_id):
        return jsonify(error='Valid stored event ID required'),422
    try:
        from live_futures_engine import GateLiveClient,build_plan,LiveSafetyError,verified_account_equity
        from live_execution_coordinator import install,control,read_rows
        with _db_connect() as con:
            _ensure_schema(con);install(con)
            if control(con)['halted']:
                return jsonify(error='Persistent execution HALT is active',
                               orders_enabled=False),409
            with con.cursor() as cur:
                cur.execute("""SELECT r.event_id,r.received_at,r.symbol,r.side,r.timeframe,
                       l.entry_price,l.stop_loss,l.take_profit,l.status AS levels_status,
                       t.order_type
                       FROM luxalgo_signal_receipts r
                       JOIN luxalgo_signal_levels l ON l.event_id=r.event_id
                       LEFT JOIN luxalgo_signal_order_types t ON t.event_id=r.event_id
                       WHERE r.event_id=%s ORDER BY r.received_at DESC LIMIT 1""",(event_id,))
                row=cur.fetchone()
            ledger=read_rows(con)
        if not row or row['order_type'] not in ('MARKET','LIMIT'):
            return jsonify(error='Signal or declared order type not verified',
                           orders_enabled=False),422
        client=GateLiveClient()
        snap=client.snapshot()
        contract=row['symbol'].removesuffix('USDT')+'_USDT'
        meta=client.request('GET','/api/v4/futures/usdt/contracts/'+contract)
        if not isinstance(meta,dict):
            raise LiveSafetyError('Contract metadata invalid')
        equity,available=verified_account_equity(snap['account'])
        reserved=sum(Decimal(str(x['risk_usdt'])) for x in ledger
                     if x['state']!='CLOSED_VERIFIED')
        plan=build_plan(dict(event_id=event_id,side=row['side'],contract=contract,
                    timeframe=row['timeframe'],levels_status=row['levels_status'],
                    entry=row['entry_price'],sl=row['stop_loss'],tp=row['take_profit'],
                    received_epoch=int(row['received_at'].timestamp()),
                    position_mode=snap['account'].get('position_mode'),leverage=5),
                    meta,equity,available,snap['positions'],reserved_risk=reserved)
        return jsonify(status='PREVIEW_ONLY_NOT_EXECUTABLE',plan=plan,
                       orders_enabled=False,live_trading=False),200
    except (psycopg.Error,RuntimeError,OSError,ValueError,TypeError,KeyError,
            InvalidOperation,OverflowError):
        return jsonify(error='Contract/risk/live account verification failed closed',
                       orders_enabled=False),503


@app.get('/health')
def health():
    """Lightweight process liveness; never represents trade readiness."""
    response = jsonify(status='ok', live_trading=False, orders_enabled=False,
                       kind='signal_receiver_with_readonly_exchange_checks')
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.get('/ready')
def ready():
    """Read-only PostgreSQL connectivity check. Never authorizes exchange writes."""
    try:
        with _db_connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute('''SELECT
                    to_regclass('public.luxalgo_signal_receipts') IS NOT NULL AS receipts,
                    to_regclass('public.luxalgo_dry_run_intents') IS NOT NULL AS intents,
                    to_regclass('public.luxalgo_bot_control') IS NOT NULL AS control,
                    to_regclass('public.luxalgo_stage6_execution_halt') IS NOT NULL AS halt''')
                row = cursor.fetchone()
                if not row or not all(row.values()):
                    raise RuntimeError('Required safety tables are missing')
    except (psycopg.Error, RuntimeError, OSError):
        response = jsonify(status='not_ready', database='unavailable',
                           live_trading=False, orders_enabled=False)
        response.headers['Cache-Control'] = 'no-store'
        return response, 503
    response = jsonify(status='ready', database='connected',
                       live_trading=False, orders_enabled=False)
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.post('/bot/status')
def bot_status():
    """Authenticated DRY RUN monitor; no order execution or sensitive account data."""
    data = request.get_json(silent=True) if request.is_json else None
    if not isinstance(data, dict) or not authorized(data):
        return jsonify(error='Unauthorized'), 401
    try:
        signal_count, recent = _db_status()
    except (psycopg.Error, RuntimeError, OSError):
        app.logger.exception('Persistent signal database unavailable')
        return jsonify(error='Persistent signal database unavailable', orders_enabled=False),503
    return jsonify(mode='DRY_RUN_ONLY', live_trading=False,
                   orders_enabled=False, exchange='Gate.io',
                   persistent_signal_receipts=signal_count,
                   dry_run_intents=recent,
                   persistent_history=True,
                   warning='No positions, order submissions, fills, SL or TP orders exist.')


@app.post('/terminal/hide-recent-signal')
def terminal_hide_recent_signal():
    if not _terminal_permitted(): return _terminal_guard()
    if not _csrf_valid(): return jsonify(error='Invalid session verification'),403
    data=request.get_json(silent=True) if request.is_json else None
    event_id=data.get('event_id') if isinstance(data,dict) else None
    if not isinstance(event_id,str) or not re.fullmatch(r'[A-Fa-f0-9]{20}',event_id):
        return jsonify(error='Invalid signal ID'),400
    try:
        with _db_connect() as con:
            _ensure_schema(con)
            with con.cursor() as cur:
                cur.execute('SELECT 1 FROM luxalgo_signal_receipts WHERE event_id=%s LIMIT 1',(event_id,))
                if cur.fetchone() is None:return jsonify(error='Signal not found'),404
                cur.execute('INSERT INTO luxalgo_hidden_signal_rows(event_id) VALUES (%s) ON CONFLICT DO NOTHING',(event_id,))
        response=jsonify(hidden=True,audit_retained=True,orders_placed=0)
        response.headers['Cache-Control']='no-store'
        return response
    except (psycopg.Error,RuntimeError,OSError):
        return jsonify(error='Signal history database unavailable'),503

def _repair_wrapped_token_newlines(text):
    """Repair one known TradingView formatting defect, never arbitrary malformed JSON.

    Some alert templates insert physical newlines within an alphanumeric token
    and, rarely, omit the token's closing quote. Only repair that field when
    it matches the *configured* secret after whitespace removal. All other
    fields must still parse as normal JSON and pass existing validation.
    """
    expected = expected_webhook_token()
    if len(expected) < 24 or not re.fullmatch(r'[A-Za-z0-9]+', expected):
        return text

    # Match only the token field directly followed by another recognized key.
    # A missing closing quote is tolerated ONLY with the configured secret.
    pattern = (r'("token"\s*:\s*")([A-Za-z0-9\s]+)("?)\s*'
               r'(,)(?=\s*"(?:source|alert|symbol|ticker|side|tf|entry|sl|tp1)"\s*:)')

    def repair(match):
        candidate = re.sub(r'\s+', '', match.group(2))
        if not hmac.compare_digest(candidate, expected):
            return match.group(0)
        return match.group(1) + expected + '"' + match.group(4)

    return re.sub(pattern, repair, text, count=1)


def _parse_luxalgo_webhook_body(raw):
    """Parse a genuine JSON object, including TradingView/LuxAlgo quote wrappers.

    No authentication takes place here. Parsing NEVER implies permission to trade.
    Do not log message contents: the JSON contains a webhook secret.
    """
    if not isinstance(raw, bytes) or not raw or len(raw) > 16000:
        return None
    try:
        text = raw.decode('utf-8-sig').strip()
    except UnicodeDecodeError:
        return None
    if not text:
        return None

    # Apply a restricted, authenticated repair for a line-wrapped token.
    # This does not accept malformed data elsewhere in the payload.
    text = _repair_wrapped_token_newlines(text)

    # Most TradingView alert() messages are ordinary JSON text. Some providers
    # wrap the WHOLE message in an extra pair of quotes, escaped or unescaped.
    # Decode at most 3 levels; require the final result to be a JSON object.
    for _ in range(3):
        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            obj = None
        if isinstance(obj, dict):
            return obj
        if isinstance(obj, str):
            text = obj.strip()
            continue
        if len(text) >= 4 and text.startswith('"') and text.endswith('"'):
            inner = text[1:-1].strip()
            if inner.startswith('{') and inner.endswith('}'):
                text = _repair_wrapped_token_newlines(inner)
                continue
        break
    return None


def _webhook_format_error(raw):
    """Report JSON syntax location only. Never log the body, secret, or fragments."""
    if not raw:
        return 'empty_body'
    if len(raw) > 16000:
        return 'body_too_large'
    try:
        value = raw.decode('utf-8-sig').strip()
    except UnicodeDecodeError:
        return 'invalid_encoding'
    if not value:
        return 'empty_body'
    # Try valid outer-string and literal quoted wrappers, matching the parser.
    for _ in range(3):
        try:
            obj = json.loads(value)
        except json.JSONDecodeError as error:
            # If the complete object has been enclosed by raw quotes,
            # inspect the interior before reporting an error position.
            if (value.startswith('"') and value.endswith('"') and
                    value[1:-1].strip().startswith('{')):
                value = value[1:-1].strip()
                continue
            # Error type and location are safe; message content is not logged.
            safe_type = ('control_char' if 'Invalid control character' in error.msg
                         else 'json_syntax')
            return f'{safe_type}_line{error.lineno}_col{error.colno}_pos{error.pos}'
        except (ValueError, TypeError):
            return 'json_decode_failure'
        if isinstance(obj, dict):
            return 'unexpected_parse_failure'
        if isinstance(obj, str):
            value = obj.strip()
            continue
        return 'json_value_not_object'
    return 'wrapper_not_decodable'


@app.post('/webhook')
def webhook():
    if len(expected_webhook_token()) < 24:
        return jsonify(error='Webhook token not configured'), 503
    # TradingView alert() may use text/plain even when its message is JSON.
    # Never accept invalid JSON, and NEVER bypass authentication.
    raw = request.get_data(cache=False, as_text=False)
    data = _parse_luxalgo_webhook_body(raw)
    if not isinstance(data, dict):
        reason = _webhook_format_error(raw)
        app.logger.warning('LuxAlgo webhook rejected: invalid_format=%s content_type=%s bytes=%d',
                           reason, (request.mimetype or 'unknown')[:60], len(raw))
        return jsonify(error='JSON object required', format_error=reason, orders_placed=0), 400
    if not authorized(data):
        return jsonify(error='Unauthorized'), 401
    symbol = str(data.get('symbol', data.get('ticker', ''))).upper().removeprefix('BINANCE:').removesuffix('.P')
    side = str(data.get('side', data.get('alert', ''))).strip().upper()
    tf = str(data.get('tf', '')).strip().lower()
    allowed = {'BUY': 'LONG', 'LONG': 'LONG', 'BULLISH CONFIRMATION': 'LONG', 'SELL': 'SHORT', 'SHORT': 'SHORT', 'BEARISH CONFIRMATION': 'SHORT'}
    if not re.fullmatch(r'[A-Z0-9]{3,25}USDT', symbol) or side not in allowed or tf not in ('5', '5m', '5min', '5 minutes', '15', '15m', '15min', '15 minutes'):
        return jsonify(error='Invalid symbol, side, or timeframe'), 422
    received_at = datetime.now(timezone.utc)
    tf = '5m' if tf.startswith('5') else '15m'
    # Missing/invalid LuxAlgo prices never authorize an exchange order.
    # Preserve legacy signal reception but flag missing/invalid levels in audit.
    from decimal import Decimal, InvalidOperation
    supplied = [data.get(k) for k in ('entry','sl','tp1')]
    parsed=[]
    for value in supplied:
        try:
            n=Decimal(str(value)) if value is not None and not isinstance(value,bool) else None
            if n is None or not n.is_finite() or n<=0: n=None
        except (InvalidOperation, ValueError, TypeError):n=None
        parsed.append(n)
    entry_price, stop_loss, take_profit=parsed
    if any(v is None for v in supplied):
        level_status='MISSING';level_reason='Missing explicit LuxAlgo entry, SL1 or TP1'
    elif any(v is None for v in parsed):
        level_status='INVALID';level_reason='Non-numeric, unresolved, or non-positive LuxAlgo level'
    elif ((allowed[side]=='LONG' and stop_loss<entry_price<take_profit) or
          (allowed[side]=='SHORT' and take_profit<entry_price<stop_loss)):
        level_status='VALID';level_reason='Direction and numeric levels checked; exchange protection still unavailable'
    else:
        level_status='INVALID';level_reason='Stop loss and take profit are on the wrong sides of entry'
    declared_order_type = str(data.get('order_type', data.get('orderType', ''))).strip().upper()
    if declared_order_type not in ('MARKET','LIMIT'):
        declared_order_type = None
    record = {'dt':received_at,'symbol':symbol,'side':allowed[side],'tf':tf}
    # A signed, authenticated webhook can optionally supply the stable
    # TradingView candle ID. Retries of the same bar produce the same event ID.
    # Old alerts remain accepted but remain permanently SIGNAL_ONLY.
    from live_signal_identity import stable_signal_identity
    event_id,source_bar_id=stable_signal_identity(
        symbol,allowed[side],tf,received_at,data.get('tv_bar_id'))
    try:
        duplicate = _db_record(record,event_id)
        with _db_connect() as con:
            _ensure_schema(con)
            con.execute("""INSERT INTO luxalgo_signal_levels(event_id,status,entry_price,stop_loss,take_profit,reason)
                VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (event_id) DO NOTHING""",
                (event_id,level_status,entry_price,stop_loss,take_profit,level_reason))
            if declared_order_type:
                con.execute('''INSERT INTO luxalgo_signal_order_types(event_id,order_type)
                    VALUES (%s,%s) ON CONFLICT (event_id) DO NOTHING''',
                    (event_id,declared_order_type))
    except (psycopg.Error,RuntimeError,OSError):
        app.logger.exception('Cannot durably record incoming LuxAlgo signal')
        return jsonify(error='Signal not stored; retry later',orders_placed=0),503
    # LIVE production signal-only audit, independent of Testnet credentials.
    # A stable, unique TradingView bar ID is mandatory for any future live order.
    # Existing LuxAlgo alerts without this field remain logged, not executable.
    try:
        with _db_connect() as con:
            _ensure_schema(con)
            con.execute("""INSERT INTO luxalgo_live_signal_handoff
                (event_id,received_at,symbol,side,timeframe,order_type,level_status,source_bar_id,status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'SIGNAL_ONLY')
                ON CONFLICT DO NOTHING""",
                (event_id,received_at,symbol,allowed[side],tf,declared_order_type,level_status,
                 source_bar_id))
    except (psycopg.Error,RuntimeError,OSError):
        app.logger.exception('Production signal handoff audit unavailable')
        return jsonify(error='Signal stored but production handoff audit unavailable',orders_placed=0),503
    # One-trade handoff: durable first-candidate reservation ONLY. Not an order.
    # Preserves existing webhook receipt behavior and does not call exchange APIs.
    one_trade_candidate = 'NOT_ELIGIBLE'
    if level_status == 'VALID' and not duplicate and tf == '15m':
        try:
            with _db_connect() as con:
                _ensure_schema(con)
                with con.cursor() as cur:
                    cur.execute("""INSERT INTO luxalgo_one_trade_candidate
                        (singleton_id,event_id,symbol,side,timeframe,entry_price,stop_loss,take_profit,status)
                        VALUES (1,%s,%s,%s,%s,%s,%s,%s,'WAITING_FOR_VERIFIED_EXECUTION')
                        ON CONFLICT (singleton_id) DO NOTHING RETURNING event_id""",
                        (event_id,symbol,allowed[side],tf,entry_price,stop_loss,take_profit))
                    one_trade_candidate = ('RESERVED_NO_ORDER' if cur.fetchone() else 'ALREADY_RESERVED')
        except (psycopg.Error, RuntimeError, OSError):
            app.logger.warning('One-trade candidate reservation failed; no execution permitted')
            one_trade_candidate = 'UNAVAILABLE_NO_ORDER'
    # Future production worker cannot run without explicit source code release,
    # permission, and authenticated, stable TradingView bar-level signal.
    if (not duplicate and level_status=='VALID' and source_bar_id and
        declared_order_type=='MARKET' and
        os.getenv('LIVE_TRADING_ENABLED','false')=='true' and
        os.getenv('ORDERS_ENABLED','false')=='true' and
        os.getenv('LIVE_ORDERS_SEPARATELY_AUTHORIZED','false')=='true'):
        try:
            from live_futures_engine import EXCHANGE_WRITE_RELEASE
            from live_execution_orchestrator import AUTOMATIC_WORKER_RELEASE
            if EXCHANGE_WRITE_RELEASE and AUTOMATIC_WORKER_RELEASE:
                _live_auto_executor.submit(_process_live_webhook_once,event_id)
        except (RuntimeError,OSError,ValueError):
            app.logger.warning('Production execution worker unavailable; signal retained')
    return jsonify(status='signal_received', mode='SIGNAL_ONLY', symbol=symbol,
                   side=allowed[side], timeframe=tf, duplicate_in_candle=duplicate,
                   dry_run_intent_id=event_id, level_status=level_status,
                   one_trade_candidate=one_trade_candidate, orders_placed=0), 200


def binance_read_only_ping(api_key, api_secret):
    # GET only: never execute any exchange orders.
    params = urlencode({'timestamp': int(time.time() * 1000), 'recvWindow': 5000})
    signature = hmac.new(api_secret.encode('utf-8'), params.encode('utf-8'), hashlib.sha256).hexdigest()
    url = 'https://fapi.binance.com/fapi/v2/account?' + params + '&signature=' + signature
    req = Request(url, headers={'X-MBX-APIKEY': api_key, 'User-Agent': 'LuxAlgo-PRO-Readonly/1.0'}, method='GET')
    with urlopen(req, timeout=8) as res:
        doc = json.loads(res.read(65536))
    if not isinstance(doc, dict) or 'canTrade' not in doc:
        raise ValueError('Unexpected Binance API response')
    return True


@app.post('/binance/check')
def binance_check():
    # Auth check occurs before any exchange/network request.
    if len(expected_webhook_token()) < 24:
        return jsonify(error='Authentication not configured'), 503
    data = request.get_json(silent=True) if request.is_json else None
    if not isinstance(data, dict) or not authorized(data):
        return jsonify(error='Unauthorized'), 401
    api_key = os.getenv('BINANCE_API_KEY', '').strip()
    api_secret = os.getenv('BINANCE_API_SECRET', '').strip()
    if not api_key or not api_secret:
        return jsonify(status='credentials_not_configured', orders_enabled=False), 503
    try:
        binance_read_only_ping(api_key, api_secret)
        return jsonify(status='binance_read_only_connection_ok', orders_enabled=False, live_trading=False), 200
    except HTTPError as exc:
        # Do not expose account data, raw Binance payloads, or secret values.
        return jsonify(status='binance_auth_or_access_failed', binance_http_status=exc.code, orders_enabled=False), 502
    except (URLError, TimeoutError, ValueError, OSError) as exc:
        app.logger.warning('Binance read-only connectivity failed: %s', type(exc).__name__)
        return jsonify(status='binance_unreachable_or_invalid_response', orders_enabled=False), 502


def gate_read_only_ping(api_key, api_secret):
    """Gate.io APIv4 authenticated GET only; do not expose account information."""
    method = 'GET'
    path = '/api/v4/futures/usdt/accounts'
    timestamp = str(int(time.time()))
    payload_hash = hashlib.sha512(b'').hexdigest()
    message = '\n'.join([method, path, '', payload_hash, timestamp])
    signature = hmac.new(api_secret.encode('utf-8'), message.encode('utf-8'), hashlib.sha512).hexdigest()
    req = Request(
        'https://api.gateio.ws' + path,
        headers={
            'KEY': api_key,
            'Timestamp': timestamp,
            'SIGN': signature,
            'Accept': 'application/json',
            'User-Agent': 'LuxAlgo-PRO-Readonly/1.0',
        },
        method='GET',
    )
    with urlopen(req, timeout=10) as res:
        doc = json.loads(res.read(65536))
    if not isinstance(doc, dict) or 'currency' not in doc:
        raise ValueError('Unexpected Gate.io Futures account response')
    return True


@app.post('/gate/check')
def gate_check():
    if len(expected_webhook_token()) < 24:
        return jsonify(error='Authentication not configured'), 503
    data = request.get_json(silent=True) if request.is_json else None
    if not isinstance(data, dict) or not authorized(data):
        return jsonify(error='Unauthorized'), 401
    api_key = os.getenv('GATE_API_KEY', '').strip()
    api_secret = os.getenv('GATE_API_SECRET', '').strip()
    if not api_key or not api_secret:
        return jsonify(status='gate_credentials_not_configured', orders_enabled=False), 503
    try:
        gate_read_only_ping(api_key, api_secret)
        return jsonify(status='gate_read_only_connection_ok', orders_enabled=False, live_trading=False), 200
    except HTTPError as exc:
        return jsonify(status='gate_auth_or_access_failed', gate_http_status=exc.code, orders_enabled=False), 502
    except (URLError, TimeoutError, ValueError, OSError) as exc:
        app.logger.warning('Gate.io read-only connectivity failed: %s', type(exc).__name__)
        return jsonify(status='gate_unreachable_or_invalid_response', orders_enabled=False), 502




@app.get('/terminal/gate-testnet-check')
def terminal_gate_testnet_check():
    """Private authenticated Testnet readback; NO real or simulated order writes."""
    if not _terminal_permitted():
        return _terminal_guard()
    key=os.getenv('GATE_TESTNET_API_KEY','').strip()
    secret=os.getenv('GATE_TESTNET_API_SECRET','').strip()
    if not key or not secret:
        return jsonify(testnet_connected=False,reason='TESTNET_CREDENTIALS_MISSING',
                       live_trading=False,orders_enabled=False),503
    path='/api/v4/futures/usdt/accounts'
    stamp=str(int(time.time()))
    digest=hashlib.sha512(b'').hexdigest()
    signing='\n'.join(('GET',path,'',digest,stamp))
    signature=hmac.new(secret.encode('utf-8'),signing.encode('utf-8'),hashlib.sha512).hexdigest()
    req=Request('https://api-testnet.gateapi.io'+path,method='GET',headers={
        'KEY':key,'Timestamp':stamp,'SIGN':signature,
        'Accept':'application/json','User-Agent':'LuxAlgo-PRO-Testnet-Checker/1.0'})
    try:
        with urlopen(req,timeout=10) as res:
            account=json.loads(res.read(65536))
        if not isinstance(account,dict) or account.get('currency')!='USDT':
            return jsonify(testnet_connected=False,reason='UNEXPECTED_TESTNET_RESPONSE',
                           live_trading=False,orders_enabled=False),502
        result=jsonify(testnet_connected=True,account_currency='USDT',
                       environment='GATE_FUTURES_TESTNET',read_only=True,
                       api_permissions='READ_VERIFIED_WRITE_NOT_TESTED',
                       write_permission_verified=False,
                       connection_mode='READ_ONLY_CHECK',
                       live_trading=False,orders_enabled=False)
        result.headers['Cache-Control']='no-store'
        return result
    except HTTPError as exc:
        return jsonify(testnet_connected=False,reason='TESTNET_HTTP_REJECTED',
                       http_status=exc.code,live_trading=False,orders_enabled=False),502
    except (URLError,TimeoutError,OSError,ValueError,TypeError):
        return jsonify(testnet_connected=False,reason='TESTNET_READBACK_FAILED',
                       live_trading=False,orders_enabled=False),502


def _gate_read_only_positions(testnet=False):
    """Authenticated GET-only position snapshot from explicitly selected environment."""
    prefix = 'GATE_TESTNET' if testnet else 'GATE'
    key = os.getenv(prefix+'_API_KEY', '').strip()
    secret = os.getenv(prefix+'_API_SECRET', '').strip()
    if not key or not secret:
        raise RuntimeError('Gate.io API credentials not configured')
    path = '/api/v4/futures/usdt/positions'
    timestamp = str(int(time.time()))
    payload_hash = hashlib.sha512(b'').hexdigest()
    message = '\n'.join(('GET', path, '', payload_hash, timestamp))
    signature = hmac.new(secret.encode(), message.encode(), hashlib.sha512).hexdigest()
    req = Request(('https://api-testnet.gateapi.io' if testnet else 'https://api.gateio.ws') + path, method='GET', headers={
        'KEY': key, 'Timestamp': timestamp, 'SIGN': signature,
        'Accept': 'application/json', 'User-Agent': 'LuxAlgo-PRO-PositionMonitor/1.0',
    })
    with urlopen(req, timeout=10) as res:
        raw = json.loads(res.read(400000))
    if not isinstance(raw, list):
        raise ValueError('Unexpected positions response')
    def number(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError('Non-finite position number')
        return result
    positions = []
    for p in raw:
        if not isinstance(p, dict):
            raise ValueError('Unexpected position')
        # Gate may return a null size for an inactive/empty position row.
        # Do not let that unrelated empty row hide valid open positions.
        raw_size=p.get('size')
        if raw_size is None or raw_size=='':
            continue
        size=number(raw_size)
        if not size:
            continue
        contract = p.get('contract')
        if not isinstance(contract, str) or not re.fullmatch(r'[A-Z0-9]{2,28}_USDT', contract):
            raise ValueError('Unexpected contract symbol')
        mode = p.get('mode', 'single')
        if mode not in ('single', 'dual_long', 'dual_short'):
            raise ValueError('Unexpected position mode')
        side = ('LONG' if mode == 'dual_long' else 'SHORT' if mode == 'dual_short'
                else 'LONG' if size > 0 else 'SHORT')
        # Some position modes return null or absent mark/PnL values.
        # Preserve a genuine open position while labeling unverifiable
        # financial fields as unavailable; never invent a zero PnL.
        def optional_number(name):
            value=p.get(name)
            return None if value is None or value=='' else number(value)
        entry=optional_number('entry_price')
        mark=optional_number('mark_price')
        unrealized=optional_number('unrealised_pnl')
        if entry is not None and entry<=0:
            entry=None
        if mark is not None and mark<=0:
            mark=None
        margin_mode = str(p.get('pos_margin_mode') or '').lower()
        leverage = str(p.get('lever') or '').strip()
        if not leverage or leverage == '0':
            leverage = str(p.get('cross_leverage_limit') if margin_mode == 'cross' else p.get('leverage') or '0')
        leverage_label = (leverage + '×' if leverage not in ('0','None','') else '—')
        if margin_mode in ('cross','isolated'):
            leverage_label += ' · ' + margin_mode.title()
        # Gate.io positions may omit some values by account mode: report unavailable, never guess.
        def optional_nonnegative(*names):
            for name in names:
                value = p.get(name)
                if value is not None and value != '':
                    n = number(value)
                    return abs(n)
            return None
        margin = optional_nonnegative('margin')
        position_value = optional_nonnegative('value')
        liquidation = optional_nonnegative('liq_price')
        roi = (round(unrealized / margin * 100, 4)
               if unrealized is not None and margin is not None and margin > 0 else None)
        positions.append(dict(contract=contract, side=side, size_contracts=abs(size),
                              entry_price=entry, mark_price=mark, unrealized_pnl=unrealized,
                              leverage_label=leverage_label, position_mode=mode,
                              margin_usdt=margin, position_value_usdt=position_value,
                              liquidation_price=liquidation, roi_percent=roi))
    return positions


def _gate_futures_account_summary():
    """Authenticated GET only; no credentials or private account IDs in response."""
    key = os.getenv('GATE_API_KEY', '').strip()
    secret = os.getenv('GATE_API_SECRET', '').strip()
    if not key or not secret:
        raise RuntimeError('Gate.io credentials unavailable')
    path = '/api/v4/futures/usdt/accounts'
    timestamp = str(int(time.time()))
    payload_hash = hashlib.sha512(b'').hexdigest()
    message = '\n'.join(('GET', path, '', payload_hash, timestamp))
    signature = hmac.new(secret.encode(), message.encode(), hashlib.sha512).hexdigest()
    req = Request('https://api.gateio.ws' + path, method='GET', headers={
        'KEY':key, 'Timestamp':timestamp, 'SIGN':signature,
        'Accept':'application/json', 'User-Agent':'LuxAlgo-PRO-EquityMonitor/1.0'
    })
    with urlopen(req, timeout=10) as res:
        raw = json.loads(res.read(65536))
    if not isinstance(raw, dict) or raw.get('currency') != 'USDT':
        raise ValueError('Invalid USDT futures account')
    # Gate documents /futures/usdt/accounts total as classic account cash balance
    # (excluding unrealized profit/loss); classic equity = total + unrealised_pnl.
    # Other account modes require a separate unified account equity integration.
    if raw.get('margin_mode', 0) not in (0, '0', None):
        raise ValueError('Unified account mode: equity mapping not yet supported')
    def amount(key):
        value = raw.get(key)
        if value is None or isinstance(value, bool):
            raise ValueError('Missing futures account field')
        number = float(value)
        if not math.isfinite(number):
            raise ValueError('Invalid futures account field')
        return round(number, 8)
    balance = amount('total')
    upl = amount('unrealised_pnl')
    available = amount('available')
    return dict(balance_usdt=balance, available_usdt=available,
                unrealized_pnl_usdt=upl, equity_usdt=round(balance+upl,8),
                currency='USDT', source='GATE_IO_FUTURES_CLASSIC_ACCOUNT',
                fetched_at=int(time.time()), orders_enabled=False,live_trading=False)


# LIVE-EXECUTION READINESS: observational fail-closed inspection only.
# This endpoint never sends an order, changes permissions, or enables trading.

# STEP 413: Only audits a BLOCKED live-order candidate. Absolutely no exchange writes.
# A real execution ledger will require explicit exchange client-order IDs and reconciliation.
@app.post('/terminal/live-order-guard-test')
def terminal_live_order_guard_test():
    if not _terminal_permitted():
        return _terminal_guard()
    if not _csrf_valid():
        return jsonify(error='Invalid session verification', orders_placed=0), 403
    if not request.is_json:
        return jsonify(error='JSON required', orders_placed=0), 400
    data=request.get_json(silent=True)
    if not isinstance(data,dict):
        return jsonify(error='Invalid request', orders_placed=0),400
    event_id=data.get('event_id')
    if not isinstance(event_id,str) or not (1 <= len(event_id) <= 128) or not re.fullmatch(r'[A-Za-z0-9_.:-]+',event_id):
        return jsonify(error='Valid stored signal ID required',orders_placed=0),400
    try:
        with _db_connect() as con:
            _ensure_schema(con)
            with con.cursor() as cur:
                # Only authenticated previously received events may be guard-tested.
                cur.execute('SELECT 1 FROM luxalgo_dry_run_intents WHERE event_id=%s',(event_id,))
                if cur.fetchone() is None:
                    return jsonify(error='Signal event not found; guard rejected',orders_placed=0),404
                cur.execute('''INSERT INTO luxalgo_live_order_guard_audit(event_id,reason)
                    VALUES (%s,%s) ON CONFLICT(event_id) DO NOTHING''',
                    (event_id,'Live execution not implemented; exchange SL/TP, reconciliation, risk lock and always-on host unverified'))
                duplicate = cur.rowcount == 0
                cur.execute('SELECT checked_at,decision,reason FROM luxalgo_live_order_guard_audit WHERE event_id=%s',(event_id,))
                row=cur.fetchone()
        response=jsonify(status='BLOCKED',duplicate=duplicate,event_id=event_id,
                         decision=row['decision'],reason=row['reason'],checked_at=row['checked_at'].isoformat(),
                         exchange_requests=0,orders_placed=0,live_trading=False)
        response.headers['Cache-Control']='no-store'
        return response
    except (psycopg.Error,RuntimeError,OSError):
        app.logger.exception('Live guard audit unavailable')
        return jsonify(error='Live guard database unavailable: blocked',orders_placed=0),503

# STAGE 1 — purely computational candidate validation. NO EXCHANGE WRITES.
# Deliberately isolated from the existing /webhook route and all paper queues.
def _live_candidate_dry_run(payload):
    from decimal import Decimal, InvalidOperation
    if not isinstance(payload, dict):
        return {'status': 'BLOCKED', 'reason': 'JSON object required', 'orders_placed': 0}
    symbol = str(payload.get('symbol', '')).strip().upper().removeprefix('GATE:').removesuffix('.P')
    side = str(payload.get('side', '')).strip().upper()
    tf = str(payload.get('tf', '')).strip().lower()
    if not re.fullmatch(r'[A-Z0-9]{3,25}USDT', symbol) or side not in ('LONG','SHORT') or tf not in ('5','5m','15','15m'):
        return {'status': 'BLOCKED', 'reason': 'Invalid symbol, direction or timeframe', 'orders_placed': 0}
    try:
        def positive(name):
            value = payload.get(name)
            if isinstance(value, bool) or value is None:
                raise ValueError(name)
            n = Decimal(str(value))
            if not n.is_finite() or n <= 0:
                raise ValueError(name)
            return n
        entry, stop, tp, equity = [positive(k) for k in ('entry','sl','tp1','equity_usdt')]
        risk_percent = Decimal(str(payload.get('risk_percent', '0.5')))
        if not risk_percent.is_finite() or risk_percent <= 0 or risk_percent > Decimal('0.5'):
            raise ValueError('risk_percent')
        if equity > Decimal('1000000000'):
            raise ValueError('equity_usdt')
        if side == 'LONG' and not (stop < entry < tp):
            raise ValueError('Invalid LONG stop/target order')
        if side == 'SHORT' and not (tp < entry < stop):
            raise ValueError('Invalid SHORT stop/target order')
        stop_gap = abs(entry-stop)
        risk_budget = equity * risk_percent / Decimal('100')
        base_asset_quantity = risk_budget / stop_gap
        indicative_notional = base_asset_quantity * entry
        if not all(v.is_finite() for v in (risk_budget,base_asset_quantity,indicative_notional)):
            raise ValueError('Nonfinite calculation')
    except (ValueError, TypeError, InvalidOperation, OverflowError, ZeroDivisionError):
        return {'status': 'BLOCKED', 'reason': 'Invalid prices, equity or risk; no quote/size inferred', 'orders_placed': 0}
    return {
        'status': 'DRY_RUN_ONLY', 'safe_to_trade': False, 'orders_enabled': False,
        'orders_placed': 0, 'exchange_requests': 0, 'symbol': symbol,
        'side': side, 'timeframe': '5m' if tf.startswith('5') else '15m',
        'risk_percent': str(risk_percent), 'risk_budget_usdt': str(risk_budget),
        'hypothetical_base_asset_quantity': str(base_asset_quantity),
        'hypothetical_notional_usdt': str(indicative_notional),
        'risk_reward_ratio': str(abs(tp-entry)/stop_gap),
        'blockers': [
            'Equity is user-supplied, not verified by this calculation',
            'Gate.io contract multiplier and lot increments are NOT validated',
            'Margin, fees, spread, leverage, position mode and existing exposure are NOT checked',
            'Exchange SL/TP placement, fill reconciliation and durable live idempotency are not implemented',
            'No real trading authorization exists'
        ]
    }


@app.post('/terminal/live-candidate-preview')
def terminal_live_candidate_preview():
    # Authenticated read-only calculator. Enforces same-origin when Origin exists.
    if not _terminal_permitted():
        return _terminal_guard()
    origin = request.headers.get('Origin')
    if origin:
        configured = os.getenv('PUBLIC_BASE_URL','').strip().rstrip('/') or ('https://' + request.host)
        target = urlsplit(configured)
        given = urlsplit(origin)
        if (target.scheme != 'https' or not target.netloc or target.path or target.query or target.fragment
                or given.scheme != 'https' or given.netloc != target.netloc or given.path or given.query or given.fragment):
            return jsonify(error='Origin mismatch',orders_placed=0),403
    if os.getenv('LIVE_TRADING_ENABLED','false').strip().lower() != 'false':
        return jsonify(error='Preview requires LIVE_TRADING_ENABLED=false',orders_placed=0),503
    data = request.get_json(silent=True) if request.is_json else None
    result = _live_candidate_dry_run(data)
    response = jsonify(result)
    response.headers['Cache-Control'] = 'private, no-store'
    return response, (200 if result['status'] == 'DRY_RUN_ONLY' else 422)


# STAGE 2: serialized, persistent DRY-RUN risk gate. Never creates exchange orders.
# Tests concurrency and duplicate rejection with PostgreSQL transaction locks.
SIM_RISK_LOCK = 65913043

def _risk_gate_inputs(payload):
    from decimal import Decimal, InvalidOperation
    if not isinstance(payload, dict):
        raise ValueError('JSON object required')
    event_id = str(payload.get('event_id', '')).strip()
    if not re.fullmatch(r'[a-zA-Z0-9_-]{8,80}', event_id):
        raise ValueError('Invalid event_id')
    def decimal_field(name):
        value = payload.get(name)
        if value is None or isinstance(value, bool):
            raise ValueError('Missing or invalid '+name)
        try: number = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError): raise ValueError('Invalid '+name)
        if not number.is_finite() or number <= 0:
            raise ValueError('Invalid '+name)
        return number
    risk = decimal_field('risk_percent')
    leverage = decimal_field('leverage')
    equity = decimal_field('hypothetical_equity_usdt')
    if risk > Decimal('3'):
        raise ValueError('Risk exceeds 3% sandbox ceiling')
    if leverage > Decimal('5'):
        raise ValueError('Leverage exceeds 5x sandbox ceiling')
    if equity > Decimal('1000000000'):
        raise ValueError('Hypothetical equity too high')
    return event_id, risk, leverage, equity

@app.post('/terminal/risk-gate-simulation')
def terminal_risk_gate_simulation():
    if not _terminal_permitted():
        return _terminal_guard()
    origin = request.headers.get('Origin')
    if origin:
        target = urlsplit(os.getenv('PUBLIC_BASE_URL','').strip().rstrip('/') or ('https://' + request.host))
        given = urlsplit(origin)
        if (target.scheme != 'https' or not target.netloc or target.path or target.query or target.fragment
                or given.scheme != 'https' or given.netloc != target.netloc or given.path or given.query or given.fragment):
            return jsonify(status='BLOCKED',reason='Origin mismatch',orders_placed=0),403
    if os.getenv('LIVE_TRADING_ENABLED','false').strip().lower() != 'false':
        return jsonify(status='BLOCKED',reason='This test requires LIVE_TRADING_ENABLED=false',orders_placed=0),503
    try:
        event_id, risk, leverage, equity = _risk_gate_inputs(request.get_json(silent=True))
    except ValueError as exc:
        return jsonify(status='BLOCKED',reason=str(exc),orders_placed=0),422
    from decimal import Decimal
    try:
        with _db_connect() as con:
            _ensure_schema(con)
            with con.cursor() as cur:
                cur.execute('SELECT pg_advisory_xact_lock(%s)', (SIM_RISK_LOCK,))
                cur.execute("UPDATE luxalgo_risk_gate_simulations SET state='EXPIRED' WHERE state='SIMULATED' AND expires_at<=NOW()")
                cur.execute('SELECT state FROM luxalgo_risk_gate_simulations WHERE event_id=%s', (event_id,))
                if cur.fetchone() is not None:
                    outcome='DUPLICATE_BLOCKED'
                else:
                    cur.execute("SELECT COUNT(*) AS n FROM luxalgo_risk_gate_simulations WHERE state='SIMULATED' AND expires_at>NOW()")
                    if cur.fetchone()['n'] >= 1:
                        outcome='ACCOUNT_SLOT_BLOCKED'
                    else:
                        budget = equity * risk / Decimal('100')
                        cur.execute('''INSERT INTO luxalgo_risk_gate_simulations
                          (event_id,expires_at,state,risk_usdt,leverage,details)
                          VALUES (%s,NOW()+INTERVAL '15 minutes','SIMULATED',%s,%s,%s::jsonb)''',
                          (event_id,budget,leverage,json.dumps({'hypothetical_equity_usdt':str(equity), 'risk_percent':str(risk), 'simulation_only':True})))
                        outcome='SIMULATED_ONLY'
        res=jsonify(status=outcome,live_trading=False,orders_enabled=False,orders_placed=0,
                    verified_equity=False,exchange_position_checked=False,
                    note='Sandbox gate only. NOT a live risk lock or proof of safe exchange execution.')
        res.headers['Cache-Control']='private, no-store'
        return res, (200 if outcome=='SIMULATED_ONLY' else 409)
    except (psycopg.Error, RuntimeError, OSError):
        app.logger.exception('Risk gate simulation storage unavailable')
        return jsonify(status='BLOCKED',reason='Database unavailable',orders_placed=0),503

# Stage 2 diagnostic only. Concurrent PostgreSQL advisory-lock exercise; never makes an exchange request.
@app.post('/terminal/risk-gate-self-test')
def terminal_risk_gate_self_test():
    if not _terminal_permitted():
        return _terminal_guard()
    if not _csrf_valid():
        return jsonify(status='BLOCKED', error='Invalid session verification', orders_placed=0), 403
    if os.getenv('LIVE_TRADING_ENABLED','false').strip().lower() != 'false':
        return jsonify(status='BLOCKED', error='LIVE_TRADING_ENABLED must remain false', orders_placed=0), 503
    prefix='st2_'+secrets.token_hex(14)
    ids=(prefix+'_A',prefix+'_B')
    created=[]
    try:
        with _db_connect() as con:
            _ensure_schema(con)
            with con.cursor() as cur:
                cur.execute('SELECT pg_advisory_xact_lock(%s)',(SIM_RISK_LOCK,))
                cur.execute("SELECT COUNT(*) AS n FROM luxalgo_risk_gate_simulations WHERE state='SIMULATED' AND expires_at>NOW()")
                if cur.fetchone()['n']:
                    return jsonify(status='INCONCLUSIVE', detail='An existing simulated reservation is active; retry after it expires.', orders_placed=0), 409
        def attempt(eid):
            with _db_connect() as con:
                with con.cursor() as cur:
                    cur.execute('SELECT pg_advisory_xact_lock(%s)',(SIM_RISK_LOCK,))
                    cur.execute("SELECT COUNT(*) AS n FROM luxalgo_risk_gate_simulations WHERE state='SIMULATED' AND expires_at>NOW()")
                    if cur.fetchone()['n']:
                        return 'ACCOUNT_SLOT_BLOCKED'
                    cur.execute("INSERT INTO luxalgo_risk_gate_simulations (event_id,expires_at,state,risk_usdt,leverage,details) VALUES (%s,NOW()+INTERVAL '3 minutes','SIMULATED',%s,%s,%s::jsonb)",
                        (eid,'2.1','5',json.dumps({'diagnostic_only':True})))
                    return 'SIMULATED_ONLY'
        # Exactly one of these concurrent contenders may reserve the single sandbox slot.
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(attempt, ids))
        created=[ids[i] for i,r in enumerate(results) if r=='SIMULATED_ONLY']
        race_ok=sorted(results)==['ACCOUNT_SLOT_BLOCKED','SIMULATED_ONLY']
        winner=created[0] if created else None
        with _db_connect() as con:
            with con.cursor() as cur:
                cur.execute('SELECT pg_advisory_xact_lock(%s)',(SIM_RISK_LOCK,))
                cur.execute("INSERT INTO luxalgo_risk_gate_simulations (event_id,expires_at,state,risk_usdt,leverage,details) VALUES (%s,NOW()+INTERVAL '3 minutes','SIMULATED',%s,%s,%s::jsonb) ON CONFLICT(event_id) DO NOTHING RETURNING event_id",(winner,'2.1','5',json.dumps({'diagnostic_only':True})))
                duplicate_blocked=winner is not None and cur.fetchone() is None
                cur.execute("SELECT COUNT(*) AS n FROM luxalgo_risk_gate_simulations WHERE event_id=ANY(%s) AND state='SIMULATED'",(list(ids),))
                exactly_one=cur.fetchone()['n']==1
        passed=race_ok and duplicate_blocked and exactly_one
        return jsonify(status='PASS' if passed else 'FAIL',
             detail=('Concurrent requests: one simulated, one blocked; repeated event ID blocked by unique DB record.'
                      if passed else 'Simulation outcome unexpected; inspect logs.'),
             concurrent_outcomes=results,duplicate_blocked=duplicate_blocked,
             single_slot_enforced=exactly_one,live_trading=False,orders_enabled=False,orders_placed=0), (200 if passed else 409)
    except (psycopg.Error,RuntimeError,OSError,ValueError) as exc:
        app.logger.warning('Stage 2 diagnostic could not finish: %s', type(exc).__name__)
        return jsonify(status='INCONCLUSIVE',detail='PostgreSQL diagnostic unavailable; no orders were placed',orders_placed=0),503
    finally:
        # Remove ONLY diagnostic rows. Never alter user-created sandbox reservations.
        try:
            with _db_connect() as con:
                with con.cursor() as cur:
                    cur.execute('SELECT pg_advisory_xact_lock(%s)',(SIM_RISK_LOCK,))
                    cur.execute('DELETE FROM luxalgo_risk_gate_simulations WHERE event_id=ANY(%s)',(list(ids),))
        except (psycopg.Error,RuntimeError,OSError):
            app.logger.warning('Stage 2 diagnostic cleanup deferred; test rows expire automatically')


# Stage 3 diagnostic: contract specification + conservative integer-contract sizing.
# Only GETs public metadata. There is deliberately no order write, no signing,
# and no authenticated futures trade request.
def _stage3_contract_sizing(spec, equity, entry, stop, risk_pct, leverage=5):
    def pos(value, label):
        try:
            x=Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            raise ValueError('Invalid '+label)
        if not x.is_finite() or x<=0:
            raise ValueError('Invalid '+label)
        return x
    if not isinstance(spec,dict) or spec.get('name')!='BTC_USDT' or spec.get('type')!='direct':
        raise ValueError('Unexpected contract metadata')
    if spec.get('in_delisting') or spec.get('enable_decimal'):
        raise ValueError('Delisting or fractional lots require separate validation')
    mult=pos(spec.get('quanto_multiplier'),'multiplier')
    maximum=pos(spec.get('order_size_max'),'maximum size')
    reported_min=pos(spec.get('order_size_min'),'minimum size')
    max_lev=pos(spec.get('leverage_max'),'maximum leverage')
    lev=pos(leverage,'leverage')
    eq=pos(equity,'equity')
    en=pos(entry,'reference entry')
    sl=pos(stop,'reference stop')
    risk=pos(risk_pct,'risk percentage')
    if risk>Decimal('3') or lev>Decimal('5') or lev>max_lev or en==sl:
        raise ValueError('Risk, leverage or stop geometry invalid')
    if reported_min!=reported_min.to_integral_value() or maximum!=maximum.to_integral_value():
        raise ValueError('Contract lot precision not yet supported')
    budget=eq*risk/100
    distance=abs(en-sl)
    count=int((budget/(distance*mult)).to_integral_value(rounding=ROUND_FLOOR))
    if count<int(reported_min) or count>int(maximum) or count<1:
        raise ValueError('Risk budget cannot meet contract limits')
    worst_stop=Decimal(count)*distance*mult
    nominal=Decimal(count)*en*mult
    estimated_margin=nominal/lev
    if worst_stop>budget or estimated_margin>eq:
        raise ValueError('Estimated stop loss or margin exceeds limits')
    return dict(contracts=count, estimated_stop_loss=str(worst_stop), risk_budget=str(budget),
                estimated_margin=str(estimated_margin), leverage=str(lev))

@app.post('/terminal/stage3-contract-self-test')
def terminal_stage3_contract_self_test():
    if not _terminal_permitted(): return _terminal_guard()
    if not _csrf_valid():
        return jsonify(status='BLOCKED',error='Invalid session verification',orders_placed=0),403
    if os.getenv('LIVE_TRADING_ENABLED','false').strip().lower()!='false':
        return jsonify(status='BLOCKED',error='Live trading must remain OFF',orders_placed=0),503
    try:
        req=Request('https://api.gateio.ws/api/v4/futures/usdt/contracts/BTC_USDT',
                    headers={'Accept':'application/json','User-Agent':'LuxAlgo-PRO-Stage3-ReadOnly/1.0'},method='GET')
        with urlopen(req,timeout=8) as r:
            if getattr(r,'status',200)!=200: raise ValueError('Contract metadata response invalid')
            doc=json.loads(r.read(65536))
        multiplier=Decimal(str(doc.get('quanto_multiplier','0')))
        # Synthetic prices chosen to test rounding; not a recommendation or live quote.
        # Equity chosen to fit at least one BTC contract for most direct BTC_USDT configurations.
        sample_entry=Decimal('100000')
        sample_stop=Decimal('99000')
        long_case=_stage3_contract_sizing(doc,Decimal('10000'),sample_entry,sample_stop,Decimal('3'))
        short_case=_stage3_contract_sizing(doc,Decimal('10000'),sample_entry,Decimal('101000'),Decimal('3'))
        valid=long_case['contracts']==short_case['contracts'] and long_case['contracts']>0
        negative_tests=[]
        for test in (
            lambda:_stage3_contract_sizing(doc,10000,100000,99000,4),
            lambda:_stage3_contract_sizing(doc,10000,100000,99000,3,6),
            lambda:_stage3_contract_sizing(doc,10000,100000,100000,3),
            lambda:_stage3_contract_sizing(doc,10000,100000,99000,3,'NaN'),
        ):
            try: test();negative_tests.append(False)
            except ValueError: negative_tests.append(True)
        passed=valid and all(negative_tests)
        return jsonify(status='PASS' if passed else 'FAIL',
            detail=('Public BTC_USDT metadata verified; LONG/SHORT integer contract sizing and four invalid inputs checked. Sizing is illustrative only.'
                    if passed else 'Unexpected sizing validation outcome.'),
            contract='BTC_USDT',public_metadata_only=True,example_long=long_case,example_short=short_case,
            bad_inputs_rejected=sum(negative_tests),simulation_only=True,orders_enabled=False,
            live_trading=False,orders_placed=0), (200 if passed else 409)
    except (ValueError,HTTPError,URLError,TimeoutError,OSError,KeyError,TypeError,OverflowError,InvalidOperation) as ex:
        app.logger.warning('Stage 3 public contract sizing self-test inconclusive: %s',type(ex).__name__)
        return jsonify(status='INCONCLUSIVE',detail='Public contract specifications or illustrative sizing unavailable; no orders placed',orders_placed=0),503

# Stage 4 protection validation is a strictly offline/read-only diagnostic.
# This does NOT create, register, or acknowledge exchange-side SL/TP orders.
def _stage4_validate_protection(spec, side, entry, stop, take_profit, contracts):
    if not isinstance(spec, dict) or spec.get('name') != 'BTC_USDT' or spec.get('type') != 'direct':
        raise ValueError('Unsupported contract')
    if spec.get('in_delisting') or spec.get('enable_decimal'):
        raise ValueError('Contract not supported by this diagnostic')
    def positive(x):
        try: d=Decimal(str(x))
        except (InvalidOperation,TypeError,ValueError): raise ValueError('Invalid numeric input')
        if not d.is_finite() or d <= 0: raise ValueError('Nonpositive/nonfinite input')
        return d
    tick=positive(spec.get('order_price_round'))
    maximum=positive(spec.get('order_size_max'))
    minimum=positive(spec.get('order_size_min'))
    ep, sl, tp=positive(entry),positive(stop),positive(take_profit)
    qty=positive(contracts)
    if qty != qty.to_integral_value() or qty < minimum or qty > maximum:
        raise ValueError('Invalid integer contract count')
    # Non-multiples are rejected instead of silently rounding protection away.
    if any(price / tick != (price / tick).to_integral_value() for price in (ep,sl,tp)):
        raise ValueError('Price not aligned with public tick size')
    if side=='LONG':
        if not sl < ep < tp: raise ValueError('Invalid LONG protection geometry')
        closing_side='SELL'
    elif side=='SHORT':
        if not tp < ep < sl: raise ValueError('Invalid SHORT protection geometry')
        closing_side='BUY'
    else:
        raise ValueError('Unsupported position direction')
    # Do not create a Gate.io order payload: actual fields/position mode require
    # separate API integration testing and acknowledgement/reconciliation.
    return dict(position_side=side,closing_side=closing_side,contracts=int(qty),
                tick_size=str(tick),entry=str(ep),stop_loss=str(sl),take_profit=str(tp),
                exchange_orders_placed=0,exchange_protection_acknowledged=False)

@app.post('/terminal/stage4-protection-self-test')
def terminal_stage4_protection_self_test():
    if not _terminal_permitted(): return _terminal_guard()
    if not _csrf_valid():
        return jsonify(status='BLOCKED',error='Invalid session verification',orders_placed=0),403
    if os.getenv('LIVE_TRADING_ENABLED','false').strip().lower()!='false':
        return jsonify(status='BLOCKED',error='Live trading must remain OFF',orders_placed=0),503
    try:
        req=Request('https://api.gateio.ws/api/v4/futures/usdt/contracts/BTC_USDT',
                    headers={'Accept':'application/json','User-Agent':'LuxAlgo-PRO-Stage4-ReadOnly/1.0'},method='GET')
        with urlopen(req,timeout=8) as r:
            if getattr(r,'status',200)!=200: raise ValueError('Public metadata HTTP error')
            spec=json.loads(r.read(65536))
        tick=Decimal(str(spec.get('order_price_round')))
        if not tick.is_finite() or tick<=0: raise ValueError('Invalid tick')
        # Synthetic prices aligned to actual public tick metadata; no live quote used.
        entry=tick*1000000
        stop_long=entry-tick*100
        tp_long=entry+tick*150
        if stop_long<=0: raise ValueError('Synthetic price too small')
        long_result=_stage4_validate_protection(spec,'LONG',entry,stop_long,tp_long,1)
        short_result=_stage4_validate_protection(spec,'SHORT',entry,tp_long,stop_long,1)
        invalid_cases=(
            ('LONG',entry,tp_long,stop_long,1),
            ('SHORT',entry,stop_long,tp_long,1),
            ('LONG',entry,entry,tp_long,1),
            ('LONG',entry,stop_long,tp_long+tick/2,1),
            ('INVALID',entry,stop_long,tp_long,1),
            ('LONG',entry,stop_long,tp_long,'NaN'),
        )
        rejected=0
        for args in invalid_cases:
            try: _stage4_validate_protection(spec,*args)
            except ValueError: rejected+=1
        passed=(long_result['closing_side']=='SELL' and short_result['closing_side']=='BUY' and rejected==len(invalid_cases))
        return jsonify(status='PASS' if passed else 'FAIL',
          detail=('Public BTC_USDT tick metadata, LONG/SHORT SL/TP geometry, closing sides and 6 invalid cases checked. Exchange-side protection NOT placed or verified.' if passed else 'Protection diagnostic mismatch'),
          long=long_result,short=short_result,invalid_inputs_rejected=rejected,
          orders_placed=0,orders_enabled=False,live_trading=False,exchange_sl_tp_acknowledged=False), (200 if passed else 409)
    except (ValueError,HTTPError,URLError,TimeoutError,OSError,TypeError,InvalidOperation,OverflowError) as ex:
        app.logger.warning('Stage 4 protection diagnostic inconclusive: %s',type(ex).__name__)
        return jsonify(status='INCONCLUSIVE',detail='Public contract metadata/precision unavailable; no exchange orders',orders_placed=0),503

# Stage 5: PostgreSQL simulated-only lifecycle diagnostic. No network requests to Gate.io.
@app.post('/terminal/stage5-ledger-self-test')
def terminal_stage5_ledger_self_test():
    if not _terminal_permitted():
        return _terminal_guard()
    if not _csrf_valid():
        return jsonify(status='BLOCKED',error='Invalid session verification',orders_placed=0),403
    if os.getenv('LIVE_TRADING_ENABLED','false').strip().lower() != 'false':
        return jsonify(status='BLOCKED',error='Live trading must remain disabled',orders_placed=0),503
    event_id='st5_'+secrets.token_hex(20)
    try:
        with _db_connect() as con:
            _ensure_schema(con)
            with con.cursor() as cur:
                # Identity-based idempotency: only one lifecycle per simulated event ID.
                cur.execute("INSERT INTO luxalgo_stage5_sim_order_ledger(event_id,state,detail) VALUES (%s,'RESERVED',%s::jsonb) ON CONFLICT(event_id) DO NOTHING RETURNING event_id",
                            (event_id,json.dumps({'test':True,'exchange_order':False})))
                inserted=cur.fetchone() is not None
                cur.execute("INSERT INTO luxalgo_stage5_sim_order_ledger(event_id,state,detail) VALUES (%s,'RESERVED',%s::jsonb) ON CONFLICT(event_id) DO NOTHING RETURNING event_id",
                            (event_id,json.dumps({'test':True})))
                replay_blocked=cur.fetchone() is None
                # Compare-and-swap state transitions prevent repeated ACK and invalid closure.
                cur.execute("UPDATE luxalgo_stage5_sim_order_ledger SET state='SIMULATED_ACK',updated_at=NOW() WHERE event_id=%s AND state='RESERVED' RETURNING event_id",(event_id,))
                acked=cur.fetchone() is not None
                cur.execute("UPDATE luxalgo_stage5_sim_order_ledger SET state='SIMULATED_ACK',updated_at=NOW() WHERE event_id=%s AND state='RESERVED' RETURNING event_id",(event_id,))
                double_ack_blocked=cur.fetchone() is None
                # A second DB read simulates a restart recovery scan; it is NOT exchange reconciliation.
                cur.execute("SELECT event_id,state FROM luxalgo_stage5_sim_order_ledger WHERE event_id=%s",(event_id,))
                recovered=cur.fetchone()
                recovered_ok=bool(recovered and recovered['state']=='SIMULATED_ACK')
                cur.execute("UPDATE luxalgo_stage5_sim_order_ledger SET state='CLOSED',updated_at=NOW() WHERE event_id=%s AND state='SIMULATED_ACK' RETURNING event_id",(event_id,))
                closed=cur.fetchone() is not None
                cur.execute("UPDATE luxalgo_stage5_sim_order_ledger SET state='SIMULATED_ACK',updated_at=NOW() WHERE event_id=%s AND state='RESERVED' RETURNING event_id",(event_id,))
                stale_transition_blocked=cur.fetchone() is None
        passed=all((inserted,replay_blocked,acked,double_ack_blocked,recovered_ok,closed,stale_transition_blocked))
        return jsonify(status='PASS' if passed else 'FAIL',
            detail=('Unique simulated event, duplicate replay rejected, compare-and-swap transitions and database recovery read verified.'
                    if passed else 'Simulation state mismatch; inspect server logs.'),
            checks={'inserted':inserted,'duplicate_rejected':replay_blocked,
                    'acknowledged':acked,'double_ack_blocked':double_ack_blocked,
                    'recoverable_from_database':recovered_ok,'closed':closed,
                    'stale_transition_blocked':stale_transition_blocked},
            simulated_only=True,live_trading=False,orders_enabled=False,orders_placed=0), (200 if passed else 409)
    except (psycopg.Error,RuntimeError,OSError,ValueError) as exc:
        app.logger.warning('Stage 5 simulated ledger test failed closed: %s',type(exc).__name__)
        return jsonify(status='INCONCLUSIVE',detail='Database lifecycle diagnostic unavailable; no exchange orders',orders_placed=0),503
    finally:
        # Delete only this diagnostic record; preserve all user signals and paper trades.
        try:
            with _db_connect() as con:
                with con.cursor() as cur:
                    cur.execute("DELETE FROM luxalgo_stage5_sim_order_ledger WHERE event_id=%s AND source='STAGE5_DIAGNOSTIC'",(event_id,))
        except (psycopg.Error,RuntimeError,OSError):
            app.logger.warning('Stage 5 diagnostic row cleanup deferred')

# Stage 6: database-backed persistent lock for any FUTURE live order integration.
# A future execution engine MUST check this flag atomically before every exchange write.
# Current app has no exchange order submission; this is not exchange-wide cancellation.
STAGE6_HALT_LOCK = 65913049

@app.get('/terminal/stage6-halt-status')
def terminal_stage6_halt_status():
    if not _terminal_permitted(): return _terminal_guard()
    try:
        with _db_connect() as con:
            _ensure_schema(con)
            with con.cursor() as cur:
                cur.execute('SELECT halted,reason,changed_at FROM luxalgo_stage6_execution_halt WHERE id=1')
                row=cur.fetchone()
        if not row or row['halted'] is not True: raise RuntimeError('Halt not asserted')
        return jsonify(halted=True,reason=row['reason'],orders_enabled=False,live_trading=False)
    except (psycopg.Error,RuntimeError,OSError):
        return jsonify(halted=True,reason='UNVERIFIED_FAIL_CLOSED',orders_enabled=False,live_trading=False),503

@app.post('/terminal/stage6-emergency-halt')
def terminal_stage6_emergency_halt():
    if not _terminal_permitted(): return _terminal_guard()
    if not _csrf_valid(): return jsonify(error='Invalid verification',halted=True),403
    try:
        with _db_connect() as con:
            _ensure_schema(con)
            with con.cursor() as cur:
                cur.execute('SELECT pg_advisory_xact_lock(%s)',(STAGE6_HALT_LOCK,))
                cur.execute("UPDATE luxalgo_stage6_execution_halt SET halted=TRUE,reason='MANUAL_EMERGENCY_HALT',changed_at=NOW() WHERE id=1 RETURNING halted")
                if not cur.fetchone(): raise RuntimeError('No persistent halt row')
                _bot_enabled(cur,lock=True)
                cur.execute('UPDATE luxalgo_bot_control SET enabled=FALSE,changed_at=NOW() WHERE id=1')
                cur.execute("UPDATE luxalgo_auto_paper_queue SET state='REJECTED',checked_at=NOW(),note='Stage 6 emergency halt' WHERE state='PENDING'")
        return jsonify(halted=True,paper_bot_enabled=False,exchange_orders_cancelled=False,exchange_positions_closed=False,orders_enabled=False,live_trading=False)
    except (psycopg.Error,RuntimeError,OSError):
        return jsonify(error='Halt state could not be confirmed; treat as unsafe. No live order implementation exists.',halted=True),503

@app.post('/terminal/stage6-halt-self-test')
def terminal_stage6_halt_self_test():
    if not _terminal_permitted(): return _terminal_guard()
    if not _csrf_valid(): return jsonify(status='BLOCKED',error='Invalid verification',orders_placed=0),403
    if os.getenv('LIVE_TRADING_ENABLED','false').lower().strip()!='false':
        return jsonify(status='BLOCKED',error='LIVE_TRADING_ENABLED must be false',orders_placed=0),503
    event_id='st6_'+secrets.token_hex(16)
    try:
        with _db_connect() as con:
            _ensure_schema(con)
            with con.cursor() as cur:
                cur.execute('SELECT halted FROM luxalgo_stage6_execution_halt WHERE id=1')
                global_halt=cur.fetchone()
                if not global_halt or global_halt['halted'] is not True:
                    return jsonify(status='FAIL',detail='Future execution halt is not asserted',orders_placed=0),409
                cur.execute('INSERT INTO luxalgo_stage6_halt_diagnostics(event_id,halted) VALUES (%s,TRUE) RETURNING event_id',(event_id,))
                inserted=cur.fetchone() is not None
                cur.execute('UPDATE luxalgo_stage6_halt_diagnostics SET halted=FALSE WHERE event_id=%s AND halted=FALSE RETURNING event_id',(event_id,))
                invalid_transition_blocked=cur.fetchone() is None
        # Separate connection tests a committed read, simulating process restart.
        with _db_connect() as con:
            with con.cursor() as cur:
                cur.execute('SELECT halted FROM luxalgo_stage6_halt_diagnostics WHERE event_id=%s',(event_id,))
                row=cur.fetchone()
                recovered=bool(row and row['halted'] is True)
                cur.execute('SELECT halted FROM luxalgo_stage6_execution_halt WHERE id=1')
                global_recovered=bool((cur.fetchone() or {}).get('halted') is True)
        passed=all((inserted,invalid_transition_blocked,recovered,global_recovered))
        return jsonify(status='PASS' if passed else 'FAIL',detail=('Persistent future-execution HALT verified; invalid diagnostic transition blocked; fresh-connection recovery read passed. No exchange positions closed.' if passed else 'Halt/recovery diagnostic failed.'),orders_placed=0,live_trading=False), (200 if passed else 409)
    except (psycopg.Error,RuntimeError,OSError) as exc:
        app.logger.warning('Stage 6 halt diagnostic unavailable: %s',type(exc).__name__)
        return jsonify(status='INCONCLUSIVE',detail='PostgreSQL halt test unavailable; fail closed',orders_placed=0),503
    finally:
        try:
            with _db_connect() as con:
                with con.cursor() as cur:
                    cur.execute('DELETE FROM luxalgo_stage6_halt_diagnostics WHERE event_id=%s',(event_id,))
        except (psycopg.Error,RuntimeError,OSError):
            pass

# Stage 7: audit-only inventory of missing live-execution protections.
# Never turns execution on, checks secret permissions, or makes exchange write calls.
@app.get('/terminal/stage7-readiness-audit')
def terminal_stage7_readiness_audit():
    if not _terminal_permitted(): return _terminal_guard()
    checks = []
    def check(name, passed, detail):
        checks.append(dict(name=name, passed=bool(passed), detail=detail))

    env_off = os.getenv('LIVE_TRADING_ENABLED', 'false').strip().lower() == 'false'
    check('Live execution environment disabled', env_off,
          'LIVE_TRADING_ENABLED must stay false; no exchange order routes implemented')
    db_halt_verified = False
    try:
        with _db_connect() as con:
            _ensure_schema(con)
            with con.cursor() as cur:
                cur.execute('SELECT halted,reason FROM luxalgo_stage6_execution_halt WHERE id=1')
                halt_row = cur.fetchone()
                cur.execute('SELECT to_regclass(%s) AS ledger',('luxalgo_stage5_sim_order_ledger',))
                ledger_row = cur.fetchone()
                db_halt_verified = bool(halt_row and halt_row['halted'] is True)
        check('PostgreSQL future-execution halt', db_halt_verified,
              'Database confirmed HALTED' if db_halt_verified else 'Halt not verified — fail closed')
        check('Stage 5 simulated ledger schema', bool(ledger_row and ledger_row['ledger']),
              'Simulated test ledger exists; not reconciled Gate.io orders')
    except (psycopg.Error, RuntimeError, OSError):
        check('PostgreSQL future-execution halt', False,
              'Database unavailable or unverified — fail closed')
        check('Stage 5 simulated ledger schema', False,
              'Cannot verify schema; simulated ledger is never a live order ledger')

    missing = [
        ('Exchange order submission and acknowledgment',
         'No tested Gate.io write API, client-order IDs, timeout recovery or acknowledgment validation'),
        ('Exchange-native protective SL/TP',
         'Stage 4 validates hypothetical levels only; real protective orders not acknowledged'),
        ('Verified fills and order reconciliation',
         'No read-after-write fill matching, partial-fill handling or exchange order lifecycle'),
        ('Durable live order idempotency',
         'Stage 5 ledger is for simulations only; no exchange order correlation or recovery'),
        ('Atomic account-wide risk enforcement',
         'Stage 2 risk gate is simulated; margin, exposure and position limits are not enforced on orders'),
        ('Emergency exchange cancellation and position handling',
         'Stage 6 HALT blocks proposed future execution; does not cancel exchange orders or close positions'),
        ('Startup reconciliation and operational resilience',
         'No audited restart reconciliation, deployment uptime/SLA, stale price or order timeout safety tests'),
    ]
    for name, detail in missing:
        check(name, False, detail)

    result = jsonify(status='BLOCKED', safe_to_trade=False, live_trading=False,
                     orders_enabled=False, orders_placed=0,
                     passed_prerequisites=sum(bool(c['passed']) for c in checks),
                     missing_critical=len(missing), checks=checks,
                     summary='Read-only safety inventory complete; live execution is NOT ready',
                     stage='STAGE7_READ_ONLY_AUDIT')
    result.headers['Cache-Control'] = 'private, no-store'
    return result

# Stage 8: TEST-ONLY uncertain acknowledgment and durable correlation exercise.
# No network calls, no live client-order IDs, no permission elevation.
@app.post('/terminal/stage8-ack-recovery-test')
def terminal_stage8_ack_recovery_test():
    if not _terminal_permitted(): return _terminal_guard()
    if os.getenv('LIVE_TRADING_ENABLED','false').strip().lower() != 'false':
        return jsonify(status='BLOCKED',error='LIVE_TRADING_ENABLED must remain false',orders_placed=0),503
    diagnostic_id = 'STAGE8_TEST_' + secrets.token_hex(16)
    try:
        # First connection: persist one unique client reference, reject a replay,
        # and quarantine a hypothetical lost acknowledgment for manual review.
        with _db_connect() as con:
            _ensure_schema(con)
            with con.cursor() as cur:
                cur.execute("""INSERT INTO luxalgo_stage8_ack_diagnostics(client_id,state)
                               VALUES (%s,'PREPARED') ON CONFLICT(client_id) DO NOTHING
                               RETURNING client_id""", (diagnostic_id,))
                inserted = cur.fetchone() is not None
                cur.execute("""INSERT INTO luxalgo_stage8_ack_diagnostics(client_id,state)
                               VALUES (%s,'PREPARED') ON CONFLICT(client_id) DO NOTHING
                               RETURNING client_id""", (diagnostic_id,))
                duplicate_rejected = cur.fetchone() is None
                cur.execute("""UPDATE luxalgo_stage8_ack_diagnostics
                               SET state='ACK_UNKNOWN',updated_at=NOW()
                               WHERE client_id=%s AND state='PREPARED' RETURNING state""",
                            (diagnostic_id,))
                marked_unknown = cur.fetchone() is not None
                # A retry is NEVER assumed safe while acknowledgment is uncertain.
                cur.execute("""UPDATE luxalgo_stage8_ack_diagnostics
                               SET state='PREPARED',updated_at=NOW()
                               WHERE client_id=%s AND state='PREPARED' RETURNING state""",
                            (diagnostic_id,))
                unsafe_retry_blocked = cur.fetchone() is None
                cur.execute("""UPDATE luxalgo_stage8_ack_diagnostics
                               SET state='REVIEW_REQUIRED',updated_at=NOW()
                               WHERE client_id=%s AND state='ACK_UNKNOWN' RETURNING state""",
                            (diagnostic_id,))
                quarantined = cur.fetchone() is not None
        # Open a fresh connection to verify restart-style durable recovery.
        with _db_connect() as con:
            with con.cursor() as cur:
                cur.execute('SELECT state,source FROM luxalgo_stage8_ack_diagnostics WHERE client_id=%s',
                            (diagnostic_id,))
                recovered = cur.fetchone()
                cur.execute('SELECT halted FROM luxalgo_stage6_execution_halt WHERE id=1')
                halt = cur.fetchone()
        passed = all((inserted,duplicate_rejected,marked_unknown,
                      unsafe_retry_blocked,quarantined,
                      bool(recovered and recovered['state']=='REVIEW_REQUIRED' and
                           recovered['source']=='STAGE8_DIAGNOSTIC'),
                      bool(halt and halt['halted'] is True)))
        detail=('Unique diagnostic client ID, duplicate replay rejection, '
                'uncertain-ack quarantine, retry prohibition, fresh-connection '
                'recovery and persistent HALT verified' if passed else
                'One or more durable safety checks did not pass; fail closed')
        response=jsonify(status='PASS' if passed else 'BLOCKED',detail=detail,
                         stage='STAGE8_SIMULATION_ONLY',exchange_orders=0,
                         live_trading=False,orders_enabled=False,
                         exchange_acknowledgment_verified=False)
        response.headers['Cache-Control']='private, no-store'
        return response, (200 if passed else 503)
    except (psycopg.Error,RuntimeError,OSError):
        return jsonify(status='BLOCKED',error='Database diagnostic unavailable; fail closed',
                       exchange_orders=0,orders_enabled=False),503

# Stage 9: fixture-only reconciliation state classification. No exchange network calls.
def _stage9_classify(expected, observed):
    # Fail closed on missing, partial, inconsistent or invalid exchange snapshots.
    if not isinstance(expected, dict) or not isinstance(observed, (dict, type(None))):
        return 'UNKNOWN'
    try:
        expected_id = str(expected['client_id'])
        total = Decimal(str(expected['contracts']))
        if not expected_id or total <= 0 or not total.is_finite():
            return 'UNKNOWN'
        if observed is None:
            return 'MISSING'
        if str(observed.get('client_id', '')) != expected_id:
            return 'CONFLICT'
        filled = Decimal(str(observed.get('filled_contracts', 'NaN')))
        if not filled.is_finite() or filled < 0 or filled > total:
            return 'CONFLICT'
        state = observed.get('state')
        if state == 'FILLED' and filled == total:
            return 'MATCHED'
        if state in ('OPEN', 'PARTIAL') and filled < total:
            return 'PARTIAL_FILL' if filled > 0 else 'UNKNOWN'
        return 'CONFLICT'
    except (KeyError, ValueError, TypeError, InvalidOperation):
        return 'UNKNOWN'


@app.post('/terminal/stage9-reconciliation-test')
def terminal_stage9_reconciliation_test():
    if not _terminal_permitted(): return _terminal_guard()
    if not _csrf_valid():
        return jsonify(status='BLOCKED',error='Invalid session verification',exchange_orders=0),403
    if os.getenv('LIVE_TRADING_ENABLED', 'false').strip().lower() != 'false':
        return jsonify(status='BLOCKED',error='Live execution environment must remain disabled',exchange_orders=0),503
    expected = {'client_id': 'STAGE9_LOCAL_FIXTURE', 'contracts': '10'}
    cases = [
        ('MATCHED', {'client_id': 'STAGE9_LOCAL_FIXTURE','filled_contracts':'10','state':'FILLED'}),
        ('MISSING', None),
        ('PARTIAL_FILL', {'client_id': 'STAGE9_LOCAL_FIXTURE','filled_contracts':'4','state':'PARTIAL'}),
        ('CONFLICT', {'client_id': 'DIFFERENT_ID','filled_contracts':'10','state':'FILLED'}),
        ('CONFLICT', {'client_id': 'STAGE9_LOCAL_FIXTURE','filled_contracts':'11','state':'FILLED'}),
        ('UNKNOWN', {'client_id': 'STAGE9_LOCAL_FIXTURE','filled_contracts':'invalid','state':'OPEN'}),
    ]
    results = [(wanted, _stage9_classify(expected, exchange_fixture)) for wanted, exchange_fixture in cases]
    if not all(wanted == actual for wanted,actual in results):
        return jsonify(status='BLOCKED',error='Fixture classification mismatch',exchange_orders=0),503
    diagnostic_id = 'STAGE9_TEST_' + secrets.token_hex(16)
    try:
        with _db_connect() as con:
            _ensure_schema(con)
            with con.cursor() as cur:
                cur.execute('''INSERT INTO luxalgo_stage9_reconcile_diagnostics(diagnostic_id,result,details)
                               VALUES (%s,%s,%s::jsonb)''',
                            (diagnostic_id,'UNKNOWN',json.dumps({'cases':len(cases),'real_exchange_data':False})))
        with _db_connect() as con:
            with con.cursor() as cur:
                cur.execute('''SELECT result,source FROM luxalgo_stage9_reconcile_diagnostics
                               WHERE diagnostic_id=%s''',(diagnostic_id,))
                persisted=cur.fetchone()
                cur.execute('SELECT halted FROM luxalgo_stage6_execution_halt WHERE id=1')
                halt=cur.fetchone()
        passed=bool(persisted and persisted['result']=='UNKNOWN' and
                    persisted['source']=='STAGE9_FIXTURE_ONLY' and halt and halt['halted'] is True)
        response=jsonify(status='PASS' if passed else 'BLOCKED',
           detail=('6 fixture cases (match, missing, partial fill, identity/quantity conflicts, malformed data), '
                   'fresh-connection recovery and persistent HALT verified; real exchange reconciliation NOT performed'
                   if passed else 'Durable diagnostic recovery or HALT verification failed'),
           stage='STAGE9_FIXTURE_ONLY',cases_checked=len(cases),exchange_orders=0,
           actual_gateio_orders_queried=False,exchange_reconciliation_verified=False,
           live_trading=False,orders_enabled=False)
        response.headers['Cache-Control']='private, no-store'
        return response,(200 if passed else 503)
    except (psycopg.Error, RuntimeError, OSError):
        return jsonify(status='BLOCKED',error='PostgreSQL unavailable; fail closed',exchange_orders=0),503


# Stage 10: pure diagnostic fixture classification; no Gate.io protection calls.
def _stage10_protection_classify(position, protections):
    try:
        if not isinstance(position,dict) or not isinstance(protections,list):
            return 'BLOCKED'
        side=position['side']
        qty=Decimal(str(position['filled_contracts']))
        entry=Decimal(str(position['entry_price']))
        if side not in ('LONG','SHORT') or not qty.is_finite() or qty<=0 or not entry.is_finite() or entry<=0:
            return 'BLOCKED'
        if len(protections)!=2:
            return 'BLOCKED'
        by_type={}
        for order in protections:
            if not isinstance(order,dict): return 'BLOCKED'
            typ=order.get('type')
            if typ not in ('SL','TP') or typ in by_type: return 'BLOCKED'
            if order.get('state')!='OPEN' or order.get('reduce_only') is not True:
                return 'BLOCKED'
            if order.get('position_side')!=side or order.get('close_side')!=('SELL' if side=='LONG' else 'BUY'):
                return 'BLOCKED'
            amount=Decimal(str(order['contracts']))
            price=Decimal(str(order['trigger_price']))
            if (not amount.is_finite() or amount!=qty or
                not price.is_finite() or price<=0): return 'BLOCKED'
            if side=='LONG' and not ((price<entry) if typ=='SL' else (price>entry)):
                return 'BLOCKED'
            if side=='SHORT' and not ((price>entry) if typ=='SL' else (price<entry)):
                return 'BLOCKED'
            by_type[typ]=order
        return 'PROTECTED_FIXTURE' if set(by_type)=={'SL','TP'} else 'BLOCKED'
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return 'BLOCKED'


@app.post('/terminal/stage10-protection-test')
def terminal_stage10_protection_test():
    if not _terminal_permitted(): return _terminal_guard()
    if not _csrf_valid():
        return jsonify(status='BLOCKED',error='Invalid session verification',exchange_orders=0),403
    if os.getenv('LIVE_TRADING_ENABLED','false').strip().lower()!='false':
        return jsonify(status='BLOCKED',error='Live execution must remain disabled',exchange_orders=0),503
    long={'side':'LONG','filled_contracts':'4','entry_price':'100'}
    short={'side':'SHORT','filled_contracts':'2','entry_price':'100'}
    sl={'type':'SL','state':'OPEN','reduce_only':True,'position_side':'LONG','close_side':'SELL','contracts':'4','trigger_price':'95'}
    tp={'type':'TP','state':'OPEN','reduce_only':True,'position_side':'LONG','close_side':'SELL','contracts':'4','trigger_price':'110'}
    ssl=dict(sl,position_side='SHORT',close_side='BUY',contracts='2',trigger_price='105')
    stp=dict(tp,position_side='SHORT',close_side='BUY',contracts='2',trigger_price='90')
    cases=[
       ('PROTECTED_FIXTURE',long,[sl,tp]),
       ('PROTECTED_FIXTURE',short,[ssl,stp]),
       ('BLOCKED',long,[sl]),
       ('BLOCKED',long,[sl,dict(tp,contracts='3')]),
       ('BLOCKED',long,[sl,dict(tp,reduce_only=False)]),
       ('BLOCKED',long,[sl,dict(tp,close_side='BUY')]),
       ('BLOCKED',long,[sl,dict(tp,trigger_price='90')]),
       ('BLOCKED',long,[sl,dict(tp,state='CANCELLED')]),
       ('BLOCKED',long,[sl,dict(tp,contracts='garbled')]),
       ('BLOCKED',long,[sl,dict(sl)]),
       ('BLOCKED',dict(long,filled_contracts='6'),[sl,tp]),
    ]
    if not all(_stage10_protection_classify(pos, orders)==wanted for wanted,pos,orders in cases):
        return jsonify(status='BLOCKED',error='Fixture classification mismatch',exchange_orders=0),503
    diagnostic_id='STAGE10_TEST_'+secrets.token_hex(16)
    try:
        with _db_connect() as con:
            _ensure_schema(con)
            with con.cursor() as cur:
                cur.execute("INSERT INTO luxalgo_stage10_protection_diagnostics(diagnostic_id,result,details) VALUES (%s,'BLOCKED',%s::jsonb)",
                            (diagnostic_id,json.dumps({'cases':len(cases),'exchange_data':False})))
        with _db_connect() as con:
            with con.cursor() as cur:
                cur.execute('SELECT result,source FROM luxalgo_stage10_protection_diagnostics WHERE diagnostic_id=%s',(diagnostic_id,))
                saved=cur.fetchone()
                cur.execute('SELECT halted FROM luxalgo_stage6_execution_halt WHERE id=1')
                halt=cur.fetchone()
        passed=bool(saved and saved['result']=='BLOCKED' and saved['source']=='STAGE10_FIXTURE_ONLY'
                    and halt and halt['halted'] is True)
        reply=jsonify(status='PASS' if passed else 'BLOCKED',
            detail=('11 fixture scenarios (LONG/SHORT, missing SL/TP, incorrect side, quantity, triggers, cancelled protection, partial fill and malformed data), fresh PostgreSQL recovery and HALT verified; real Gate.io protective orders NOT checked'
                    if passed else 'Diagnostic recovery or persistent HALT failed'),
            cases_checked=len(cases),exchange_orders=0,live_trading=False,orders_enabled=False,
            real_exchange_protection_verified=False,stage='STAGE10_FIXTURE_ONLY')
        reply.headers['Cache-Control']='private, no-store'
        return reply,(200 if passed else 503)
    except (psycopg.Error,RuntimeError,OSError):
        return jsonify(status='BLOCKED',error='PostgreSQL unavailable; fail closed',exchange_orders=0),503


# Stage 11: account-wide risk limits for isolated diagnostic fixtures ONLY.
# No signal handler or Gate.io route consumes these records.
def _stage11_reserve_fixture(run_id, event_id, equity, available_margin, risk, margin):
    try:
        values=[Decimal(str(v)) for v in (equity,available_margin,risk,margin)]
        eq,available,risk_value,margin_value=values
        if any(not v.is_finite() for v in values) or eq<=0 or available<=0 or risk_value<=0 or margin_value<=0:
            return 'INVALID'
        # Fail closed: <=0.5% per trade, <=2.5% aggregate, <=5 positions.
        if risk_value>eq*Decimal('0.005'):
            return 'PER_TRADE_LIMIT'
        with _db_connect() as con:
            with con.cursor() as cur:
                # Lock per run across different app processes, NOT an in-memory threading lock.
                cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',('STAGE11:'+run_id,))
                cur.execute('SELECT halted FROM luxalgo_stage6_execution_halt WHERE id=1')
                halted=cur.fetchone()
                if not halted or halted['halted'] is not True:
                    return 'HALT_NOT_CONFIRMED'
                cur.execute('SELECT COUNT(*) AS n,COALESCE(SUM(risk_usdt),0) AS risk,COALESCE(SUM(margin_usdt),0) AS margin FROM luxalgo_stage11_risk_diagnostics WHERE run_id=%s',(run_id,))
                totals=cur.fetchone()
                if totals['n']>=5: return 'MAX_POSITIONS'
                if totals['risk']+risk_value>eq*Decimal('0.025'): return 'TOTAL_RISK'
                if totals['margin']+margin_value>available: return 'INSUFFICIENT_MARGIN'
                cur.execute('INSERT INTO luxalgo_stage11_risk_diagnostics(run_id,event_id,risk_usdt,margin_usdt) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING event_id',
                            (run_id,event_id,risk_value,margin_value))
                return 'RESERVED_FIXTURE' if cur.fetchone() else 'DUPLICATE'
    except (InvalidOperation,ValueError,TypeError):
        return 'INVALID'


@app.post('/terminal/stage11-atomic-risk-test')
def terminal_stage11_atomic_risk_test():
    if not _terminal_permitted(): return _terminal_guard()
    if not _csrf_valid(): return jsonify(status='BLOCKED',error='Invalid session verification',exchange_orders=0),403
    if os.getenv('LIVE_TRADING_ENABLED','false').strip().lower()!='false':
        return jsonify(status='BLOCKED',error='Live execution must remain disabled',exchange_orders=0),503
    run_id='st11_'+secrets.token_hex(16)
    try:
        with _db_connect() as con:
            _ensure_schema(con)
            with con.cursor() as cur:
                cur.execute('SELECT halted FROM luxalgo_stage6_execution_halt WHERE id=1')
                halt=cur.fetchone()
                if not halt or halt['halted'] is not True:
                    return jsonify(status='BLOCKED',error='Persistent HALT not confirmed',exchange_orders=0),503
        # Thread pool forces competition between distinct DB transactions.
        with ThreadPoolExecutor(max_workers=6) as pool:
            outcomes=list(pool.map(lambda i:_stage11_reserve_fixture(run_id,'event_'+str(i),'1000','100','5','20'),range(6)))
        duplicate_run=run_id+'_duplicate'
        duplicate_first=_stage11_reserve_fixture(duplicate_run,'same','1000','100','5','20')
        duplicates=_stage11_reserve_fixture(duplicate_run,'same','1000','100','5','20')
        invalid=_stage11_reserve_fixture(run_id,'invalid','1000','100','NaN','20')
        per_trade=_stage11_reserve_fixture(run_id,'too_risky','1000','100','5.01','1')
        # Dedicated runs isolate margin/aggregate rejection cases without touching bot data.
        margin_run=run_id+'_margin'
        margin_first=_stage11_reserve_fixture(margin_run,'one','1000','10','5','9')
        margin_block=_stage11_reserve_fixture(margin_run,'two','1000','10','5','2')
        with _db_connect() as con:
            with con.cursor() as cur:
                cur.execute('SELECT COUNT(*) AS n,COALESCE(SUM(risk_usdt),0) AS risk,COALESCE(SUM(margin_usdt),0) AS margin FROM luxalgo_stage11_risk_diagnostics WHERE run_id=%s',(run_id,))
                recovered=cur.fetchone()
                cur.execute('SELECT halted FROM luxalgo_stage6_execution_halt WHERE id=1')
                halted=cur.fetchone()
        passed=(outcomes.count('RESERVED_FIXTURE')==5 and outcomes.count('MAX_POSITIONS')==1
                and duplicate_first=='RESERVED_FIXTURE' and duplicates=='DUPLICATE' and invalid=='INVALID' and per_trade=='PER_TRADE_LIMIT'
                and margin_first=='RESERVED_FIXTURE' and margin_block=='INSUFFICIENT_MARGIN'
                and recovered and recovered['n']==5 and recovered['risk']==Decimal('25')
                and recovered['margin']==Decimal('100') and halted and halted['halted'] is True)
        result=jsonify(status='PASS' if passed else 'BLOCKED',exchange_orders=0,live_trading=False,
            stage='STAGE11_FIXTURE_ONLY',reserved=outcomes.count('RESERVED_FIXTURE'),
            detail=('Six concurrent reservations: five admitted, sixth rejected; 0.5% per-trade and 2.5% aggregate cap, duplicate, malformed input, margin rejection, fresh-connection recovery and persistent HALT verified; NOT exchange-wide risk enforcement'
                    if passed else 'Risk simulation results inconsistent; no orders placed'))
        result.headers['Cache-Control']='private, no-store'
        return result,(200 if passed else 503)
    except (psycopg.Error,RuntimeError,OSError):
        return jsonify(status='BLOCKED',error='Database unavailable; fail closed',exchange_orders=0),503



# Stage 12: authenticated Gate.io GET-only visibility test. No orders are sent,
# no simulator records are mistaken for exchange-originating order IDs.
def _stage12_gate_get(path, query=''):
    if path not in ('/api/v4/futures/usdt/orders','/api/v4/futures/usdt/price_orders'):
        raise ValueError('Unapproved read-only endpoint')
    if query != 'status=open&limit=100':
        raise ValueError('Unapproved query')
    key=os.getenv('GATE_API_KEY','').strip()
    secret=os.getenv('GATE_API_SECRET','').strip()
    if not key or not secret:
        raise RuntimeError('Gate.io read-only credentials not configured')
    timestamp=str(int(time.time()))
    body_hash=hashlib.sha512(b'').hexdigest()
    to_sign='\n'.join(('GET',path,query,body_hash,timestamp))
    signature=hmac.new(secret.encode('utf-8'),to_sign.encode('utf-8'),hashlib.sha512).hexdigest()
    req=Request('https://api.gateio.ws'+path+'?'+query,method='GET',headers={
        'KEY':key,'Timestamp':timestamp,'SIGN':signature,
        'Accept':'application/json','User-Agent':'LuxAlgo-PRO-Stage12-Readonly/1.0'})
    with urlopen(req,timeout=10) as response:
        data=json.loads(response.read(400000))
    if not isinstance(data,list) or len(data)>=100:
        raise ValueError('Order list invalid or may be truncated; fail closed')
    seen=set()
    for order in data:
        if not isinstance(order,dict): raise ValueError('Invalid order')
        contract=order.get('contract')
        if not isinstance(contract,str) or not re.fullmatch(r'[A-Z0-9]{2,28}_USDT',contract):
            raise ValueError('Invalid contract')
        oid=order.get('id')
        if not isinstance(oid,(str,int)) or isinstance(oid,bool) or not str(oid).strip():
            raise ValueError('Missing order ID')
        if str(oid) in seen: raise ValueError('Duplicate order ID in snapshot')
        seen.add(str(oid))
    return len(data)

@app.post('/terminal/stage12-gate-readonly-test')
def terminal_stage12_gate_readonly_test():
    if not _terminal_permitted(): return _terminal_guard()
    if not _csrf_valid():
        return jsonify(status='BLOCKED',error='Invalid session verification',exchange_orders=0),403
    if os.getenv('LIVE_TRADING_ENABLED','false').strip().lower()!='false':
        return jsonify(status='BLOCKED',error='Live execution must remain disabled',exchange_orders=0),503
    try:
        with _db_connect() as con:
            _ensure_schema(con)
            with con.cursor() as cur:
                cur.execute('SELECT halted FROM luxalgo_stage6_execution_halt WHERE id=1')
                row=cur.fetchone()
                if not row or row['halted'] is not True:
                    return jsonify(status='BLOCKED',error='Persistent HALT not confirmed',exchange_orders=0),503
        positions=_gate_read_only_positions()
        if not isinstance(positions,list) or len(positions)>1000:
            raise ValueError('Unexpected open positions')
        orders=_stage12_gate_get('/api/v4/futures/usdt/orders','status=open&limit=100')
        triggers=_stage12_gate_get('/api/v4/futures/usdt/price_orders','status=open&limit=100')
        count=len(positions)
        assessment='OBSERVED_UNRECONCILED' if (count or orders or triggers) else 'EMPTY_SNAPSHOT'
        snapshot_id='st12_'+secrets.token_hex(16)
        with _db_connect() as con:
            _ensure_schema(con)
            with con.cursor() as cur:
                cur.execute('SELECT halted FROM luxalgo_stage6_execution_halt WHERE id=1')
                halt=cur.fetchone()
                if not halt or halt['halted'] is not True: raise ValueError('Persistent HALT changed')
                cur.execute('INSERT INTO luxalgo_stage12_readonly_snapshots (snapshot_id,position_count,open_order_count,trigger_order_count,assessment) VALUES (%s,%s,%s,%s,%s)',(snapshot_id,count,orders,triggers,assessment))
        with _db_connect() as con:
            with con.cursor() as cur:
                cur.execute('SELECT position_count,open_order_count,trigger_order_count,assessment FROM luxalgo_stage12_readonly_snapshots WHERE snapshot_id=%s',(snapshot_id,))
                saved=cur.fetchone()
                cur.execute('SELECT halted FROM luxalgo_stage6_execution_halt WHERE id=1')
                h=cur.fetchone()
        if not saved or (saved['position_count'],saved['open_order_count'],saved['trigger_order_count'],saved['assessment'])!=(count,orders,triggers,assessment) or not h or h['halted'] is not True:
            return jsonify(status='BLOCKED',error='Read-only snapshot recovery mismatch',exchange_orders=0),503
        detail=(f'Authenticated Gate.io GET snapshot: {count} open positions, {orders} open orders, '
            f'{triggers} trigger orders; PostgreSQL recovery and HALT verified. '
            'Snapshot visibility ONLY; no matching against actual fills or bot exchange order IDs; '
            'pagination/history not reconciled. No exchange orders placed')
        response=jsonify(status='PASS',stage='STAGE12_GATE_READONLY_SNAPSHOT',assessment=assessment,
                         position_count=count,open_order_count=orders,trigger_order_count=triggers,
                         real_exchange_reconciliation=False,exchange_orders=0,live_trading=False,detail=detail)
        response.headers['Cache-Control']='private, no-store'
        return response,200
    except (psycopg.Error,RuntimeError,HTTPError,URLError,TimeoutError,ValueError,TypeError,KeyError,OSError,json.JSONDecodeError) as exc:
        app.logger.warning('Stage 12 read-only snapshot blocked: %s',type(exc).__name__)
        return jsonify(status='BLOCKED',error='Gate.io or PostgreSQL read-only snapshot unavailable/incomplete; fail closed',exchange_orders=0),503


# Stage 13: recovery and failure-handling DIAGNOSTICS ONLY. Nothing here submits
# an exchange request. This is not a live uptime/chaos or order-recovery test.
def _stage13_decide_snapshot(age_seconds, api_ok, db_ok, halt_on):
    # Strict type validation avoids accepting bools/NaN as valid ages.
    if type(age_seconds) not in (int,float) or not math.isfinite(age_seconds) or age_seconds<0:
        return 'BLOCKED_INVALID_AGE'
    if type(api_ok) is not bool or type(db_ok) is not bool or type(halt_on) is not bool:
        return 'BLOCKED_MALFORMED_STATUS'
    if not halt_on:
        return 'BLOCKED_HALT_NOT_SET'
    if not db_ok:
        return 'BLOCKED_DATABASE'
    if not api_ok:
        return 'BLOCKED_API'
    if age_seconds>90:
        return 'BLOCKED_STALE_DATA'
    return 'OBSERVABLE_ONLY'

@app.post('/terminal/stage13-recovery-test')
def terminal_stage13_recovery_test():
    if not _terminal_permitted(): return _terminal_guard()
    if not _csrf_valid():
        return jsonify(status='BLOCKED',error='Session verification failed',exchange_orders=0),403
    if os.getenv('LIVE_TRADING_ENABLED','false').strip().lower()!='false':
        return jsonify(status='BLOCKED',error='Live execution must remain disabled',exchange_orders=0),503
    fixtures=[
      (0,True,True,True,'OBSERVABLE_ONLY'),
      (91,True,True,True,'BLOCKED_STALE_DATA'),
      (0,False,True,True,'BLOCKED_API'),
      (0,True,False,True,'BLOCKED_DATABASE'),
      (0,True,True,False,'BLOCKED_HALT_NOT_SET'),
      (float('nan'),True,True,True,'BLOCKED_INVALID_AGE'),
      (0,'true',True,True,'BLOCKED_MALFORMED_STATUS'),
    ]
    if any(_stage13_decide_snapshot(*case[:4])!=case[4] for case in fixtures):
        return jsonify(status='BLOCKED',error='Failure fixture classification mismatch',exchange_orders=0),503
    diagnostic_id='st13_'+secrets.token_hex(16)
    try:
        with _db_connect() as con:
            _ensure_schema(con)
            with con.cursor() as cur:
                cur.execute('SELECT halted FROM luxalgo_stage6_execution_halt WHERE id=1')
                halt=cur.fetchone()
                if not halt or halt['halted'] is not True:
                    return jsonify(status='BLOCKED',error='Future-execution HALT not confirmed',exchange_orders=0),503
                cur.execute("INSERT INTO luxalgo_stage13_recovery_diagnostics(diagnostic_id,observed_at,status) VALUES (%s,NOW(),'DIAGNOSTIC_ONLY')",(diagnostic_id,))
        # A NEW database connection exercises durable read-after-reconnect only.
        with _db_connect() as con:
            with con.cursor() as cur:
                cur.execute('SELECT observed_at, status FROM luxalgo_stage13_recovery_diagnostics WHERE diagnostic_id=%s',(diagnostic_id,))
                saved=cur.fetchone()
                cur.execute('SELECT halted FROM luxalgo_stage6_execution_halt WHERE id=1')
                halt=cur.fetchone()
                cur.execute("SELECT COUNT(*) AS n FROM luxalgo_stage8_ack_diagnostics WHERE state='ACK_UNKNOWN'")
                unknown=cur.fetchone()['n']
        if not saved or saved['status']!='DIAGNOSTIC_ONLY' or not halt or halt['halted'] is not True:
            return jsonify(status='BLOCKED',error='Database recovery/HALT mismatch',exchange_orders=0),503
        age=(datetime.now(timezone.utc)-saved['observed_at']).total_seconds()
        if _stage13_decide_snapshot(max(0,age),True,True,True)!='OBSERVABLE_ONLY':
            return jsonify(status='BLOCKED',error='Recovered diagnostic timestamp stale',exchange_orders=0),503
        detail=('7 failure fixtures (fresh/stale data, API outage, database outage, HALT OFF, malformed status/age) passed; '
                'new PostgreSQL connection recovered diagnostic, persistent HALT confirmed; '
                f'{unknown} simulated unknown-ack rows observed, NOT retransmitted. '
                'No real process restart, uptime window, exchange outage or order recovery tested')
        response=jsonify(status='PASS',stage='STAGE13_OPERATIONAL_FAILURE_FIXTURES',
                         fixture_count=len(fixtures),durable_readback=True,persistent_halt=True,
                         real_restart_tested=False,exchange_orders=0,live_trading=False,detail=detail)
        response.headers['Cache-Control']='private, no-store'
        return response,200
    except (psycopg.Error,RuntimeError,OSError,ValueError,TypeError):
        return jsonify(status='BLOCKED',error='PostgreSQL recovery unavailable; fail closed',exchange_orders=0),503

# Stage 14: final, isolated end-to-end *synthetic* paper workflow.
# Production webhook receipts, paper positions and exchange endpoints are untouched.
def _stage14_paper_case(side, entry, stop, take_profit, observed):
    try:
        if side not in ('LONG','SHORT'):
            return 'BLOCKED_INVALID_SIDE'
        values=[Decimal(str(x)) for x in (entry,stop,take_profit,observed)]
        if any(not x.is_finite() or x<=0 for x in values):
            return 'BLOCKED_INVALID_PRICE'
        e,s,t,p=values
        if side=='LONG' and not(s<e<t) or side=='SHORT' and not(t<e<s):
            return 'BLOCKED_INVALID_LEVELS'
        # Conservative tie resolution when observing one sampled price only.
        if side=='LONG':
            return 'PAPER_SL' if p<=s else 'PAPER_TP' if p>=t else 'PAPER_OPEN'
        return 'PAPER_SL' if p>=s else 'PAPER_TP' if p<=t else 'PAPER_OPEN'
    except (InvalidOperation,TypeError,ValueError):
        return 'BLOCKED_INVALID_PRICE'

@app.post('/terminal/stage14-final-paper-test')
def terminal_stage14_final_paper_test():
    if not _terminal_permitted(): return _terminal_guard()
    if not _csrf_valid():
        return jsonify(status='BLOCKED',error='Session verification failed',exchange_orders=0),403
    if os.getenv('LIVE_TRADING_ENABLED','false').strip().lower()!='false':
        return jsonify(status='BLOCKED',error='Live trading configuration must remain OFF',exchange_orders=0),503
    cases=[
        ('LONG',100,97,106,100,'PAPER_OPEN'),
        ('LONG',100,97,106,107,'PAPER_TP'),
        ('LONG',100,97,106,96,'PAPER_SL'),
        ('SHORT',100,103,94,93,'PAPER_TP'),
        ('SHORT',100,103,94,104,'PAPER_SL'),
        ('SHORT',100,103,94,100,'PAPER_OPEN'),
        ('LONG',100,102,106,105,'BLOCKED_INVALID_LEVELS'),
        ('SHORT',100,97,94,95,'BLOCKED_INVALID_LEVELS'),
        ('LONG',100,97,106,float('nan'),'BLOCKED_INVALID_PRICE'),
        ('SIDEWAYS',100,97,106,100,'BLOCKED_INVALID_SIDE'),
    ]
    if any(_stage14_paper_case(*c[:5])!=c[5] for c in cases):
        return jsonify(status='BLOCKED',error='Paper-workflow fixture mismatch',exchange_orders=0),503
    run_id='st14_'+secrets.token_hex(16)
    rows=[(run_id,'case_'+str(i),side,_stage14_paper_case(side,e,s,t,p),50)
          for i,(side,e,s,t,p,expected) in enumerate(cases) if expected.startswith('PAPER_')]
    phase='connect_and_schema'
    try:
        with _db_connect() as con:
            _ensure_schema(con)
            phase='halt_and_fixture_writes'
            with con.cursor() as cur:
                cur.execute('SELECT halted FROM luxalgo_stage6_execution_halt WHERE id=1')
                h=cur.fetchone()
                if not h or h['halted'] is not True:
                    return jsonify(status='BLOCKED',error='Persistent future-execution HALT not active',exchange_orders=0),503
                # Only Stage14 diagnostic rows; no real signal or order tables updated.
                for record in rows:
                    cur.execute('''INSERT INTO luxalgo_stage14_final_diagnostics
                        (run_id,event_id,side,state,risk_bps) VALUES (%s,%s,%s,%s,%s)''',record)
                # Duplicate event must be rejected by the durable unique key.
                cur.execute('''INSERT INTO luxalgo_stage14_final_diagnostics
                    (run_id,event_id,side,state,risk_bps) VALUES (%s,%s,%s,%s,%s)
                    ON CONFLICT (run_id,event_id) DO NOTHING RETURNING event_id''',rows[0])
                if cur.fetchone() is not None:
                    return jsonify(status='BLOCKED',error='Duplicate event not rejected',exchange_orders=0),503
        phase='fresh_connection_readback'
        with _db_connect() as con:
            with con.cursor() as cur:
                cur.execute('''SELECT event_id,side,state,risk_bps FROM luxalgo_stage14_final_diagnostics
                               WHERE run_id=%s ORDER BY event_id''',(run_id,))
                persisted=cur.fetchall()
                cur.execute('SELECT halted FROM luxalgo_stage6_execution_halt WHERE id=1')
                h=cur.fetchone()
        phase='readback_validation'
        expected=sorted((event,side,state,risk) for _,event,side,state,risk in rows)
        actual=sorted((r['event_id'],r['side'],r['state'],r['risk_bps']) for r in persisted)
        if actual!=expected or not h or h['halted'] is not True or any(risk>50 for _,_,_,_,risk in rows):
            return jsonify(status='BLOCKED',error='Durable paper audit/HALT/risk mismatch',exchange_orders=0),503
        detail=('10 synthetic cases: LONG/SHORT open, SL, TP, invalid geometry/price/side; '
                'duplicate event rejected, <=0.5% fixture risk, PostgreSQL fresh-connection recovery '
                'and persistent HALT verified. Live execution readiness remains BLOCKED. '
                'No TradingView delivery, real fill, protective order, true uptime or exchange recovery tested')
        response=jsonify(status='PASS',stage='STAGE14_FINAL_SYNTHETIC_PAPER_AUDIT',
                         fixture_count=len(cases),persisted_cases=len(rows),duplicate_rejected=True,
                         database_recovered=True,halted=True,live_execution_ready=False,
                         real_end_to_end_tested=False,exchange_orders=0,live_trading=False,detail=detail)
        response.headers['Cache-Control']='private, no-store'
        return response,200
    except (psycopg.Error,RuntimeError,OSError,ValueError,TypeError) as exc:
        # Sanitized diagnostics: no SQL text, credentials, private account data, or exception message.
        # Gives the operator a concrete failure location and stable error classification.
        code=getattr(exc, 'sqlstate', None)
        if not isinstance(code,str) or not re.fullmatch(r'[0-9A-Z]{5}',code):
            code='NONE'
        kind=type(exc).__name__
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}',kind):
            kind='UnknownError'
        return jsonify(status='BLOCKED',error='Stage 14 audit failed safely',
                       diagnostic_phase=phase,error_type=kind,sqlstate=code,
                       detail=f'Audit blocked at {phase}; {kind}, SQLSTATE {code}. Live trading stays OFF',
                       exchange_orders=0,live_trading=False),503

@app.post('/terminal/stage15-real-paper-audit')
def terminal_stage15_real_paper_audit():
    """Read-only inspection of ACTUAL stored webhooks, never inserts diagnostics or exchange orders.

    Historical receipt visibility is not proof of TradingView provenance or real fills.
    """
    if not _terminal_permitted(): return _terminal_guard()
    if not _csrf_valid():
        return jsonify(status='BLOCKED',error='Session verification failed',exchange_orders=0),403
    if os.getenv('LIVE_TRADING_ENABLED','false').strip().lower()!='false':
        return jsonify(status='BLOCKED',error='Live-trading setting must be false',exchange_orders=0),503
    try:
        with _db_connect() as con:
            _ensure_schema(con)
            with con.cursor() as cur:
                cur.execute('SELECT halted FROM luxalgo_stage6_execution_halt WHERE id=1')
                halt=cur.fetchone()
                if not halt or halt['halted'] is not True:
                    return jsonify(status='BLOCKED',detail='Persistent execution HALT not verified',exchange_orders=0),503
                cur.execute('SELECT enabled FROM luxalgo_bot_control WHERE id=1')
                bot=cur.fetchone()
                if bot is None:
                    return jsonify(status='BLOCKED',detail='Paper bot control record unavailable',exchange_orders=0),503
                cur.execute("""SELECT count(*) AS n, count(DISTINCT event_id) AS distinct_n
                    FROM luxalgo_signal_receipts""")
                counts=cur.fetchone()
                cur.execute("""SELECT i.event_id,i.symbol,i.side,i.timeframe,
                    l.status AS levels_status, q.state AS queue_state,
                    s.status AS simulated_status, p.state AS tracking_state
                    FROM luxalgo_dry_run_intents i
                    LEFT JOIN luxalgo_signal_levels l ON l.event_id=i.event_id
                    LEFT JOIN luxalgo_auto_paper_queue q ON q.event_id=i.event_id
                    LEFT JOIN luxalgo_simulated_orders s ON s.event_id=i.event_id
                    LEFT JOIN luxalgo_paper_tracking p ON p.event_id=i.event_id
                    ORDER BY i.received_at DESC LIMIT 100""")
                rows=cur.fetchall()
        # Classify actual lifecycle inconsistencies separately from historical
        # signal metadata gaps. Missing metadata remains visible and is NOT repaired.
        invalid=[];missing_levels=[];incomplete_levels=[];historical_simulated_gaps=[]
        for r in rows:
            if r['levels_status'] is None:
                missing_levels.append(r['event_id'])
                if r['simulated_status']=='SIMULATED_ONLY':
                    historical_simulated_gaps.append(r['event_id'])
            elif r['levels_status']!='VALID':
                incomplete_levels.append(r['event_id'])
            if (r['side'] not in ('LONG','SHORT') or r['timeframe'] not in ('5m','15m') or
                r['levels_status'] not in (None,'VALID','INVALID','MISSING') or
                r['queue_state'] not in (None,'PENDING','CREATED','REJECTED') or
                r['simulated_status'] not in (None,'SIMULATED_ONLY') or
                r['tracking_state'] not in (None,'OPEN','SL','TP','UNVERIFIABLE') or
                (r['queue_state']=='CREATED' and r['simulated_status']!='SIMULATED_ONLY') or
                (r['simulated_status']=='SIMULATED_ONLY' and r['queue_state']!='CREATED') or
                (r['tracking_state'] is not None and r['simulated_status']!='SIMULATED_ONLY')):
                invalid.append(r['event_id'])
        breakdown={name:sum(r['queue_state']==name for r in rows)
                   for name in ('PENDING','CREATED','REJECTED')}
        simulated=sum(r['simulated_status']=='SIMULATED_ONLY' for r in rows)
        with_levels=sum(r['levels_status']=='VALID' for r in rows)
        if invalid:
            state='BLOCKED';detail=f'{len(invalid)} core paper lifecycle conflict(s) in latest {len(rows)} intents; review required'
        elif not rows or simulated==0:
            state='INSUFFICIENT_DATA';detail=('No verified end-to-end webhook-to-paper simulation among latest intents. '
                f'Missing level records: {len(missing_levels)}; non-valid level records: {len(incomplete_levels)}. '
                'Historical receipts do not prove TradingView origin or paper execution')
        elif missing_levels or incomplete_levels:
            state='PASS_WITH_GAPS';detail=(f'Core lifecycle conflicts: 0 among {len(rows)} intents; '
                f'{simulated} simulated, {with_levels} valid explicit level sets; '
                f'{len(missing_levels)} missing level records, {len(incomplete_levels)} non-valid level records, '
                f'{len(historical_simulated_gaps)} older simulated trades without level records. '
                'Historical gaps were not altered; full integrity is NOT confirmed')
        else:
            state='PASS';detail=(f'No detected lifecycle or signal-level record gaps across {len(rows)} recent intents; '
                f'{simulated} simulated, {with_levels} valid level sets, '
                f'queue created={breakdown["CREATED"]}, pending={breakdown["PENDING"]}, rejected={breakdown["REJECTED"]}. '
                'Does NOT prove TradingView origin, real fills, protective orders or 24/7 runtime')
        response=jsonify(status=state,detail=detail,receipt_count=counts['n'],
            distinct_receipt_events=counts['distinct_n'],intents_inspected=len(rows),
            simulated_count=simulated,queue_counts=breakdown,core_lifecycle_conflicts=len(invalid),missing_level_records=len(missing_levels),nonvalid_level_records=len(incomplete_levels),historical_simulated_missing_levels=len(historical_simulated_gaps),paper_bot_enabled=bool(bot['enabled']),
            persistent_halt=True,live_trading=False,real_exchange_reconciled=False,exchange_orders=0)
        response.headers['Cache-Control']='private, no-store'
        return response, 503 if state=='BLOCKED' else 200
    except (psycopg.Error,RuntimeError,OSError,ValueError,TypeError):
        app.logger.exception('Stage 15 read-only audit unavailable')
        return jsonify(status='BLOCKED',detail='Database unavailable or unexpected record format; fail closed',exchange_orders=0),503


@app.post('/terminal/stage15-record-issues')
def terminal_stage15_record_issues():
    # Authenticated read-only report; no modifications or order actions.
    if not _terminal_permitted(): return _terminal_guard()
    if not _csrf_valid():
        return jsonify(status='BLOCKED',detail='Session verification failed',exchange_orders=0),403
    if os.getenv('LIVE_TRADING_ENABLED','false').strip().lower()!='false':
        return jsonify(status='BLOCKED',detail='Live trading must remain disabled',exchange_orders=0),503
    try:
        with _db_connect() as con:
            # Existing schema is managed by application initialization/earlier audits.
            # This inspection intentionally runs SELECT statements only.
            with con.cursor() as cur:
                cur.execute('SELECT halted FROM luxalgo_stage6_execution_halt WHERE id=1')
                halt=cur.fetchone()
                if not halt or halt['halted'] is not True:
                    return jsonify(status='BLOCKED',detail='Persistent HALT not verified',exchange_orders=0),503
                cur.execute('''SELECT i.event_id,i.symbol,i.side,i.timeframe,
                    l.status AS levels_status,q.state AS queue_state,
                    s.status AS simulated_status,p.state AS tracking_state
                    FROM luxalgo_dry_run_intents i
                    LEFT JOIN luxalgo_signal_levels l ON l.event_id=i.event_id
                    LEFT JOIN luxalgo_auto_paper_queue q ON q.event_id=i.event_id
                    LEFT JOIN luxalgo_simulated_orders s ON s.event_id=i.event_id
                    LEFT JOIN luxalgo_paper_tracking p ON p.event_id=i.event_id
                    ORDER BY i.received_at DESC LIMIT 100''')
                rows=cur.fetchall()
        records=[];reason_counts={};core_conflicts=0;missing_only=0
        for r in rows:
            reasons=[]
            if r['side'] not in ('LONG','SHORT'): reasons.append('INVALID_SIDE')
            if r['timeframe'] not in ('5m','15m'): reasons.append('INVALID_TIMEFRAME')
            if r['levels_status'] is None: reasons.append('MISSING_LEVEL_RECORD')
            elif r['levels_status'] not in ('VALID','INVALID','MISSING'): reasons.append('INVALID_LEVEL_STATUS')
            if r['queue_state'] not in (None,'PENDING','CREATED','REJECTED'): reasons.append('INVALID_QUEUE_STATE')
            if r['simulated_status'] not in (None,'SIMULATED_ONLY'): reasons.append('INVALID_SIM_ORDER_STATUS')
            if r['tracking_state'] not in (None,'OPEN','SL','TP','UNVERIFIABLE'): reasons.append('INVALID_TRACKING_STATE')
            if r['queue_state']=='CREATED' and r['simulated_status']!='SIMULATED_ONLY': reasons.append('CREATED_QUEUE_WITHOUT_SIM_ORDER')
            if r['simulated_status']=='SIMULATED_ONLY' and r['queue_state']!='CREATED': reasons.append('SIM_ORDER_WITHOUT_CREATED_QUEUE')
            if r['tracking_state'] is not None and r['simulated_status']!='SIMULATED_ONLY': reasons.append('TRACKING_WITHOUT_SIM_ORDER')
            if not reasons: continue
            for reason in reasons:reason_counts[reason]=reason_counts.get(reason,0)+1
            if reasons==['MISSING_LEVEL_RECORD']:missing_only+=1
            else:core_conflicts+=1
            records.append(dict(event_id=str(r['event_id'])[:128],symbol=str(r['symbol'])[:48],
                side=str(r['side'])[:24],timeframe=str(r['timeframe'])[:24],
                levels_status=r['levels_status'],queue_state=r['queue_state'],
                simulated_status=r['simulated_status'],tracking_state=r['tracking_state'],reasons=reasons))
        response=jsonify(status='REPORT_ONLY',intents_inspected=len(rows),
            flagged_by_original_rules=len(records),core_lifecycle_conflicts=core_conflicts,
            missing_levels_only=missing_only,reason_counts=reason_counts,records=records,
            historical_rows_modified=0,exchange_orders=0,live_trading=False,persistent_halt=True)
        response.headers['Cache-Control']='private, no-store'
        return response
    except (psycopg.Error,RuntimeError,OSError,ValueError,TypeError):
        app.logger.exception('Stage 15 detail inspection unavailable')
        return jsonify(status='BLOCKED',detail='Read-only inspection unavailable; fail closed',exchange_orders=0),503

@app.get('/terminal/live-readiness')
def terminal_live_readiness():
    if not _terminal_permitted():
        return _terminal_guard()
    checks = [
        dict(name='Dedicated protected live-order engine', ready=False,
             detail='No order-submission route or tested signing/contract-sizing engine exists'),
        dict(name='Exchange-side protective SL and TP', ready=False,
             detail='Must be acknowledged by Gate.io and survive browser/server shutdown'),
        dict(name='Post-fill exchange reconciliation', ready=False,
             detail='Must reconcile entry, fills, position mode and protective orders'),
        dict(name='Durable order idempotency and restart recovery', ready=False,
             detail='PostgreSQL dry-run IDs are not a live exchange order ledger'),
        dict(name='Global risk and one-position enforcement', ready=False,
             detail='Must enforce account-wide exposure atomically before exchange writes'),
        dict(name='Emergency stop and position handling', ready=False,
             detail='Current BOT OFF stops new paper simulations only'),
        dict(name='Reliable always-on service and API health', ready=False,
             detail='Validate Render uptime, PostgreSQL, webhook delivery, queues and market-data freshness'),
    ]
    try:
        data = _gate_futures_account_summary()
        checks.append(dict(name='Read-only Gate.io USDT equity', ready=True,
                           detail='Authenticated equity endpoint responded successfully'))
        # Report availability, not the private balance, in a readiness checklist.
        equity_verified = True
    except (RuntimeError, HTTPError, URLError, TimeoutError, ValueError, TypeError, KeyError, OSError):
        checks.append(dict(name='Read-only Gate.io USDT equity', ready=False,
                           detail='Account verification currently unavailable'))
        equity_verified = False
    try:
        positions = _gate_read_only_positions()
        checks.append(dict(name='Read-only open positions', ready=True,
                           detail='Exchange returned %d verified positions' % len(positions)))
    except (RuntimeError, HTTPError, URLError, TimeoutError, ValueError, TypeError, KeyError, OSError):
        checks.append(dict(name='Read-only open positions', ready=False,
                           detail='Cannot validate the current position inventory'))
    try:
        with _db_connect() as con:
            _ensure_schema(con)
            with con.cursor() as cur:
                bot_enabled = _bot_enabled(cur)
        checks.append(dict(name='Persistent paper-bot state', ready=True,
                           detail='Paper bot is '+('ON' if bot_enabled else 'OFF')+'; not a live-order kill switch'))
    except (psycopg.Error, RuntimeError, OSError):
        checks.append(dict(name='Persistent paper-bot state', ready=False,
                           detail='PostgreSQL cannot verify current paper-bot state'))
    live_env = os.getenv('LIVE_TRADING_ENABLED', 'false').strip().lower()
    checks.append(dict(name='Safe environment flag', ready=live_env == 'false',
                       detail='Keep LIVE_TRADING_ENABLED=false; no order routes exist'))
    response=jsonify(status='BLOCKED',safe_to_trade=False,orders_enabled=False,
                     live_trading=False,orders_placed=0,equity_verified=equity_verified,
                     checks=checks,notice='Readiness inspection only. Never change API write permissions on this basis.')
    response.headers['Cache-Control']='private, no-store'
    return response


# Step 415: read-only and fail-closed risk inspection; never authorizes an order.
@app.get('/terminal/live-risk-check')
def terminal_live_risk_check():
    if not _terminal_permitted():
        return _terminal_guard()
    checks=[]
    safe_env = os.getenv('LIVE_TRADING_ENABLED','false').strip().lower()=='false'
    checks.append(dict(name='Live trading remains disabled', passed=safe_env,
                       detail='No Gate.io order requests are available in this version'))
    try:
        account=_gate_futures_account_summary()
        equity=account['equity_usdt']
        available=account['available_usdt']
        if not all(math.isfinite(float(x)) for x in (equity,available)) or equity<=0 or available<0:
            raise ValueError('Invalid account data')
        risk_budget=round(equity * .03, 8)
        checks.append(dict(name='Verified Gate.io account equity',passed=True,
                           detail='Read-only equity and available funds verified'))
    except (RuntimeError,HTTPError,URLError,TimeoutError,ValueError,KeyError,TypeError,OSError):
        equity=None; available=None; risk_budget=None
        checks.append(dict(name='Verified Gate.io account equity',passed=False,
                           detail='Unavailable or invalid; fail closed'))
    try:
        positions=_gate_read_only_positions()
        position_count=len(positions)
        checks.append(dict(name='Maximum one existing open position',passed=position_count==0,
                           detail=f'{position_count} exchange positions found; new entry blocked if any already open'))
        # Gate.io returned leverage labels are presentation data and may not fully
        # describe hedge-mode exposure. Never approve leverage from these strings.
    except (RuntimeError,HTTPError,URLError,TimeoutError,ValueError,KeyError,TypeError,OSError):
        position_count=None
        checks.append(dict(name='Maximum one existing open position',passed=False,
                           detail='Exchange position inventory unavailable; fail closed'))
    try:
        with _db_connect() as con:
            _ensure_schema(con)
            with con.cursor() as cur:
                cur.execute('SELECT COUNT(*) AS c FROM luxalgo_live_order_guard_audit')
                audited=int(cur.fetchone()['c'])
        checks.append(dict(name='PostgreSQL guard audit available',passed=True,
                           detail=f'{audited} blocked audit records; not a live order ledger'))
    except (psycopg.Error,RuntimeError,OSError):
        checks.append(dict(name='PostgreSQL guard audit available',passed=False,
                           detail='Database unavailable; fail closed'))
    checks.append(dict(name='Leverage maximum 5x',passed=False,
                       detail='5x is the planned ceiling; a live order/contract leverage validation does not exist'))
    checks.append(dict(name='Atomic account-wide risk and exposure lock',passed=False,
                       detail='3% is a budget, not enforced for simultaneous orders or pending exchange requests'))
    checks.append(dict(name='Exchange protective stops and take profit',passed=False,
                       detail='No verified exchange-side SL/TP placement or recovery'))
    response=jsonify(status='BLOCKED',safe_to_trade=False,orders_placed=0,live_trading=False,
                     limits=dict(max_new_positions=1,max_leverage=5,risk_percent=3),
                     snapshot=dict(verified_equity_usdt=equity,verified_available_usdt=available,
                                   risk_budget_usdt=risk_budget,open_positions=position_count),
                     checks=checks,
                     notice='Read-only observation, not atomic authorization. No live trading is available.')
    response.headers['Cache-Control']='private, no-store'
    return response


@app.get('/terminal/futures-equity')
def terminal_futures_equity():
    if not _terminal_permitted():
        return _terminal_guard()
    try:
        response = jsonify(_gate_futures_account_summary())
        response.headers['Cache-Control'] = 'private, no-store'
        return response
    except RuntimeError:
        return jsonify(error='Gate.io read-only credentials unavailable',orders_enabled=False),503
    except HTTPError as exc:
        app.logger.warning('Gate equity HTTP status %s', exc.code)
        return jsonify(error='Gate.io rejected the read-only equity request',orders_enabled=False),502
    except (URLError,TimeoutError,ValueError,TypeError,KeyError,OSError) as exc:
        app.logger.warning('Gate equity not available: %s',type(exc).__name__)
        return jsonify(error='Account equity unavailable for this account type or connection',orders_enabled=False),502


@app.get('/terminal/testnet-open-orders')
def terminal_testnet_open_orders():
    """Gate Futures Testnet unfilled orders; authenticated GET only."""
    if not _terminal_permitted():
        return _terminal_guard()
    key=os.getenv('GATE_TESTNET_API_KEY','').strip()
    secret=os.getenv('GATE_TESTNET_API_SECRET','').strip()
    if not key or not secret:
        return jsonify(error='Testnet API credentials missing',orders_enabled=False),503
    path='/api/v4/futures/usdt/orders'
    query=urlencode({'status':'open','limit':50})
    stamp=str(int(time.time()))
    hashed=hashlib.sha512(b'').hexdigest()
    signed='\n'.join(('GET',path,query,hashed,stamp))
    signature=hmac.new(secret.encode(),signed.encode(),hashlib.sha512).hexdigest()
    req=Request('https://api-testnet.gateapi.io'+path+'?'+query,method='GET',headers={
        'KEY':key,'Timestamp':stamp,'SIGN':signature,'Accept':'application/json'})
    try:
        with urlopen(req,timeout=8) as response:
            orders=json.loads(response.read(150000))
        if not isinstance(orders,list):
            raise ValueError('Invalid open orders response')
        cleaned=[]
        for o in orders[:50]:
            if not isinstance(o,dict):
                continue
            cleaned.append(dict(contract=str(o.get('contract') or ''),
                order_id=str(o.get('id_string') or o.get('id') or ''),
                size=str(o.get('size') or ''),price=str(o.get('price') or ''),
                remaining=str(o.get('left') or ''),tif=str(o.get('tif') or ''),
                status=str(o.get('status') or '')))
        result=jsonify(environment='GATE_FUTURES_TESTNET',orders=cleaned,
            count=len(cleaned),orders_enabled=False,live_trading=False)
        result.headers['Cache-Control']='private, no-store'
        return result
    except HTTPError as exc:
        return jsonify(error='Testnet orders API rejected request',http_status=exc.code,orders_enabled=False),502
    except (URLError,TimeoutError,OSError,ValueError,TypeError):
        return jsonify(error='Testnet open orders unavailable',orders_enabled=False),502


@app.get('/terminal/testnet-live-readiness')
def terminal_testnet_live_readiness():
    """Authenticated live Testnet GET-only preflight, without order submission."""
    if not _terminal_permitted():
        return _terminal_guard()
    try:
        from testnet_readiness import inspect_testnet_readiness
        report=inspect_testnet_readiness()
        report['production_trading_enabled']=False
        report['orders_placed']=0
        response=jsonify(report)
        response.headers['Cache-Control']='private, no-store'
        return response
    except (RuntimeError,OSError,ValueError,TypeError) as exc:
        app.logger.warning('Read-only Testnet readiness unavailable: %s',type(exc).__name__)
        return jsonify(ready=False,error='Live Testnet readiness check failed',
                       production_trading_enabled=False,orders_placed=0),503


@app.get('/terminal/testnet-readiness')
def terminal_testnet_readiness():
    """Signed-in private, read-only snapshot of Testnet readiness."""
    if not _terminal_permitted():
        return _terminal_guard()
    try:
        with _db_connect() as conn:
            _ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT state,details,checked_at FROM luxalgo_testnet_readiness WHERE id=1")
                saved=cur.fetchone()
        if saved is None:
            return jsonify(ready=False,blockers=['READINESS_CHECK_NOT_YET_RUN'],
                           production_trading_enabled=False),503
        payload=dict(saved['details'])
        payload['snapshot_state']=saved['state']
        payload['checked_at']=saved['checked_at'].isoformat()
        payload['production_trading_enabled']=False
        response=jsonify(payload)
        response.headers['Cache-Control']='private, no-store'
        return response
    except (psycopg.Error,RuntimeError,OSError,ValueError,TypeError):
        return jsonify(ready=False,blockers=['READINESS_DATABASE_UNAVAILABLE'],
                       production_trading_enabled=False),503


@app.get('/terminal/paper-open-direct')
def terminal_paper_open_direct():
    """Fast read-only paper open-position feed. No external market calls or writes.

    This feed must not be interpreted as exchange-side positions or fills.
    It is isolated from the slower historical candle review service.
    """
    if not _terminal_permitted():
        return _terminal_guard()
    try:
        with _db_connect() as con:
            with con.cursor() as cur:
                cur.execute("""SELECT o.event_id,o.symbol,o.side,o.timeframe,
                                     o.created_at,o.details,t.state
                    FROM luxalgo_simulated_orders o
                    JOIN luxalgo_paper_tracking t ON t.event_id=o.event_id
                    WHERE t.state='OPEN' AND o.status='SIMULATED_ONLY'
                    ORDER BY o.created_at DESC LIMIT 30""")
                found=cur.fetchall()
        rows=[]
        for r in found:
            d=r['details'] if isinstance(r['details'],dict) else {}
            rows.append(dict(event_id=r['event_id'],symbol=r['symbol'],
                side=r['side'],timeframe=r['timeframe'],
                created_at=r['created_at'].isoformat(),
                entry=d.get('reference_entry'),stop_loss=d.get('stop_loss'),
                take_profit=d.get('take_profit'),size_contracts=d.get('size_contracts'),
                state=r['state'],execution='SIMULATED_ONLY'))
        response=jsonify(positions=rows,visible_count=len(rows),
            source='POSTGRES_PAPER_SIMULATION',exchange_fills_verified=False,
            orders_placed=0,checked_at=datetime.now(timezone.utc).isoformat())
        response.headers['Cache-Control']='private, no-store'
        return response
    except (psycopg.Error,RuntimeError,OSError,ValueError,TypeError):
        app.logger.warning('Stored paper positions query unavailable',exc_info=False)
        return jsonify(error='Stored paper positions temporarily unavailable',
                       exchange_fills_verified=False,orders_placed=0),503


@app.get('/terminal/testnet-signal-trace')
def terminal_testnet_signal_trace():
    """Authenticated, read-only distinction between webhook/paper and exchange fills.

    A LuxAlgo receipt, paper simulation, or handoff entry must never be
    presented as proof that Gate Testnet accepted an order.
    """
    if not _terminal_permitted():
        return _terminal_guard()
    try:
        with _db_connect() as con:
            _ensure_schema(con)
            with con.cursor() as cur:
                cur.execute("""SELECT r.event_id,r.symbol,r.side,r.timeframe,r.received_at,
                    p.status AS paper_status,
                    h.status AS handoff_status,h.reason AS handoff_reason,
                    j.state AS testnet_state
                    FROM luxalgo_signal_receipts r
                    LEFT JOIN luxalgo_simulated_orders p ON p.event_id=r.event_id
                    LEFT JOIN luxalgo_testnet_execution_handoff h ON h.event_id=r.event_id
                    LEFT JOIN luxalgo_testnet_protected_journal j ON j.signal_id=r.event_id
                    ORDER BY r.id DESC LIMIT 1""")
                found=cur.fetchone()
        latest=None
        if found:
            latest=dict(event_id=found['event_id'],symbol=found['symbol'],
                side=found['side'],timeframe=found['timeframe'],
                received_at=found['received_at'].isoformat(),
                paper_status=found['paper_status'],
                handoff_status=found['handoff_status'],
                handoff_reason=found['handoff_reason'],
                testnet_state=found['testnet_state'])
        response=jsonify(latest_signal=latest,read_only=True,
                         production_trading_enabled=False)
        response.headers['Cache-Control']='private, no-store'
        return response
    except (psycopg.Error,RuntimeError,OSError,ValueError,TypeError,KeyError):
        return jsonify(error='Signal trace temporarily unavailable',
                       production_trading_enabled=False),503


@app.get('/terminal/testnet-protected-journal')
def terminal_testnet_protected_journal():
    """Authenticated read-only order journal, no exchange write or secret fields."""
    if not _terminal_permitted():
        return _terminal_guard()
    try:
        with _db_connect() as con:
            _ensure_schema(con)
            with con.cursor() as cur:
                cur.execute("""SELECT signal_id,contract,side,state,updated_at
                    FROM luxalgo_testnet_protected_journal
                    ORDER BY updated_at DESC LIMIT 25""")
                rows=cur.fetchall()
        report=[dict(signal_id=x['signal_id'],contract=x['contract'],
                     side=x['side'],state=x['state'],
                     updated_at=x['updated_at'].isoformat()) for x in rows]
        response=jsonify(environment='GATE_FUTURES_TESTNET',journal=report,
            testnet_automation_enabled=(
                os.getenv('GATE_TESTNET_PROTECTED_EXECUTION_ENABLED','false').lower()=='true' and
                os.getenv('GATE_TESTNET_WEBHOOK_AUTOMATION_ENABLED','false').lower()=='true'),
            live_trading=False)
        response.headers['Cache-Control']='private, no-store'
        return response
    except (psycopg.Error,RuntimeError,OSError,ValueError,TypeError,KeyError):
        return jsonify(error='Testnet journal temporarily unavailable',live_trading=False),503


@app.post('/terminal/testnet-manual-close')
def terminal_testnet_manual_close():
    """Explicit authenticated click: one Testnet-only reduce-only full close."""
    if not _terminal_permitted():
        return _terminal_guard()
    if not _csrf_valid():
        return jsonify(error='Invalid terminal session verification'),403
    data=request.get_json(silent=True) if request.is_json else None
    if not isinstance(data,dict) or set(data)!={'signal_id','contract','side','entry_order_id'}:
        return jsonify(error='Exact Testnet position identity required'),400
    try:
        from testnet_manual_close import request_testnet_close
        result=request_testnet_close(data['signal_id'],data['contract'],
                                     data['side'],data['entry_order_id'])
        code=200 if result.get('state')=='CLOSED_VERIFIED' else 409
        response=jsonify(**result,environment='GATE_FUTURES_TESTNET',
                         live_trading=False)
        response.headers['Cache-Control']='private, no-store'
        return response,code
    except (psycopg.Error,RuntimeError,OSError,ValueError,TypeError,KeyError):
        return jsonify(state='HALTED_UNKNOWN',error='Testnet closure not verified; inspect Gate.io before retry',
                       live_trading=False),503


@app.get('/terminal/testnet-position-cards')
def terminal_testnet_position_cards():
    """Private exchange-backed TESTNET cards; strictly GET and read-only."""
    if not _terminal_permitted():
        return _terminal_guard()
    try:
        positions=_gate_read_only_positions(testnet=True)
        with _db_connect() as con:
            _ensure_schema(con)
            with con.cursor() as cur:
                cur.execute("""SELECT signal_id,client_id,contract,side,state,payload,details
                               FROM luxalgo_testnet_protected_journal
                               WHERE state IN ('PROTECTED_VERIFIED','RECOVERING',
                                   'HALTED_UNKNOWN','HALTED_UNPROTECTED','CLOSED_VERIFIED')
                               ORDER BY updated_at DESC LIMIT 50""")
                journal_rows=list(cur.fetchall())
        from gate_testnet_broker import GateFuturesTestnetBroker
        from testnet_card_feed import build_testnet_position_cards
        # For each journal-matched position, native protection is freshly
        # verified against Gate Testnet. A verification error retains the
        # position card but clears its protection prices/status.
        cards=build_testnet_position_cards(positions,journal_rows,
                                          GateFuturesTestnetBroker())
        response=jsonify(environment='GATE_FUTURES_TESTNET',
                         positions=cards,count=len(cards),
                         source='EXCHANGE_READBACK_AND_PROTECTION_AUDIT',
                         fetched_at=int(time.time()),
                         orders_enabled=False,live_trading=False)
        response.headers['Cache-Control']='private, no-store'
        return response
    except (psycopg.Error,RuntimeError,OSError,ValueError,TypeError,KeyError):
        response=jsonify(error='Testnet position cards currently unavailable',
                         environment='GATE_FUTURES_TESTNET',orders_enabled=False)
        response.headers['Cache-Control']='private, no-store'
        return response,503


@app.get('/terminal/testnet-positions')
def terminal_testnet_positions():
    """Isolated Gate.io Futures TESTNET position readback. No order submission."""
    if not _terminal_permitted():
        return _terminal_guard()
    try:
        positions=_gate_read_only_positions(testnet=True)
        response=jsonify(environment='GATE_FUTURES_TESTNET',positions=positions,
            count=len(positions),orders_enabled=False,live_trading=False,
            fetched_at=int(time.time()))
        response.headers['Cache-Control']='private, no-store'
        return response
    except RuntimeError:
        return jsonify(error='Gate Testnet API credentials missing',orders_enabled=False),503
    except HTTPError as exc:
        return jsonify(error='Gate Testnet position API rejected the read-only request',
                       http_status=exc.code,orders_enabled=False),502
    except (URLError,TimeoutError,OSError,ValueError,TypeError,KeyError):
        return jsonify(error='Gate Testnet positions unavailable or invalid',orders_enabled=False),502


@app.get('/terminal/positions')
def terminal_positions():
    if not _terminal_permitted():
        return _terminal_guard()
    try:
        positions = _gate_read_only_positions()
        response = jsonify(positions=positions, count=len(positions),
                           unrealized_pnl_usdt=(round(sum(p['unrealized_pnl'] for p in positions), 8)
                                               if all(p['unrealized_pnl'] is not None for p in positions)
                                               else None),
                           fetched_at=int(time.time()), orders_enabled=False, live_trading=False)
        response.headers['Cache-Control'] = 'no-store'
        return response
    except RuntimeError:
        return jsonify(error='Gate.io read-only credentials unavailable', orders_enabled=False),503
    except HTTPError as exc:
        app.logger.warning('Gate positions HTTP status %s', exc.code)
        reason=('Gate.io credentials or Futures read permission rejected (HTTP 401/403)'
                if exc.code in (401,403) else
                'Gate.io rate limit reached (HTTP 429)' if exc.code==429 else
                'Gate.io position endpoint returned HTTP '+str(exc.code))
        return jsonify(error=reason,gate_http_status=exc.code,orders_enabled=False),502
    except (URLError, TimeoutError, OSError):
        app.logger.warning('Gate positions transport error')
        return jsonify(error='Gate.io position request timed out or network failed',
                       orders_enabled=False),502
    except (ValueError, KeyError, TypeError):
        app.logger.warning('Gate positions response format invalid')
        return jsonify(error='Gate.io position response incompatible with current parser',
                       orders_enabled=False),502

def _gate_atr_plan(symbol, direction, tf="15m"):
    """Read ONLY public, completed timeframe candles. Return an illustrative plan.

    Not order sizing: 3% risk requires verified available funds, instrument
    contract multiplier, minimum contract count, open positions, and margin rules.
    """
    if tf not in ('5m', '15m'):
        raise ValueError('Unsupported timeframe')
    seconds = 300 if tf == '5m' else 900
    contract = symbol[:-4] + '_USDT'
    url = ('https://api.gateio.ws/api/v4/futures/usdt/candlesticks?' +
           urlencode({'contract': contract, 'interval': tf, 'limit': 60}))
    req = Request(url, headers={'Accept': 'application/json', 'User-Agent': 'LuxAlgo-PRO-Readonly/1.0'}, method='GET')
    with urlopen(req, timeout=10) as res:
        candles = json.loads(res.read(131072))
    if not isinstance(candles, list):
        raise ValueError('Invalid Gate candle response')
    now = int(time.time())
    complete = []
    for c in candles:
        start = int(c['t'])
        if start + seconds > now:  # Exclude unfinished interval
            continue
        high, low, close = float(c['h']), float(c['l']), float(c['c'])
        if not all(math.isfinite(v) and v > 0 for v in (high,low,close)) or high < low:
            raise ValueError('Invalid OHLC data')
        complete.append((start, high, low, close))
    complete.sort(key=lambda x:x[0])
    complete = list({c[0]: c for c in complete}.values())
    complete.sort(key=lambda x:x[0])
    if len(complete) < 16 or any(b[0]-a[0] != seconds for a,b in zip(complete[-16:-1],complete[-15:])):
        raise ValueError('Insufficient consecutive completed candles')
    # Wilder ATR(14) computed over all available completed candles.
    true_ranges = [max(c[1]-c[2], abs(c[1]-prev[3]), abs(c[2]-prev[3]))
                   for prev,c in zip(complete, complete[1:])]
    atr = sum(true_ranges[:14]) / 14
    for tr in true_ranges[14:]:
        atr = (atr*13 + tr)/14
    entry = complete[-1][3]  # Last 15m closing price, NOT an execution price
    if direction == 'LONG':
        sl, tp1 = entry - 1.5*atr, entry + 2*atr
    else:
        sl, tp1 = entry + 1.5*atr, entry - 2*atr
    if min(entry,sl,tp1,atr) <= 0:
        raise ValueError('Plan would produce nonpositive prices')
    return {
        'contract':contract, 'side':direction, 'timeframe':tf,
        'reference':'most_recent_completed_'+tf+'_close_NOT_order_fill',
        'last_candle_start_utc':datetime.fromtimestamp(complete[-1][0], timezone.utc).isoformat(),
        'reference_entry':entry, 'atr_14_wilder':atr,
        'stop_loss_1_5_atr':sl, 'take_profit_1_2_atr':tp1,
        'planned_leverage':5,
        'intended_risk_fraction':0.005,
        'margin_mode':'CROSS', 'position_mode':'HEDGE',
        'position_size':'NOT_CALCULATED', 'exchange_orders_sent':0,
        'warning':'Planning only; prices are not executable quotes. No SL/TP orders exist.'
    }


def _execution_dry_run_preview(symbol, side, tf, hypothetical_equity, risk_percent):
    """Planning ONLY: use public Gate.io contract details, no order signing/writes."""
    from decimal import Decimal, ROUND_FLOOR, InvalidOperation
    if not re.fullmatch(r'[A-Z0-9]{3,25}USDT', symbol) or side not in ('LONG','SHORT') or tf not in ('5m','15m'):
        raise ValueError('Invalid symbol, side or timeframe')
    try:
        equity = Decimal(str(hypothetical_equity))
    except (InvalidOperation, ValueError):
        raise ValueError('Invalid hypothetical equity')
    if not equity.is_finite() or not Decimal('1') <= equity <= Decimal('10000000'):
        raise ValueError('Hypothetical equity must be 1 to 10,000,000 USDT')
    try:
        risk = Decimal(str(risk_percent))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('Invalid risk percentage')
    if not risk.is_finite() or risk < Decimal('0.5') or risk > Decimal('5') or (risk * 2) != (risk * 2).to_integral_value():
        raise ValueError('Risk percentage must be 0.5 to 5 in 0.5% steps')
    plan = _gate_atr_plan(symbol, side, tf)
    contract = plan['contract']
    path = '/api/v4/futures/usdt/contracts/' + contract
    req = Request('https://api.gateio.ws'+path,
                  headers={'Accept':'application/json','User-Agent':'LuxAlgo-PRO-DryRun/1.0'},method='GET')
    with urlopen(req,timeout=10) as resp:
        spec=json.loads(resp.read(65536))
    if not isinstance(spec,dict) or spec.get('name') != contract or spec.get('type') != 'direct':
        raise ValueError('Unverified or unsupported contract specification')
    def positive_decimal(name):
        try: x=Decimal(str(spec[name]))
        except (KeyError,InvalidOperation,ValueError): raise ValueError('Invalid '+name)
        if not x.is_finite() or x<=0: raise ValueError('Invalid '+name)
        return x
    multiplier=positive_decimal('quanto_multiplier')
    # Gate contract metadata can report an order_size_min of zero. This is not
    # a verified permission to place zero-sized or sub-one-contract orders.
    # Continue the READ-ONLY illustration with integer sizing, but flag the
    # exchange minimum as unverified rather than rejecting the entire pair.
    try:
        reported_min_size=Decimal(str(spec['order_size_min']))
    except (KeyError, InvalidOperation, ValueError, TypeError):
        raise ValueError('Invalid order_size_min')
    if not reported_min_size.is_finite() or reported_min_size<0:
        raise ValueError('Invalid order_size_min')
    minimum_unverified=(reported_min_size==0)
    # Integer-size previews always use at least one contract; this is an
    # illustrative floor, NOT an assertion of Gate.io's actual minimum.
    min_size=reported_min_size if not minimum_unverified else Decimal(1)
    max_size=positive_decimal('order_size_max')
    leverage_max=positive_decimal('leverage_max')
    if min_size>max_size: raise ValueError('Inconsistent Gate size limits')
    entry=Decimal(str(plan['reference_entry']))
    stop=Decimal(str(plan['stop_loss_1_5_atr']))
    take=Decimal(str(plan['take_profit_1_2_atr']))
    distance=abs(entry-stop)
    if distance<=0 or (side=='LONG' and not stop<entry<take) or (side=='SHORT' and not take<entry<stop):
        raise ValueError('Invalid SL/TP geometry')
    budget=equity*risk/Decimal('100')
    loss_per_contract=distance*multiplier
    raw_size=budget/loss_per_contract
    # Conservative integer rounding. Some contracts allow decimals but require
    # verified lot increment; don't fabricate a decimal-lot increment.
    size=int(raw_size.to_integral_value(rounding=ROUND_FLOOR))
    blockers=['Actual account equity/available margin and trade permissions not verified',
              'No exchange-side stop loss or take-profit orders are implemented',
              'No idempotent live order engine, fill reconciliation, or emergency close exists',
              '5x Cross and Hedge Mode have not been verified against Gate.io',
              'Fees, funding, slippage and current spread excluded']
    if minimum_unverified:
        blockers.append('Gate.io returned order_size_min=0; actual exchange minimum is unverified. Displayed sizing is illustrative ONLY')
    if spec.get('in_delisting'):
        blockers.append('Contract marked for delisting')
    if leverage_max<5: blockers.append('Contract maximum leverage below configured 5x')
    if spec.get('enable_decimal'):
        blockers.append('Fractional contract lot increments not verified; integer size used only')
    if Decimal(size)<min_size:
        blockers.append('Risk budget cannot meet Gate.io minimum contract size')
    if Decimal(size)>max_size:
        blockers.append('Computed size exceeds Gate.io maximum per-order size')
    # Never propose a size outside contract boundaries.
    eligible = size>0 and Decimal(size)>=min_size and Decimal(size)<=max_size and leverage_max>=5 and not spec.get('in_delisting')
    size_out = size if eligible else None
    notional=(Decimal(size)*entry*multiplier) if eligible else None
    return dict(contract=contract,side=side,timeframe=tf,
      hypothetical_equity_usdt=str(equity),risk_percent=str(risk),risk_budget_usdt=str(round(budget,8)),
      reference_entry=str(entry),stop_loss=str(stop),take_profit=str(take),
      quanto_multiplier=str(multiplier),
      order_size_min=('0 (UNVERIFIED; illustration uses >=1)' if minimum_unverified else str(reported_min_size)),
      order_size_max=str(max_size),
      size_contracts=size_out,estimated_sl_loss_usdt=str(round(Decimal(size)*loss_per_contract,8)) if eligible else None,
      notional_usdt=str(round(notional,8)) if eligible else None,
      estimated_margin_usdt=str(round(notional/Decimal(5),8)) if eligible else None,
      blockers=blockers,result='NOT APPROVED FOR LIVE TRADING — PREVIEW ONLY', orders_placed=0)


@app.post('/terminal/execution-preview')
def terminal_execution_preview():
    if not _terminal_permitted():
        return _terminal_guard()
    data=request.get_json(silent=True) if request.is_json else None
    if not isinstance(data,dict): return jsonify(error='JSON object required'),400
    symbol=data.get('symbol'); side=data.get('side'); tf=data.get('tf')
    if not all(isinstance(x,str) for x in (symbol,side,tf)):
        return jsonify(error='Symbol, side and timeframe must be strings'),422
    if not re.fullmatch(r'[A-Z0-9]{3,25}USDT',symbol) or side not in ('LONG','SHORT') or tf not in ('5m','15m'):
        return jsonify(error='Invalid symbol, side or timeframe'),422
    try:
        preview=_execution_dry_run_preview(symbol,side,tf,data.get('hypothetical_equity_usdt'),data.get('risk_percent',1))
        response=jsonify(preview=preview, mode='DRY_RUN_ONLY',live_trading=False,orders_enabled=False)
        response.headers['Cache-Control']='no-store'
        return response
    except ValueError as exc:
        return jsonify(error=str(exc),orders_enabled=False),422
    except (HTTPError,URLError,TimeoutError,KeyError,TypeError,OSError,OverflowError):
        app.logger.warning('Public data unavailable for execution preview')
        return jsonify(error='Gate contract or ATR market data temporarily unavailable',orders_enabled=False),502




@app.post('/terminal/execution-precheck')
def terminal_execution_precheck():
    """Read-only execution readiness for a stored LuxAlgo signal. No order APIs."""
    if not _terminal_permitted():
        return _terminal_guard()
    data = request.get_json(silent=True) if request.is_json else None
    if not isinstance(data, dict):
        return jsonify(error='JSON object required', orders_placed=0), 400
    event_id = data.get('event_id')
    if not isinstance(event_id, str) or not re.fullmatch(r'[a-f0-9]{20}', event_id):
        return jsonify(error='Invalid stored event ID', orders_placed=0), 422
    from decimal import Decimal, InvalidOperation
    try:
        risk_percent = Decimal(os.getenv('DEFAULT_RISK_PERCENT', '3'))
        if not risk_percent.is_finite() or risk_percent < Decimal('0.5') or risk_percent > Decimal('5'):
            raise InvalidOperation()
    except (InvalidOperation, TypeError, ValueError):
        return jsonify(error='Invalid DEFAULT_RISK_PERCENT (expected 0.5 to 5)', orders_placed=0), 503
    try:
        with _db_connect() as con:
            _ensure_schema(con)
            with con.cursor() as cur:
                # Durable identity and freshness: never trust a browser-supplied symbol or side.
                cur.execute("""SELECT event_id, received_at, symbol, side, timeframe
                               FROM luxalgo_dry_run_intents WHERE event_id=%s""", (event_id,))
                signal = cur.fetchone()
                if signal is None:
                    return jsonify(error='Signal not found in database', orders_placed=0), 404
                # Repeated requests return the same stored result; no duplicate intents.
                cur.execute('SELECT status, details FROM luxalgo_execution_prechecks WHERE event_id=%s', (event_id,))
                prior = cur.fetchone()
                if prior is not None:
                    return jsonify(mode='DRY_RUN_ONLY', order_execution_enabled=False, orders_placed=0,
                                   precheck=prior['details'], duplicate=True)
                now = datetime.now(timezone.utc)
                age_seconds = max(0, int((now-signal['received_at']).total_seconds()))
                blockers = ['No signed live order submission implementation',
                            'No exchange-side protective stop and take-profit implementation',
                            'No exchange fill/order reconciliation or emergency-close handling',
                            'Gate.io API remains READ ONLY; LIVE_TRADING_ENABLED must remain false',
                            'Account balance, margin, slippage and fees are not execution-verified']
                if age_seconds > 120:
                    blockers.append('Signal is stale (>120 seconds): cannot use as a fresh entry')
                if os.getenv('LIVE_TRADING_ENABLED', 'false').lower() != 'false':
                    blockers.append('CONFIGURATION WARNING: live toggle is not false; no orders supported')
                if os.getenv('DEFAULT_LEVERAGE', '5') != '5':
                    blockers.append('DEFAULT_LEVERAGE is not 5')
                if os.getenv('DEFAULT_MARGIN_MODE', 'cross').lower() != 'cross':
                    blockers.append('DEFAULT_MARGIN_MODE is not cross')
                if os.getenv('GATE_SETTLE', 'usdt').lower() != 'usdt':
                    blockers.append('GATE_SETTLE is not usdt')
                # Deliberately do not calculate an actionable size from hypothetical equity.
                # The existing manual preview remains available for indicative numbers.
                details = dict(event_id=event_id, symbol=signal['symbol'], side=signal['side'],
                    timeframe=signal['timeframe'], received_at=signal['received_at'].isoformat(),
                    age_at_check_seconds=age_seconds, risk_percent=str(risk_percent),
                    planned_leverage=5, margin_mode='cross', position_mode='hedge',
                    sl_atr_multiplier=1.5, tp_atr_multiplier=2,
                    risk_equity_source='NOT_VERIFIED', estimated_contracts=None,
                    status='BLOCKED', blockers=blockers, orders_placed=0,
                    explanation='Foundation readiness record only. The manual risk calculator is separate. No exchange write requests exist.')
                cur.execute("""INSERT INTO luxalgo_execution_prechecks
                    (event_id,symbol,side,timeframe,status,risk_percent,details)
                    VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT(event_id) DO NOTHING""",
                    (event_id,signal['symbol'],signal['side'],signal['timeframe'],'BLOCKED',risk_percent,json.dumps(details)))
                if cur.rowcount == 0:
                    cur.execute('SELECT details FROM luxalgo_execution_prechecks WHERE event_id=%s', (event_id,))
                    details=cur.fetchone()['details']
                response=jsonify(mode='DRY_RUN_ONLY',order_execution_enabled=False,orders_placed=0,precheck=details)
                response.headers['Cache-Control']='no-store'
                return response
    except (psycopg.Error, RuntimeError, OSError):
        app.logger.exception('Execution precheck database unavailable; fail closed')
        return jsonify(error='Database unavailable; execution precheck blocked', orders_placed=0), 503

@app.post('/terminal/simulate-order')
def terminal_simulate_order():
    """Manual paper intent only; no order APIs, no simulated fills and no autonomous trading."""
    if not _terminal_permitted():
        return _terminal_guard()
    data=request.get_json(silent=True) if request.is_json else None
    if not isinstance(data,dict) or not isinstance(data.get('event_id'),str) or not re.fullmatch(r'[a-f0-9]{20}',data['event_id']):
        return jsonify(error='Valid stored event ID required',orders_placed=0),422
    event_id=data['event_id']
    try:
        with _db_connect() as con:
            _ensure_schema(con)
            with con.cursor() as cur:
                if not _bot_enabled(cur):
                    return jsonify(error='BOT OFF: paper simulations paused',orders_placed=0),409
        with _db_connect() as con:
            _ensure_schema(con)
            with con.cursor() as cur:
                cur.execute('SELECT details FROM luxalgo_simulated_orders WHERE event_id=%s',(event_id,))
                row=cur.fetchone()
                if row:
                    return jsonify(simulation=row['details'],duplicate=True,orders_placed=0,mode='SIMULATION_ONLY')
                cur.execute('SELECT received_at,symbol,side,timeframe FROM luxalgo_dry_run_intents WHERE event_id=%s',(event_id,))
                signal=cur.fetchone()
                if not signal:
                    return jsonify(error='Stored event not found',orders_placed=0),404
                age=(datetime.now(timezone.utc)-signal['received_at']).total_seconds()
                if age<0 or age>120:
                    return jsonify(error='Signal is older than 120 seconds. Cannot simulate a fresh entry from stale data',orders_placed=0),422
                try:
                    from decimal import Decimal
                    risk=Decimal(os.getenv('DEFAULT_RISK_PERCENT','1'))
                    if not risk.is_finite() or not Decimal('0.5')<=risk<=Decimal('5'):
                        raise ValueError('Risk setting outside 0.5%-5%')
                except (ValueError,ArithmeticError):
                    return jsonify(error='Invalid risk configuration',orders_placed=0),503
                if os.getenv('LIVE_TRADING_ENABLED','false').strip().lower() != 'false':
                    return jsonify(error='Simulation blocked: LIVE_TRADING_ENABLED must be false',orders_placed=0),503
                # Release the transaction before public API reads; reacquire for the unique insert.
                sym,side,tf=signal['symbol'],signal['side'],signal['timeframe']
        preview=_execution_dry_run_preview(sym,side,tf,100,str(risk))
        if not preview.get('size_contracts'):
            return jsonify(error='Risk preview has no valid illustrative contract size',orders_placed=0),422
        if any('unverified' in x.lower() for x in preview.get('blockers',[])):
            return jsonify(error='Contract metadata is unverified; simulation rejected',orders_placed=0),422
        details=dict(event_id=event_id,symbol=sym,side=side,timeframe=tf,
            status='SIMULATED_ONLY',simulated_fill=False,
            risk_percent=str(risk),hypothetical_equity_usdt='100',
            reference_entry=preview['reference_entry'],stop_loss=preview['stop_loss'],
            take_profit=preview['take_profit'],size_contracts=preview['size_contracts'],
            notes='Manual simulated intent. Prices are from a completed candle, not a market fill. No orders, no SL/TP placement, no position created.',
            orders_placed=0)
        with _db_connect() as con:
            _ensure_schema(con)
            with con.cursor() as cur:
                cur.execute("""INSERT INTO luxalgo_simulated_orders(event_id,symbol,side,timeframe,status,details)
                    VALUES (%s,%s,%s,%s,'SIMULATED_ONLY',%s::jsonb) ON CONFLICT(event_id) DO NOTHING""",
                    (event_id,sym,side,tf,json.dumps(details)))
                duplicate=cur.rowcount==0
                if duplicate:
                    cur.execute('SELECT details FROM luxalgo_simulated_orders WHERE event_id=%s',(event_id,))
                    details=cur.fetchone()['details']
        response=jsonify(simulation=details,duplicate=duplicate,orders_placed=0,mode='SIMULATION_ONLY')
        response.headers['Cache-Control']='no-store'
        return response
    except (psycopg.Error,RuntimeError,OSError):
        app.logger.exception('Simulation database not available')
        return jsonify(error='Simulation store unavailable; no order created',orders_placed=0),503
    except (HTTPError,URLError,TimeoutError,ValueError,KeyError,TypeError,OverflowError):
        return jsonify(error='Risk preview unavailable; simulation rejected',orders_placed=0),502

def _completed_gate_candles_after(symbol, tf, after):
    """Public OHLC only; fail closed on insufficient contiguous candle history."""
    seconds = 300 if tf == '5m' else 900
    start = int(after.timestamp())
    now = int(time.time())
    if now <= start + seconds:
        return []
    # Bound historical review; never silently infer outcomes outside the available history.
    if now - start > seconds * 950:
        raise ValueError('Historical interval exceeds conservative candle retrieval window')
    contract = symbol[:-4] + '_USDT'
    url = ('https://api.gateio.ws/api/v4/futures/usdt/candlesticks?' +
           urlencode({'contract':contract,'interval':tf,'from':max(0,start-seconds), 'to':now}))
    req=Request(url,headers={'Accept':'application/json','User-Agent':'LuxAlgoPRO-PaperMonitor/1.0'},method='GET')
    with urlopen(req,timeout=10) as res:
        raw=json.loads(res.read(450000))
    if not isinstance(raw,list): raise ValueError('Invalid candle data')
    out=[]
    for bar in raw:
        stamp=int(bar['t'])
        if stamp < start or stamp + seconds > now:continue
        high,low=(float(bar[k]) for k in ('h','l'))
        if not math.isfinite(high) or not math.isfinite(low) or low<=0 or high<low:
            raise ValueError('Invalid completed candle')
        out.append((stamp, high, low))
    out=sorted(set(out))
    # Require a consecutive series for all *fully completed* intervals after intent creation.
    first=((start + seconds-1)//seconds)*seconds
    last=((now//seconds)-1)*seconds
    required=max(0,(last-first)//seconds+1)
    if required and (len(out)<required or out[0][0]!=first or
                     any(b[0]-a[0]!=seconds for a,b in zip(out,out[1:]))):
        raise ValueError('Candle history has gaps; outcome cannot be established')
    return out


# Read-only per-signal market outcome. Never implies a filled exchange order.
@app.get('/terminal/testnet-signal-check/<event_id>')
def terminal_testnet_signal_check(event_id):
    """Read-only validation of a stored signal; does not create an order."""
    if not _terminal_permitted():
        return _terminal_guard()
    if not re.fullmatch(r'[a-f0-9]{20}',event_id):
        return jsonify(error='Invalid signal identifier',eligible=False),400
    try:
        with _db_connect() as con:
            with con.cursor() as cur:
                cur.execute("""SELECT i.received_at,i.symbol,i.side,i.timeframe,
                    l.status AS level_status,t.order_type
                    FROM luxalgo_dry_run_intents i
                    LEFT JOIN luxalgo_signal_levels l ON l.event_id=i.event_id
                    LEFT JOIN luxalgo_signal_order_types t ON t.event_id=i.event_id
                    WHERE i.event_id=%s""",(event_id,))
                row=cur.fetchone()
        if not row:
            return jsonify(error='Signal not found',eligible=False),404
        elapsed=(datetime.now(timezone.utc)-row['received_at']).total_seconds()
        failures=[]
        if elapsed<0 or elapsed>120:
            failures.append('SIGNAL_EXPIRED')
        if row['level_status']!='VALID':
            failures.append('ENTRY_SL_TP_NOT_VALIDATED')
        if row['order_type'] not in ('MARKET','LIMIT'):
            failures.append('ORDER_TYPE_UNDECLARED')
        failures.append('TESTNET_EXECUTION_ENGINE_NOT_READY')
        response=jsonify(event_id=event_id,symbol=row['symbol'],side=row['side'],
            timeframe=row['timeframe'],order_type=row['order_type'] or 'UNDECLARED',
            signal_age_seconds=max(0,int(elapsed)),blockers=failures,
            eligible=False,orders_placed=0,live_trading=False)
        response.headers['Cache-Control']='no-store'
        return response
    except (psycopg.Error,RuntimeError,OSError):
        return jsonify(error='Cannot verify signal',eligible=False),503


@app.get('/terminal/testnet-execution-handoffs')
def terminal_testnet_execution_handoffs():
    """Read-only durable signal-to-testnet-stage audit; never exchange orders."""
    if not _terminal_permitted():
        return _terminal_guard()
    try:
        with _db_connect() as con:
            with con.cursor() as cur:
                cur.execute("""SELECT event_id,received_at,symbol,side,timeframe,order_type,status,reason
                    FROM luxalgo_testnet_execution_handoff ORDER BY received_at DESC LIMIT 50""")
                rows=cur.fetchall()
        result=jsonify(count=len(rows),handoffs=[dict(event_id=r['event_id'],
            received_at=r['received_at'].isoformat(),symbol=r['symbol'],side=r['side'],
            timeframe=r['timeframe'],order_type=r['order_type'] or 'UNDECLARED',
            status=r['status'],reason=r['reason']) for r in rows],
            orders_submitted=0,environment='GATE_FUTURES_TESTNET')
        result.headers['Cache-Control']='private, no-store'
        return result
    except (psycopg.Error,RuntimeError,OSError):
        return jsonify(error='Testnet handoff audit unavailable'),503


@app.get('/terminal/testnet-order-readiness')
def terminal_testnet_order_readiness():
    if not _terminal_permitted():
        return _terminal_guard()
    response=jsonify(environment='GATE_FUTURES_TESTNET',
        signal_receiver=True,read_only_monitor=True,
        order_submission_implemented=False,
        protective_orders_implemented=False,
        fill_reconciliation_implemented=False,
        automatic_execution_ready=False,
        production_trading_enabled=False,
        reason='Automatic execution requires verified exchange-native protection and fill reconciliation')
    response.headers['Cache-Control']='no-store'
    return response


def _incoming_visible_since(cursor):
    """Persist a one-way dashboard reset; preserve receipt/execution audit data.

    The Render reset environment value seeds a fresh view even if no user
    ever clicks Clear. Interactive clear advances the same durable cursor.
    """
    # Production display reset uses standard ISO 8601; no Testnet UI module.
    cursor.execute("""CREATE TABLE IF NOT EXISTS luxalgo_incoming_display_reset (
       id SMALLINT PRIMARY KEY CHECK(id=1),
       cleared_at TIMESTAMPTZ NOT NULL
    )""")
    cursor.execute("SELECT cleared_at FROM luxalgo_incoming_display_reset WHERE id=1")
    row=cursor.fetchone()
    boundary=row['cleared_at'] if row else datetime(1970,1,1,tzinfo=timezone.utc)
    raw_reset=os.getenv('LUXALGO_INCOMING_VISIBLE_AFTER_UTC','')
    seeded=None
    if raw_reset:
        try:
            seeded=datetime.fromisoformat(raw_reset.replace('Z','+00:00'))
            if seeded.tzinfo is None: raise ValueError('Reset requires timezone')
            seeded=seeded.astimezone(timezone.utc)
        except (ValueError,TypeError,AttributeError):
            raise ValueError('Invalid incoming display reset time') from None
    if seeded is not None and seeded>boundary:
        cursor.execute("""INSERT INTO luxalgo_incoming_display_reset(id,cleared_at)
            VALUES(1,%s) ON CONFLICT(id) DO UPDATE
            SET cleared_at=GREATEST(luxalgo_incoming_display_reset.cleared_at,EXCLUDED.cleared_at)
            RETURNING cleared_at""",(seeded,))
        boundary=cursor.fetchone()['cleared_at']
    return boundary


@app.post('/terminal/incoming-signals/reset')
def terminal_reset_incoming_signals():
    """Reset only dashboard visibility; never delete exchange safety evidence."""
    if not _terminal_permitted():
        return _terminal_guard()
    if not _csrf_valid():
        return jsonify(error='Invalid terminal session verification'),403
    try:
        with _db_connect() as con:
            with con.cursor() as cur:
                # Ensure previous Render reset has also been applied.
                _incoming_visible_since(cur)
                cur.execute("""INSERT INTO luxalgo_incoming_display_reset(id,cleared_at)
                    VALUES(1,clock_timestamp()) ON CONFLICT(id) DO UPDATE
                    SET cleared_at=GREATEST(luxalgo_incoming_display_reset.cleared_at,clock_timestamp())
                    RETURNING cleared_at""")
                since=cur.fetchone()['cleared_at']
        response=jsonify(status='CLEARED',visible_signals=0,
                         display_since=since.isoformat(),audit_records_preserved=True,
                         environment='GATE_FUTURES_TESTNET')
        response.headers['Cache-Control']='private, no-store'
        return response
    except (psycopg.Error,RuntimeError,OSError,ValueError,TypeError):
        return jsonify(error='Visible history reset could not be saved'),503


@app.get('/terminal/incoming-signals')
def terminal_incoming_signals():
    """Latest 25 webhook receipts and independently verified Testnet states."""
    if not _terminal_permitted():
        return _terminal_guard()
    production_monitor=(request.args.get('production')=='1' and
                        os.getenv('PRODUCTION_TERMINAL_VIEW','false').strip().lower()=='true')
    if production_monitor:
        # Live production signal monitor is sourced solely from the webhook
        # receipt and validated levels tables. No Testnet broker, worker,
        # execution attempts, positions, or legacy DB tables are consulted.
        try:
            with _db_connect() as con:
                _ensure_schema(con)
                with con.cursor() as cur:
                    cutoff=_incoming_visible_since(cur)
                    cur.execute("""SELECT p.event_id,p.received_at,p.symbol,p.side,p.timeframe,
                           l.status AS level_status,l.entry_price,l.stop_loss,l.take_profit,
                           o.order_type
                        FROM (
                           SELECT DISTINCT ON (event_id)
                               event_id,received_at,symbol,side,timeframe
                           FROM luxalgo_signal_receipts
                           WHERE received_at>%s
                           ORDER BY event_id,received_at DESC
                        ) p
                        LEFT JOIN luxalgo_signal_levels l ON l.event_id=p.event_id
                        LEFT JOIN luxalgo_signal_order_types o ON o.event_id=p.event_id
                        ORDER BY p.received_at DESC LIMIT 25""",(cutoff,))
                    rows=cur.fetchall()
            signals=[]
            for r in rows:
                level=r['level_status'] or 'MISSING'
                reason=('Recorded LuxAlgo alert; real-money execution disabled until native SL/TP and recovery are verified'
                        if level=='VALID' else
                        'Alert stored but entry, stop or target levels are missing/invalid; no exchange order')
                signals.append(dict(event_id=r['event_id'],time=r['received_at'].isoformat(),
                     symbol=r['symbol'],side=r['side'],timeframe=r['timeframe'],
                     entry=str(r['entry_price']) if r['entry_price'] is not None else None,
                     stop_loss=str(r['stop_loss']) if r['stop_loss'] is not None else None,
                     take_profit=str(r['take_profit']) if r['take_profit'] is not None else None,
                     order_type=r['order_type'] or 'UNDECLARED',state='NOT EXECUTED',
                     status_label='NOT EXECUTED',reason=reason,level_status=level))
            response=jsonify(signals=signals,count=len(signals),display_limit=25,
                             display_since=cutoff.isoformat(),mode='PRODUCTION_SIGNAL_ONLY',
                             live_trading=False,orders_enabled=False,
                             audit_records_preserved=True)
            response.headers['Cache-Control']='no-store'
            return response
        except (psycopg.Error,RuntimeError,OSError,ValueError,TypeError):
            app.logger.exception('Production signal receipt monitor unavailable')
            return jsonify(error='Production signal monitor unavailable',orders_placed=0),503
    try:
        from testnet_execution_attempts import DDL as _execution_attempts_schema
        with _db_connect() as con:
            with con.cursor() as cur:
                # Dashboard works before the first successful Testnet event.
                cur.execute(_execution_attempts_schema)
                cutoff=_incoming_visible_since(cur)
                cur.execute('''SELECT i.event_id,i.received_at,i.symbol,i.side,i.timeframe,
                    l.status AS level_status,l.entry_price,l.stop_loss,l.take_profit,
                    q.state AS queue_state,q.note AS queue_note,
                    s.status AS simulated_status,b.enabled AS bot_enabled,ot.order_type,
                    j.state AS testnet_execution_state,j.payload AS testnet_payload,
                    j.details AS testnet_details,j.client_id AS testnet_client_id,
                    a.state AS attempt_state,a.detail AS attempt_detail
                    FROM luxalgo_dry_run_intents i
                    LEFT JOIN luxalgo_signal_levels l ON l.event_id=i.event_id
                    LEFT JOIN luxalgo_auto_paper_queue q ON q.event_id=i.event_id
                    LEFT JOIN luxalgo_simulated_orders s ON s.event_id=i.event_id
                    LEFT JOIN luxalgo_bot_control b ON b.id=1
                    LEFT JOIN luxalgo_signal_order_types ot ON ot.event_id=i.event_id
                    LEFT JOIN luxalgo_testnet_protected_journal j ON j.signal_id=i.event_id
                    LEFT JOIN luxalgo_testnet_execution_attempts a ON a.event_id=i.event_id
                    WHERE i.received_at > %s
                    ORDER BY i.received_at DESC LIMIT 25''',(cutoff,))
                rows=cur.fetchall()
        # Only a freshly rechecked exchange position with native SL/TP
        # qualifies for a LIVE indicator, never an unverified journal label.
        verified_live=set()
        exchange_checked=False
        if not production_monitor and any(r['testnet_execution_state']=='PROTECTED_VERIFIED' for r in rows):
            try:
                from gate_testnet_broker import GateFuturesTestnetBroker
                from testnet_card_feed import build_testnet_position_cards
                positions=_gate_read_only_positions(testnet=True)
                eligible=[dict(signal_id=r['event_id'],
                    contract=r['symbol'][:-4]+'_USDT',side=r['side'],
                    state=r['testnet_execution_state'],payload=r['testnet_payload'],
                    details=r['testnet_details'],client_id=r['testnet_client_id'])
                    for r in rows if r['testnet_execution_state']=='PROTECTED_VERIFIED']
                cards=build_testnet_position_cards(positions,eligible,GateFuturesTestnetBroker())
                verified_live={str(c['signal_id']) for c in cards
                    if c.get('protection_state')=='EXCHANGE_VERIFIED' and c.get('signal_id')}
                exchange_checked=True
            except Exception:
                exchange_checked=False
        from incoming_testnet_status import classify_signal,explain_testnet_attempt
        worker_armed=(os.getenv('GATE_TESTNET_PROTECTED_EXECUTION_ENABLED','false').lower()=='true'
                      and os.getenv('GATE_TESTNET_WEBHOOK_AUTOMATION_ENABLED','false').lower()=='true'
                      and os.getenv('GATE_TESTNET_RECOVERY_ENABLED','false').lower()=='true'
                      and os.getenv('LIVE_TRADING_ENABLED','false').lower()=='false'
                      and os.getenv('ORDERS_ENABLED','false').lower()=='false')
        now=datetime.now(timezone.utc)
        signals=[]
        for r in rows:
            age=(now-r['received_at']).total_seconds()
            state=classify_signal(
                journal_state=r['testnet_execution_state'],
                attempt_state=r['attempt_state'],
                attempt_detail=r['attempt_detail'],
                queue_state=r['queue_state'],
                age_seconds=age,
                exchange_checked=exchange_checked,
                exchange_open=r['event_id'] in verified_live,
                worker_armed=worker_armed)
            signals.append(dict(event_id=r['event_id'],time=r['received_at'].isoformat(),
                symbol=r['symbol'],side=r['side'],timeframe=r['timeframe'],
                entry=str(r['entry_price']) if r['entry_price'] is not None else None,
                stop_loss=str(r['stop_loss']) if r['stop_loss'] is not None else None,
                take_profit=str(r['take_profit']) if r['take_profit'] is not None else None,
                order_type=(r['order_type'] or
                   ('MARKET (TESTNET)' if isinstance(r['testnet_payload'],dict)
                      and r['testnet_payload'].get('tif')=='ioc'
                      and str(r['testnet_payload'].get('price'))=='0'
                    else 'UNDECLARED')),state=state,
                reason=(('Gate Testnet journal: '+str(r['testnet_execution_state']))
                        if r['testnet_execution_state'] else
                        explain_testnet_attempt(r['attempt_state'],r['attempt_detail'],
                                                worker_armed=worker_armed)),
                level_status=r['level_status'] or 'UNKNOWN',
                status_label=state))
            if production_monitor:
                # Never portray archived Testnet execution as a REAL-MONEY fill.
                # Main production terminal is signal intake only until sender verified.
                signals[-1].update(state='NOT EXECUTED',status_label='NOT EXECUTED',
                    order_type=r['order_type'] or 'UNDECLARED',
                    reason='Production signal saved; real-money order engine OFF. No live exchange entry.')
        response=jsonify(signals=signals,count=len(signals),display_limit=25,
                         display_since=cutoff.isoformat(),
                         mode='PRODUCTION_SIGNAL_ONLY' if production_monitor else 'TESTNET_SIGNAL_MONITOR',
                         live_trading=False,audit_records_preserved=True)
        response.headers['Cache-Control']='no-store'
        return response
    except (psycopg.Error,RuntimeError,OSError):
        app.logger.exception('Incoming signal monitor unavailable')
        return jsonify(error='Signal monitor unavailable',orders_placed=0),503


@app.get('/terminal/signal-diagnostics/<event_id>')
def terminal_signal_diagnostics(event_id):
    """Read-only reason a received signal did not become a paper intent.

    Neither this route nor its queries place exchange or simulated orders.
    """
    if not _terminal_permitted():
        return _terminal_guard()
    if not re.fullmatch(r'[a-f0-9]{20}', event_id):
        return jsonify(error='Invalid signal ID', orders_placed=0), 400
    try:
        with _db_connect() as con:
            with con.cursor() as cur:
                cur.execute('''SELECT i.received_at,i.symbol,i.side,i.timeframe,
                    l.status AS level_status,l.reason AS level_reason,
                    q.state AS queue_state,q.note AS queue_note,
                    s.status AS simulated_status,
                    b.enabled AS bot_enabled
                    FROM luxalgo_dry_run_intents i
                    LEFT JOIN luxalgo_signal_levels l ON l.event_id=i.event_id
                    LEFT JOIN luxalgo_auto_paper_queue q ON q.event_id=i.event_id
                    LEFT JOIN luxalgo_simulated_orders s ON s.event_id=i.event_id
                    LEFT JOIN luxalgo_bot_control b ON b.id=1
                    WHERE i.event_id=%s''', (event_id,))
                signal=cur.fetchone()
        if signal is None:
            return jsonify(error='Signal not found', orders_placed=0), 404
        age=(datetime.now(timezone.utc)-signal['received_at']).total_seconds()
        if signal['simulated_status']=='SIMULATED_ONLY':
            reason='PAPER_CREATED'
        elif signal['queue_state']=='REJECTED':
            note=(signal['queue_note'] or '').lower()
            if 'expired' in note:
                reason='SIGNAL_EXPIRED'
            elif 'entry/sl/tp' in note or 'missing' in note or 'invalid' in note and 'level' in note:
                reason='MISSING_OR_INVALID_SL_TP'
            elif 'switched off' in note:
                reason='BOT_OFF'
            else:
                reason='RISK_OR_DATA_REJECTED'
        elif signal['queue_state']=='PENDING':
            reason='SIGNAL_EXPIRED' if age>120 else 'PAPER_PENDING'
        elif signal['level_status'] in ('MISSING','INVALID'):
            reason='MISSING_OR_INVALID_SL_TP'
        elif not signal['bot_enabled']:
            reason='BOT_OFF'
        else:
            reason='NOT_QUEUED_OR_DUPLICATE'
        response=jsonify(event_id=event_id,symbol=signal['symbol'],side=signal['side'],
            timeframe=signal['timeframe'],received_at=signal['received_at'].isoformat(),
            reason=reason,level_status=signal['level_status'] or 'UNKNOWN',
            level_reason=signal['level_reason'] or '',queue_state=signal['queue_state'] or 'NOT_QUEUED',
            queue_note=signal['queue_note'] or '',paper_intent_created=bool(signal['simulated_status']),
            bot_enabled=bool(signal['bot_enabled']),orders_placed=0,live_trading=False)
        response.headers['Cache-Control']='no-store'
        return response
    except (psycopg.Error, RuntimeError, OSError):
        app.logger.exception('Signal diagnostic read failed')
        return jsonify(error='Signal diagnostics unavailable',orders_placed=0),503


@app.get('/terminal/call-status/<event_id>')
def terminal_call_status(event_id):
    if not _terminal_permitted(): return _terminal_guard()
    if not re.fullmatch(r'[a-f0-9]{20}',event_id):
        return jsonify(error='Invalid signal ID'),400
    try:
        with _db_connect() as con:
            _ensure_schema(con)
            with con.cursor() as cur:
                cur.execute("""SELECT r.received_at,r.symbol,r.side,r.timeframe,l.status,l.entry_price,l.stop_loss,l.take_profit
                   FROM luxalgo_signal_receipts r LEFT JOIN luxalgo_signal_levels l ON l.event_id=r.event_id
                   WHERE r.event_id=%s ORDER BY r.id ASC LIMIT 1""",(event_id,))
                sig=cur.fetchone()
        if not sig: return jsonify(error='Signal not found'),404
        info=dict(event_id=event_id,symbol=sig['symbol'],side=sig['side'],timeframe=sig['timeframe'],orders_placed=0,source='PUBLIC_COMPLETED_CANDLES',is_exchange_fill=False)
        if sig['status']!='VALID' or any(sig[k] is None for k in ('entry_price','stop_loss','take_profit')):
            return jsonify(**info,state='UNVERIFIABLE',detail='Original signal lacks validated entry / SL / TP levels')
        entry=float(sig['entry_price']); sl=float(sig['stop_loss']);tp=float(sig['take_profit'])
        side=sig['side']
        if not ((side=='LONG' and sl<entry<tp) or (side=='SHORT' and tp<entry<sl)):
            return jsonify(**info,state='UNVERIFIABLE',detail='Invalid signal risk levels')
        candles=_completed_gate_candles_after(sig['symbol'],sig['timeframe'],sig['received_at'])
        if not candles:return jsonify(**info,state='AWAITING DATA',detail='Waiting for first fully completed candle after signal')
        entered=False
        for stamp,hi,lo in candles:
            just_entered=False
            if not entered:
                if not lo<=entry<=hi:continue
                entered=True
                just_entered=True
            sh=lo<=sl if side=='LONG' else hi>=sl
            th=hi>=tp if side=='LONG' else lo<=tp
            # On the entry candle, intrabar ordering is unknown.
            if just_entered and (sh or th):
                return jsonify(**info,state='UNVERIFIABLE',detail='Entry and exit level touched in same candle; price sequence unknown')
            if sh and th:
                return jsonify(**info,state='UNVERIFIABLE',detail='SL and TP touched in the same candle; order unknown')
            if sh:return jsonify(**info,state='SL HIT',detail='SL price touched in completed market candle',candle=stamp)
            if th:return jsonify(**info,state='TP HIT',detail='TP price touched in completed market candle',candle=stamp)
        return jsonify(**info,state='LIVE' if entered else 'AWAITING ENTRY',detail=('Entry price touched; no exit level touched in reviewed candles' if entered else 'No completed reviewed candle touched entry'))
    except (HTTPError,URLError,TimeoutError,ValueError,KeyError,TypeError,OverflowError,psycopg.Error,RuntimeError,OSError) as exc:
        app.logger.warning('Read-only call status unavailable for %s: %s',event_id,type(exc).__name__)
        return jsonify(state='UNVERIFIABLE',detail='Reliable completed market history currently unavailable',orders_placed=0,is_exchange_fill=False),200


def _paper_trade_snapshot(order):
    """Conservative hypothetical OHLC simulation, NOT a trade execution or fill."""
    from decimal import Decimal, InvalidOperation
    doc=order['details']
    try:
        entry=Decimal(str(doc['reference_entry']))
        stop=Decimal(str(doc['stop_loss']))
        take=Decimal(str(doc['take_profit']))
        contracts=Decimal(str(doc['size_contracts']))
        if not all(v.is_finite() and v>0 for v in (entry,stop,take,contracts)):
            raise ValueError('Invalid simulation prices/size')
        side=order['side']
        if not ((side=='LONG' and stop<entry<take) or (side=='SHORT' and take<entry<stop)):
            raise ValueError('Invalid risk geometry')
        # Public specs are not assumed to be unchanged: P&L needs a known multiplier.
        contract=order['symbol'][:-4]+'_USDT'
        req=Request('https://api.gateio.ws/api/v4/futures/usdt/contracts/'+contract,
                    headers={'Accept':'application/json','User-Agent':'LuxAlgoPRO-PaperMonitor/1.0'},method='GET')
        with urlopen(req,timeout=8) as res:spec=json.loads(res.read(65536))
        mult=Decimal(str(spec['quanto_multiplier']))
        if not mult.is_finite() or mult<=0:raise ValueError('Multiplier unverified')
    except (InvalidOperation,KeyError,TypeError):
        raise ValueError('Unverifiable simulated trade fields')
    candles=_completed_gate_candles_after(order['symbol'],order['timeframe'],order['created_at'])
    state='OPEN';exit_price=None;exit_bar=None
    for t,hi,lo in candles:
        stop_hit=lo<=float(stop) if side=='LONG' else hi>=float(stop)
        take_hit=hi>=float(take) if side=='LONG' else lo<=float(take)
        if stop_hit or take_hit:
            # Pessimistic assumption for same-bar ambiguity: stop first.
            state='SL' if stop_hit else 'TP'
            exit_price=stop if stop_hit else take
            exit_bar=t
            break
    pnl=None
    if exit_price is not None:
        delta=(exit_price-entry) if side=='LONG' else (entry-exit_price)
        pnl=delta*contracts*mult
    return dict(event_id=order['event_id'],created_at=order['created_at'].isoformat(),
                symbol=order['symbol'],side=side,timeframe=order['timeframe'],
                entry=str(entry),stop=str(stop),take=str(take),
                state=state,exit_candle=exit_bar,exit_price=str(exit_price) if exit_price is not None else None,
                estimated_pnl_usdt=str(round(pnl,8)) if pnl is not None else None)


# Single paper worker keeps slow public-market requests off webhook response threads.
# Durable PostgreSQL queue remains the source of truth; no exchange order writes.
_paper_auto_executor = ThreadPoolExecutor(max_workers=1,thread_name_prefix='luxalgo-paper')
# Production worker queue is separate from public market data. Source gates OFF.
_live_auto_executor = ThreadPoolExecutor(max_workers=1,thread_name_prefix='luxalgo-live-locked')
def _process_live_webhook_once(event_id):
    from live_execution_orchestrator import process_stored_luxalgo_alert
    from live_futures_engine import LiveSafetyError
    try:
        result=process_stored_luxalgo_alert(_db_connect,event_id)
        app.logger.info('Production lifecycle state=%s',result.get('state','UNKNOWN'))
    except (LiveSafetyError,psycopg.Error,RuntimeError,OSError,ValueError,TypeError) as exc:
        app.logger.warning('Production lifecycle worker blocked: %s',type(exc).__name__)


# Separate TESTNET-only serialized executor. Remains OFF without BOTH dedicated
# Testnet opt-in variables. Never reads production trading credentials.
_testnet_auto_executor = ThreadPoolExecutor(max_workers=1,thread_name_prefix='luxalgo-testnet')

def _testnet_recovery_monitor():
    """Readback-only timeout reconciliation + Testnet emergency close if needed.

    Never replays an entry order. DB claims protect against duplicate recovery
    across workers/redeploys. Only starts under BOTH independent Testnet flags.
    """
    try:
        with _db_connect() as con:
            _ensure_schema(con)
    except (psycopg.Error,RuntimeError,OSError):
        app.logger.error('Testnet recovery startup schema unavailable; will retry')
    while (os.getenv('GATE_TESTNET_RECOVERY_ENABLED','false').lower()=='true' or
           (os.getenv('GATE_TESTNET_PROTECTED_EXECUTION_ENABLED','false').lower()=='true' and
            os.getenv('GATE_TESTNET_WEBHOOK_AUTOMATION_ENABLED','false').lower()=='true')):
        time.sleep(20)
        try:
            from testnet_recovery import recover_testnet_once
            from testnet_protection_watchdog import audit_protected_testnet_once
            report=recover_testnet_once(limit=3)
            audit=audit_protected_testnet_once(limit=3)
            from testnet_unknown_exposure import check_unknown_once
            unknown=check_unknown_once()
            if unknown.get("reports"):
                app.logger.info("Testnet unknown-exposure audit: %s",
                                [(r["contract"],r["state"]) for r in unknown["reports"]])
            outcomes=report.get('outcomes',[])+audit.get('outcomes',[])
            if any(x=='HALTED_UNPROTECTED' for x in outcomes):
                app.logger.error('Testnet exposure is unprotected; manual intervention required')
            elif any(x=='HALTED_UNKNOWN' for x in outcomes):
                from testnet_unknown_exposure import unknown_exposure_gate
                if unknown_exposure_gate():
                    app.logger.info('Historical unknown Testnet order retained for audit; repeat current-exposure checks passed')
                else:
                    app.logger.error('Unknown current Testnet exposure is not cleared; new entries remain blocked')
        except (psycopg.Error,RuntimeError,OSError,ValueError,TypeError,KeyError) as exc:
            app.logger.warning('Testnet recovery polling unavailable: %s',type(exc).__name__)

# Retired Testnet recovery cannot rearm on production startup.

def _process_testnet_webhook_soon(event_id):
    from testnet_execution_attempts import record_testnet_attempt
    try:
        from testnet_automation import run_fresh_luxalgo_testnet_signal
        result=run_fresh_luxalgo_testnet_signal(event_id)
    except Exception as exc:
        # A transport error after POST cannot prove that an order was never sent.
        result={"state":"EXECUTION_EXCEPTION","submitted":"unknown",
                "detail":type(exc).__name__.upper()[:50]}
    try:
        record_testnet_attempt(event_id,result)
    except (psycopg.Error,RuntimeError,OSError,ValueError,TypeError) as exc:
        app.logger.error('Testnet worker outcome could not be audited: %s',type(exc).__name__)
    if result.get('state') not in ('PROTECTED_VERIFIED','TESTNET_DISABLED'):
        app.logger.warning('Testnet protected execution state=%s detail=%s',
                           result.get('state','UNKNOWN'),result.get('detail',''))


def _process_paper_webhook_soon():
    try:
        _auto_paper_process(max_items=2)
    except (psycopg.Error,RuntimeError,OSError) as exc:
        app.logger.warning('Paper queue processing deferred: %s',type(exc).__name__)


def _auto_paper_process(max_items=5):
    # Legacy paper history remains readable; no new simulations are allowed.
    # The existing queue is retained as the Testnet signal eligibility handoff.
    return 0
    from decimal import Decimal, InvalidOperation
    if os.getenv('LIVE_TRADING_ENABLED','false').strip().lower() != 'false':
        return 0
    with _db_connect() as con:
        _ensure_schema(con)
        with con.cursor() as cur:
            if not _bot_enabled(cur):
                return 0
            cur.execute('''SELECT q.event_id,q.received_at,i.symbol,i.side,i.timeframe,
                       l.status AS levels_status
                FROM luxalgo_auto_paper_queue q
                JOIN luxalgo_dry_run_intents i ON i.event_id=q.event_id
                LEFT JOIN luxalgo_signal_levels l ON l.event_id=q.event_id
                WHERE q.state='PENDING' ORDER BY q.received_at ASC LIMIT %s''',(max_items,))
            pending=cur.fetchall()
    completed=0
    for item in pending:
        eid=item['event_id']
        age=(datetime.now(timezone.utc)-item['received_at']).total_seconds()
        state='REJECTED';note='Unverified risk or market data'
        if item['levels_status']!='VALID':
            # Never create a new simulated order when the original alert did
            # not supply validated LONG/SHORT entry, stop and target levels.
            note='Rejected: explicit LuxAlgo entry/SL/TP levels missing or invalid'
        elif age < 0 or age > 120:
            note='Signal expired before paper simulation; no retrospective entry assumed'
        else:
            try:
                risk=Decimal(os.getenv('DEFAULT_RISK_PERCENT','1'))
                if not risk.is_finite() or not Decimal('0.5')<=risk<=Decimal('5'):
                    note='Risk configuration invalid'
                else:
                    preview=_execution_dry_run_preview(item['symbol'],item['side'],item['timeframe'],100,str(risk))
                    if isinstance(preview.get('size_contracts'), int) and preview['size_contracts'] > 0:
                        details=dict(event_id=eid,symbol=item['symbol'],side=item['side'],timeframe=item['timeframe'],
                            status='SIMULATED_ONLY',simulated_fill=False,risk_percent=str(risk),
                            hypothetical_equity_usdt='100',reference_entry=preview['reference_entry'],
                            stop_loss=preview['stop_loss'],take_profit=preview['take_profit'],
                            size_contracts=preview['size_contracts'],
                            notes='Illustrative paper sizing only; preview live-trading blockers do not block simulation; no fill or exchange orders.',
                            preview_blockers=preview.get('blockers',[]),
                            orders_placed=0)
                        with _db_connect() as con:
                            with con.cursor() as cur:
                                # Coordinate with OFF toggle across workers.
                                if not _bot_enabled(cur,lock=True):
                                    return completed
                                cur.execute('SELECT state FROM luxalgo_auto_paper_queue WHERE event_id=%s FOR UPDATE',(eid,))
                                row=cur.fetchone()
                                if row and row['state']=='PENDING':
                                    cur.execute('''INSERT INTO luxalgo_simulated_orders
                                         (event_id,symbol,side,timeframe,status,details)
                                         VALUES (%s,%s,%s,%s,'SIMULATED_ONLY',%s::jsonb)
                                         ON CONFLICT(event_id) DO NOTHING''',
                                         (eid,item['symbol'],item['side'],item['timeframe'],json.dumps(details)))
                                    cur.execute('''UPDATE luxalgo_auto_paper_queue SET state='CREATED',checked_at=NOW(),
                                         note='Stored simulated intent; no exchange action' WHERE event_id=%s''',(eid,))
                        completed+=1
                        continue
                    note='Illustrative size or contract metadata invalid'
            except (HTTPError,URLError,TimeoutError,ValueError,KeyError,TypeError,OverflowError,InvalidOperation,OSError) as exc:
                # If public data is temporarily unavailable, keep the queue pending until
                # freshness expires; never invent a paper fill from later prices.
                note='Market data unavailable; will retry while signal is fresh'
                if age < 120:
                    continue
            except psycopg.Error:
                app.logger.exception('Auto paper database failure')
                continue
        with _db_connect() as con:
            with con.cursor() as cur:
                cur.execute('''UPDATE luxalgo_auto_paper_queue SET state=%s,checked_at=NOW(),note=%s
                     WHERE event_id=%s AND state='PENDING' ''',(state,note,eid))
    return completed


_paper_multiplier_cache = {}  # public contract specifications; cached for 30 minutes

def _paper_open_marks(orders):
    """Public GET-only marked hypothetical P&L. Never estimates missing specs."""
    from decimal import Decimal, InvalidOperation
    opens = [o for o in orders if o.get('state') == 'OPEN']
    if not opens:
        return {}, Decimal('0')
    try:
        tickers = _scan_http_json('/api/v4/futures/usdt/tickers')
        if not isinstance(tickers, list):
            return {}, None
        quotes = {str(t.get('contract')): t for t in tickers if isinstance(t,dict)}
    except (HTTPError,URLError,TimeoutError,ValueError,TypeError,OSError):
        return {}, None
    marked = {}; total = Decimal('0'); complete = True
    multipliers = {}
    for o in opens:
        eid = o['event_id']
        try:
            sym = o['symbol']
            if not re.fullmatch(r'[A-Z0-9]{2,24}USDT',sym):
                raise ValueError('Invalid symbol')
            contract = sym[:-4]+'_USDT'
            quote = quotes[contract]
            price = Decimal(str(quote['last']))
            entry = Decimal(str(o['entry']))
            size = Decimal(str(o['size_contracts']))
            if contract not in multipliers:
                cached=_paper_multiplier_cache.get(contract)
                if cached is None or time.monotonic()-cached[0]>1800:
                    spec = _scan_http_json('/api/v4/futures/usdt/contracts/'+contract)
                    cached=(time.monotonic(),str(spec['quanto_multiplier']))
                    _paper_multiplier_cache[contract]=cached
                multipliers[contract]=Decimal(cached[1])
            mult = multipliers[contract]
            if not all(x.is_finite() and x>0 for x in (price,entry,size,mult)):
                raise ValueError('Invalid market valuation inputs')
            if o['side'] not in ('LONG','SHORT'):
                raise ValueError('Invalid direction')
            pnl = ((price-entry) if o['side']=='LONG' else (entry-price))*size*mult
            marked[eid] = {'mark_price': str(price),'unrealized_pnl_usdt': str(round(pnl,8))}
            total += pnl
        except (HTTPError,URLError,TimeoutError,KeyError,InvalidOperation,ValueError,TypeError,OSError,OverflowError):
            complete = False
    return marked, round(total,8) if complete else None


# Fast, GET-only paper quote snapshots. Never runs candle simulation or writes to DB.
# A brief shared cache prevents each browser polling session from multiplying API load.
_paper_live_cache_lock = Lock()
_paper_live_cache = {'at':0.0,'value':None}

@app.get('/terminal/paper-live-pnl')
def terminal_paper_live_pnl():
    if not _terminal_permitted(): return _terminal_guard()
    if os.getenv('LIVE_TRADING_ENABLED','false').strip().lower() != 'false':
        return jsonify(error='Live exchange execution must remain disabled',orders_placed=0),503
    try:
        now=time.monotonic()
        with _paper_live_cache_lock:
            if _paper_live_cache['value'] is not None and now-_paper_live_cache['at']<3.0:
                result=_paper_live_cache['value']
            else:
                with _db_connect() as con:
                    with con.cursor() as cur:
                        cur.execute("""SELECT o.event_id,o.symbol,o.side,o.details
                          FROM luxalgo_simulated_orders o
                          JOIN luxalgo_paper_tracking t ON t.event_id=o.event_id
                          WHERE t.state='OPEN'
                          ORDER BY o.created_at DESC LIMIT 10""")
                        orders=cur.fetchall()
                opens=[dict(event_id=o['event_id'],symbol=o['symbol'],side=o['side'],
                    state='OPEN',entry=o['details'].get('reference_entry'),
                    size_contracts=o['details'].get('size_contracts')) for o in orders]
                marks,total=_paper_open_marks(opens)
                result=dict(marks=marks,open_unrealized_pnl_usdt=str(total) if total is not None else None,
                    priced=len(marks),open_count=len(opens),source='GATE_IO_PUBLIC_LAST_PRICE',
                    as_of=datetime.now(timezone.utc).isoformat(),orders_placed=0)
                _paper_live_cache.update(at=time.monotonic(),value=result)
        response=jsonify(result)
        response.headers['Cache-Control']='no-store'
        return response
    except (psycopg.Error,RuntimeError,OSError,ValueError,TypeError):
        app.logger.warning('Fast paper mark data temporarily unavailable',exc_info=False)
        return jsonify(error='Paper live quotes unavailable',orders_placed=0),503

@app.get('/terminal/paper-trades')
def terminal_paper_trades():
    if not _terminal_permitted():return _terminal_guard()
    if os.getenv('LIVE_TRADING_ENABLED','false').strip().lower() != 'false':
        return jsonify(error='Paper monitor requires LIVE_TRADING_ENABLED=false'),503
    output=[];errors=0
    try:
        _auto_paper_process(max_items=2)
        with _db_connect() as con:
            _ensure_schema(con)
            with con.cursor() as cur:
                cur.execute("SELECT event_id, created_at, symbol, side, timeframe, details FROM luxalgo_simulated_orders ORDER BY created_at DESC LIMIT 10")
                orders=cur.fetchall()
        for order in orders:
            try:
                # Once a simulated outcome has been recorded, never rewrite it after later candles.
                with _db_connect() as con:
                    with con.cursor() as cur:
                        cur.execute('SELECT state,exit_price,estimated_pnl_usdt,last_checked_candle FROM luxalgo_paper_tracking WHERE event_id=%s',(order['event_id'],))
                        saved=cur.fetchone()
                if saved and saved['state'] in ('SL','TP'):
                    d=order['details']
                    output.append(dict(event_id=order['event_id'],created_at=order['created_at'].isoformat(),
                        symbol=order['symbol'],side=order['side'],entry=d['reference_entry'],stop=d['stop_loss'],
                        take=d['take_profit'],state=saved['state'],estimated_pnl_usdt=str(saved['estimated_pnl_usdt']) if saved['estimated_pnl_usdt'] is not None else None))
                    continue
                snapshot=_paper_trade_snapshot(order)
                with _db_connect() as con:
                    with con.cursor() as cur:
                        cur.execute("""INSERT INTO luxalgo_paper_tracking
                           (event_id,checked_at,state,last_checked_candle,exit_price,estimated_pnl_usdt,notes)
                           VALUES (%s,NOW(),%s,%s,%s,%s,'Historical OHLC estimate only; no exchange fill')
                           ON CONFLICT(event_id) DO UPDATE SET checked_at=NOW(),state=EXCLUDED.state,
                           last_checked_candle=EXCLUDED.last_checked_candle,exit_price=EXCLUDED.exit_price,
                           estimated_pnl_usdt=EXCLUDED.estimated_pnl_usdt
                           WHERE luxalgo_paper_tracking.state='OPEN' """,
                           (order['event_id'],snapshot['state'],snapshot['exit_candle'],
                            snapshot['exit_price'],snapshot['estimated_pnl_usdt']))
                output.append(snapshot)
            except (ValueError,HTTPError,URLError,TimeoutError,KeyError,TypeError,OSError,psycopg.Error) as err:
                errors+=1
                app.logger.warning('Paper candle review unavailable for event %s: %s',order['event_id'],type(err).__name__)
                d=order['details']
                output.append(dict(event_id=order['event_id'],created_at=order['created_at'].isoformat(),
                    symbol=order['symbol'],side=order['side'],entry=d.get('reference_entry'),
                    stop=d.get('stop_loss'),take=d.get('take_profit'),state='UNVERIFIABLE',estimated_pnl_usdt=None))
        # Open-only mark-to-market: request-time estimates, never stored as realized P&L.
        open_source=[]
        for order in orders:
            row=next((r for r in output if r['event_id']==order['event_id']),None)
            if row and row['state']=='OPEN':
                open_source.append(dict(event_id=order['event_id'],symbol=order['symbol'],
                    side=order['side'],state='OPEN',entry=row['entry'],
                    size_contracts=order['details'].get('size_contracts')))
        marks,total=_paper_open_marks(open_source)
        for row in output:
            mark=marks.get(row['event_id'],{})
            row['mark_price']=mark.get('mark_price')
            row['unrealized_pnl_usdt']=mark.get('unrealized_pnl_usdt')
        response=jsonify(trades=output,errors=errors,mode='HISTORICAL_PAPER_ONLY',
            open_unrealized_pnl_usdt=str(total) if total is not None else None,
            quote_source='GATE_IO_PUBLIC_LAST_PRICE',orders_placed=0)
        response.headers['Cache-Control']='no-store'
        return response
    except (psycopg.Error,RuntimeError,OSError):
        app.logger.exception('Paper tracking database unavailable')
        return jsonify(error='Paper tracking DB unavailable',orders_placed=0),503



@app.get('/terminal/paper-history')
def terminal_paper_history():
    """Closed simulated trades only. Read-only and separate from verified exchange fills."""
    if not _terminal_permitted():
        return _terminal_guard()
    try:
        with _db_connect() as con:
            with con.cursor() as cur:
                cur.execute("""
                    SELECT o.event_id,o.created_at,o.symbol,o.side,o.timeframe,
                           o.details->>'reference_entry' AS entry,
                           o.details->>'stop_loss' AS stop_loss,
                           o.details->>'take_profit' AS take_profit,
                           t.state,t.exit_price,t.estimated_pnl_usdt,t.checked_at
                    FROM luxalgo_paper_tracking t
                    JOIN luxalgo_simulated_orders o ON o.event_id=t.event_id
                    WHERE t.state IN ('SL','TP')
                    ORDER BY o.created_at DESC LIMIT 100
                """)
                rows=cur.fetchall()
        trades=[dict(event_id=r['event_id'],created_at=r['created_at'].isoformat(),
                     symbol=r['symbol'],side=r['side'],timeframe=r['timeframe'],
                     entry=r['entry'],stop_loss=r['stop_loss'],take_profit=r['take_profit'],
                     state=r['state'],exit_price=str(r['exit_price']) if r['exit_price'] is not None else None,
                     estimated_pnl_usdt=str(r['estimated_pnl_usdt']) if r['estimated_pnl_usdt'] is not None else None,
                     checked_at=r['checked_at'].isoformat() if r['checked_at'] is not None else None)
                for r in rows]
        wins=sum(x['state']=='TP' for x in trades)
        losses=sum(x['state']=='SL' for x in trades)
        pnl_values=[Decimal(x['estimated_pnl_usdt']) for x in trades if x['estimated_pnl_usdt'] is not None]
        response=jsonify(mode='PAPER_HISTORICAL_ESTIMATES_ONLY',real_orders=0,
                         trades=trades,summary=dict(closed=len(trades),tp=wins,sl=losses,
                         win_rate_pct=round(100*wins/len(trades),2) if trades else None,
                         estimated_pnl_usdt=str(sum(pnl_values,Decimal('0'))) if pnl_values else None,
                         pnl_records=len(pnl_values),limit=100))
        response.headers['Cache-Control']='no-store'
        return response
    except (psycopg.Error,RuntimeError,OSError,ValueError):
        app.logger.warning('Paper history temporarily unavailable',exc_info=False)
        return jsonify(error='Paper history unavailable',real_orders=0),503

@app.post('/gate/plan')
def gate_plan():
    """Protected diagnostic plan; no authenticated Gate trading API call."""
    data = request.get_json(silent=True) if request.is_json else None
    if not isinstance(data,dict) or not authorized(data):
        return jsonify(error='Unauthorized'),401
    symbol = str(data.get('symbol','')).upper().removeprefix('BINANCE:').removesuffix('.P')
    side = str(data.get('side','')).upper()
    side = {'BUY':'LONG','SELL':'SHORT','LONG':'LONG','SHORT':'SHORT'}.get(side)
    if not re.fullmatch(r'[A-Z0-9]{3,25}USDT',symbol) or not side:
        return jsonify(error='Expected supported USDT symbol and BUY/SELL side'),422
    try:
        plan = _gate_atr_plan(symbol,side)
        return jsonify(status='illustrative_plan_only',live_trading=False,orders_enabled=False,plan=plan),200
    except HTTPError as exc:
        return jsonify(error='Gate market data unavailable',gate_http_status=exc.code,orders_enabled=False),502
    except (URLError,TimeoutError,ValueError,KeyError,TypeError,OSError):
        return jsonify(error='Market data invalid or temporarily unavailable',orders_enabled=False),502


@app.get('/market/candles')
def public_market_candles():
    """Public chart data only. Never returns account or private trading data."""
    contract = request.args.get('contract', 'BTC_USDT')
    if contract not in ('BTC_USDT', 'ETH_USDT', 'SOL_USDT', 'XRP_USDT'):
        return jsonify(error='Unsupported chart contract'), 422
    url = 'https://api.gateio.ws/api/v4/futures/usdt/candlesticks?' + urlencode(
        {'contract': contract, 'interval': '15m', 'limit': 60})
    req = Request(url, headers={'Accept': 'application/json', 'User-Agent': 'LuxAlgo-PRO-MarketChart/1.0'}, method='GET')
    try:
        with urlopen(req, timeout=8) as res:
            raw = json.loads(res.read(120000))
        if not isinstance(raw, list):
            raise ValueError('Invalid response')
        now = int(time.time())
        candles = []
        for bar in raw:
            t = int(bar['t'])
            if t + 900 > now:
                continue
            o, h, l, c = [float(bar[k]) for k in ('o', 'h', 'l', 'c')]
            if not all(math.isfinite(v) and v > 0 for v in (o,h,l,c)) or h < max(o,l,c) or l > min(o,c):
                raise ValueError('Invalid OHLC')
            candles.append({'t': t, 'o': o, 'h': h, 'l': l, 'c': c})
        candles = sorted({b['t']: b for b in candles}.values(), key=lambda b:b['t'])[-55:]
        response = jsonify(contract=contract, timeframe='15m', candles=candles, orders_enabled=False)
        response.headers['Cache-Control'] = 'public, max-age=30'
        return response
    except (HTTPError, URLError, TimeoutError, ValueError, KeyError, TypeError, OSError):
        return jsonify(error='Market data temporarily unavailable', orders_enabled=False), 502


# Public, read-only technical scanner. Never fetch private account data or send orders.
_scanner_lock = Lock()
_scanner_cache = {'at': 0.0, 'data': None}


def _scan_http_json(path):
    """Production PUBLIC futures GET, with official live-only host fallback.

    Market data is independent of account authentication and both order switches.
    Testnet endpoints are never consulted. Any unavailable feed fails explicitly.
    """
    parsed=urlsplit(path)
    if (parsed.scheme or parsed.netloc or parsed.fragment or
        parsed.path not in ('/api/v4/futures/usdt/tickers',
                            '/api/v4/futures/usdt/candlesticks')):
        raise ValueError('Only approved live USDT futures public feeds are permitted')
    last_error = None
    for host in ('https://api.gateio.ws', 'https://fx-api.gateio.ws'):
        req = Request(host + path, headers={
            'Accept':'application/json','User-Agent':'LuxAlgo-PRO-PublicScanner/1.1'}, method='GET')
        try:
            with urlopen(req, timeout=7) as res:
                data=json.loads(res.read(2000000))
            if not isinstance(data,list):
                raise ValueError('Invalid Gate Futures public feed')
            return data
        except (HTTPError,URLError,TimeoutError,OSError,ValueError,TypeError) as exc:
            last_error=exc
            app.logger.warning('Live public futures GET failed via %s: %s',
                               urlsplit(host).hostname,type(exc).__name__)
    raise RuntimeError('Both official live Futures public feeds unavailable') from last_error


def _ema(values, n):
    acc = sum(values[:n])/n
    for v in values[n:]:
        acc += (v-acc)*(2/(n+1))
    return acc


def _rsi(closes, n=14):
    diffs=[b-a for a,b in zip(closes[-(n+1):-1], closes[-n:])]
    up=sum(max(x,0) for x in diffs)/n
    down=sum(max(-x,0) for x in diffs)/n
    if up == 0 and down == 0:
        return 50.0
    if down == 0:
        return 100.0
    return 100-100/(1+up/down)


def _scan_contract(t):
    contract = t['contract']
    data = _scan_http_json('/api/v4/futures/usdt/candlesticks?' + urlencode(
        {'contract':contract, 'interval':'15m', 'limit':55}))
    now=int(time.time())
    completed=[]
    for c in data:
        start=int(c['t'])
        if start+900 > now: continue
        close=float(c['c'])
        if close<=0 or not math.isfinite(close): continue
        completed.append((start,close))
    completed=sorted(set(completed))
    if len(completed)<23: raise ValueError('Insufficient candles')
    recent=completed[-23:]
    if any(b[0]-a[0]!=900 for a,b in zip(recent,recent[1:])):
        raise ValueError('Candle data gaps')
    closes=[v for _,v in completed]
    ema9=_ema(closes,9);ema21=_ema(closes,21);rsi=_rsi(closes)
    change=float(t.get('change_percentage') or 0)
    score=sum((25 if ema9>ema21 else -25, 20 if closes[-1]>ema9 else -20,
               20 if rsi>55 else -20 if rsi<45 else 0,
               15 if change>2 else -15 if change< -2 else 0))
    bias='BULLISH' if score>=35 else 'BEARISH' if score<=-35 else 'MIXED'
    return {'contract':contract,'bias':bias,'score':score,'change_24h':change,'rsi':round(rsi,1)}


@app.get('/market/scanner')
def public_futures_scanner():
    now=time.time()
    with _scanner_lock:
        if _scanner_cache['data'] is not None and now-_scanner_cache['at']<120:
            return jsonify(_scanner_cache['data'])
        try:
            tickers=_scan_http_json('/api/v4/futures/usdt/tickers')
            if not isinstance(tickers,list): raise ValueError('Unexpected tickers response')
            good=[]
            for t in tickers:
                try:
                    symbol=str(t['contract'])
                    vol=float(t.get('volume_24h_quote') or 0)
                    if re.fullmatch(r'[A-Z0-9]{2,24}_USDT',symbol) and math.isfinite(vol) and vol>=3000000:
                        good.append((vol,t))
                except (TypeError,ValueError,KeyError):
                    continue
            good.sort(key=lambda x:-x[0])
            chosen=[t for _,t in good[:10]]
            # Bound calls and avoid overlapping scans from multiple viewers.
            from concurrent.futures import ThreadPoolExecutor, as_completed
            results=[];failed=0
            with ThreadPoolExecutor(max_workers=5) as pool:
                futures=[pool.submit(_scan_contract,t) for t in chosen]
                for future in as_completed(futures):
                    try: results.append(future.result())
                    except (HTTPError,URLError,TimeoutError,ValueError,KeyError,TypeError,OSError): failed+=1
            results.sort(key=lambda d:(-abs(d['score']),d['contract']))
            payload={'generated_at':int(time.time()),'analyzed':len(results),'failed':failed,
                     'bullish':sum(x['bias']=='BULLISH' for x in results),
                     'bearish':sum(x['bias']=='BEARISH' for x in results),
                     'mixed':sum(x['bias']=='MIXED' for x in results),'results':results,
                     'orders_enabled':False,'kind':'public_rule_based_scan'}
            _scanner_cache.update(at=time.time(),data=payload)
            return jsonify(payload)
        except (HTTPError,URLError,TimeoutError,ValueError,KeyError,TypeError,OSError,RuntimeError):
            return jsonify(error='Gate.io public market scan temporarily unavailable',orders_enabled=False),502



# Market intelligence endpoints, public market data protected behind terminal login.
# No Gate.io account data is returned and no trade orders are implemented.
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Thread

_radar_cache = {'at': 0.0, 'data': None}
_radar_lock = Lock()
_scan_state = {tf: {'at':0., 'running':False, 'results':{}, 'total':0, 'scanned':0, 'failed':0}
               for tf in ('5m','15m')}
_scan_state_lock = Lock()


def _radar_data():
    now = time.time()
    with _radar_lock:
        if _radar_cache['data'] is not None and now - _radar_cache['at'] < 45:
            return _radar_cache['data']
        items = _scan_http_json('/api/v4/futures/usdt/tickers')
        if not isinstance(items, list):
            raise ValueError('Invalid tickers')
        results = []
        for x in items:
            try:
                sym = x['contract']
                if not re.fullmatch(r'[A-Z0-9]{2,28}_USDT',sym):
                    continue
                last = float(x.get('last') or 0)
                change = float(x.get('change_percentage') or 0)
                vol = float(x.get('volume_24h_quote') or 0)
                if not all(math.isfinite(v) for v in (last,change,vol)) or last <= 0 or vol < 0:
                    continue
                results.append({'contract':sym,'last':last,'change':change,'volume':vol})
            except (KeyError,TypeError,ValueError):
                continue
        payload = {'results':results,'total':len(results),'generated_at':int(now),'orders_enabled':False}
        _radar_cache.update(at=now,data=payload)
        return payload


@app.get('/terminal/radar')
def terminal_radar():
    if not _terminal_permitted():
        return _terminal_guard()
    try:
        response = jsonify(_radar_data())
        response.headers['Cache-Control'] = 'private, max-age=10'
        return response
    except (HTTPError,URLError,OSError,TimeoutError,ValueError,TypeError,RuntimeError):
        app.logger.warning('Radar ticker feed unavailable', exc_info=True)
        return jsonify(error='Live Gate.io public Futures feed unavailable; execution remains separate'),502


def _scan_one_pair(symbol,tf,change):
    # Completed candles only; technical heuristic, not AI prediction.
    secs = 300 if tf == '5m' else 900
    bars = _scan_http_json('/api/v4/futures/usdt/candlesticks?' + urlencode(
        {'contract':symbol,'interval':tf,'limit':58}))
    now = int(time.time())
    usable = []
    for b in bars:
        try:
            t = int(b['t']); c = float(b['c']); h = float(b['h']); l = float(b['l']); v = float(b.get('v') or 0)
            if t + secs <= now and all(math.isfinite(z) for z in (c,h,l,v)) and c>0 and h>=c>=l and v>=0:
                usable.append((t,c,h,l,v))
        except (KeyError,ValueError,TypeError):
            continue
    usable = sorted({x[0]:x for x in usable}.values())
    if len(usable)<26 or any(b[0]-a[0]!=secs for a,b in zip(usable[-26:-1],usable[-25:])):
        raise ValueError('Insufficient consecutive candles')
    # Wilder ATR(14) on completed candles; must not use an in-progress bar.
    true_ranges = [max(cur[2]-cur[3], abs(cur[2]-prev[1]), abs(cur[3]-prev[1]))
                   for prev,cur in zip(usable[:-1],usable[1:])]
    atr14 = sum(true_ranges[:14])/14
    for tr in true_ranges[14:]:
        atr14 = (atr14*13 + tr)/14
    if not math.isfinite(atr14) or atr14<=0:
        raise ValueError('Invalid completed-candle ATR')
    closes = [b[1] for b in usable]; current = closes[-1]
    ema9 = _ema(closes,9); ema21 = _ema(closes,21); rsi = _rsi(closes)
    h20 = max(b[2] for b in usable[-21:-1]); l20 = min(b[3] for b in usable[-21:-1])
    vols = [b[4] for b in usable]; avg = sum(vols[-21:-1])/20
    vr = vols[-1]/avg if avg>0 else 0
    bull_break = current>h20 and ema9>ema21 and rsi>=55 and vr>=1.15
    bear_break = current<l20 and ema9<ema21 and rsi<=45 and vr>=1.15
    score = int((25 if ema9>ema21 else -25) + (20 if current>ema9 else -20)
                + (15 if rsi>55 else -15 if rsi<45 else 0)
                + (20 if current>h20 else -20 if current<l20 else 0)
                + (10 if change>1 else -10 if change< -1 else 0))
    return {'contract':symbol,'bias':'bullish' if bull_break else 'bearish' if bear_break else 'mixed',
            'score':score,'rsi':round(rsi,2),'volume_ratio':round(vr,2),'timeframe':tf,
            'reference_price':current,'support':l20,'resistance':h20,'atr14':atr14,'candle_at':usable[-1][0]}


def _run_scan(tf):
    try:
        # Scan a bounded, liquidity-ranked universe; unbounded all-contract scans
        # caused long stalls and exchange rate-limit failures on the dashboard.
        all_tickers = _radar_data()['results']
        tickers = sorted(all_tickers, key=lambda item: item['volume'], reverse=True)[:36]
        with _scan_state_lock:
            _scan_state[tf]['total'] = len(tickers)
        # Bounded concurrency avoids excessive simultaneous exchange requests.
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(_scan_one_pair,t['contract'],tf,t['change']) for t in tickers]
            for f in as_completed(futures):
                try:
                    result = f.result()
                    with _scan_state_lock:
                        _scan_state[tf]['results'][result['contract']] = result
                        _scan_state[tf]['scanned'] += 1
                except (HTTPError,URLError,TimeoutError,OSError,KeyError,ValueError,TypeError,ZeroDivisionError,RuntimeError):
                    with _scan_state_lock:
                        _scan_state[tf]['failed'] += 1
    except (HTTPError,URLError,TimeoutError,OSError,KeyError,ValueError,TypeError,RuntimeError):
        app.logger.warning('Technical scanner could not access market data', exc_info=True)
    finally:
        with _scan_state_lock:
            _scan_state[tf]['running'] = False
            _scan_state[tf]['at'] = time.time()


@app.get('/terminal/scanner')
def terminal_scanner():
    if not _terminal_permitted():
        return _terminal_guard()
    tf = request.args.get('tf','15m')
    if tf not in ('5m','15m'):
        return jsonify(error='Unsupported timeframe'),422
    with _scan_state_lock:
        state = _scan_state[tf]
        due = state['at'] == 0 or time.time()-state['at'] > 600
        if request.args.get('refresh') == '1':
            due = True
        if due and not state['running']:
            state.update(running=True,results={},scanned=0,failed=0,total=0,at=time.time())
            Thread(target=_run_scan,args=(tf,),daemon=True).start()
        payload = {'results':list(state['results'].values()),'running':state['running'],
                   'total':state['total'],'scanned':state['scanned'],'failed':state['failed'],
                   'generated_at':int(state['at']),'timeframe':tf,'orders_enabled':False}
    response=jsonify(payload)
    response.headers['Cache-Control']='no-store'
    return response


@app.get('/terminal/advanced-scanner')
def terminal_advanced_scanner():
    """Read-only progressive multi-timeframe market screen; no execution paths."""
    if not _terminal_permitted():
        return _terminal_guard()
    now=time.time()
    # Reuse both bounded public market scans. Never run an unbounded second scan engine.
    with _scan_state_lock:
        active_any=any(q['running'] for q in _scan_state.values())
        for tf in ('15m','5m'):
            st=_scan_state[tf]
            due=(not st['at'] or now-st['at']>600 or (request.args.get('refresh')=='1' and now-st['at']>45))
            if due and not st['running'] and not active_any:
                st.update(running=True,results={},scanned=0,failed=0,total=0,at=now)
                Thread(target=_run_scan,args=(tf,),daemon=True).start()
                active_any=True
        snapshots={tf: {'running':st['running'],'total':st['total'],'scanned':st['scanned'],
                        'failed':st['failed'],'at':int(st['at']),'results':dict(st['results'])}
                   for tf,st in _scan_state.items()}
    # Fail closed on market-data unavailability. Public tickers are from the existing radar.
    try:
        tickers={t['contract']:t for t in _radar_data()['results']}
    except (HTTPError,URLError,TimeoutError,OSError,KeyError,ValueError,TypeError,RuntimeError):
        return jsonify(error='Market liquidity feed unavailable; NO TRADE',orders_enabled=False),503
    equity=None;available=None;open_count=None
    try:
        acct=_gate_futures_account_summary();equity=float(acct['equity_usdt']);available=float(acct['available_usdt'])
        open_count=len(_gate_read_only_positions())
    except (HTTPError,URLError,TimeoutError,OSError,KeyError,ValueError,TypeError,RuntimeError):
        pass
    # Both contract + timeframe must match, and genuine level records must be recent.
    fresh=[]
    try:
        with _db_connect() as con:
            _ensure_schema(con)
            with con.cursor() as cur:
                cur.execute("""SELECT r.symbol,r.side,r.timeframe,r.received_at,l.entry_price,l.stop_loss,l.take_profit
                               FROM luxalgo_signal_receipts r JOIN luxalgo_signal_levels l ON r.event_id=l.event_id
                               WHERE l.status='VALID' AND r.received_at>NOW()-INTERVAL '3 minutes'
                               ORDER BY r.received_at DESC LIMIT 80""")
                fresh=[dict(z) for z in cur.fetchall()]
    except (psycopg.Error,RuntimeError,OSError):
        pass
    s5=snapshots['5m']['results'];s15=snapshots['15m']['results']
    candidates=[]
    for sym, p15 in s15.items():
        p5=s5.get(sym);ticker=tickers.get(sym)
        if not p5 or not ticker or ticker['volume']<1000000:continue
        # Agreeing completed-candle trends, expanded 5m volume and active directional move.
        islong=p15['score']>=35 and p5['score']>=30 and p5['volume_ratio']>=1.15
        isshort=p15['score']<=-35 and p5['score']<=-30 and p5['volume_ratio']>=1.15
        if not (islong or isshort):continue
        side='LONG' if islong else 'SHORT'
        score=abs(p15['score'])+abs(p5['score'])+min(30,p5['volume_ratio']*10)
        signal=next((a for a in fresh if a['symbol'].replace('_','')==sym.replace('_','') and a['side']==side
                     and a['timeframe'] in ('5m','15m')),None)
        # Technical plan is a non-executable, completed-candle ATR estimate.
        # A market watchlist never becomes an authenticated LuxAlgo order.
        entry=sl=tp=rr=None;size_hint='—';confirmation='PENDING';state='WATCHLIST ONLY'
        level_source='UNAVAILABLE';reason='Technical market reference unavailable'
        technical_atr=p15.get('atr14');technical_entry=p15.get('reference_price')
        try:
            e=float(technical_entry);atr=float(technical_atr)
            if not all(math.isfinite(x) and x>0 for x in (e,atr)):
                raise ValueError('Invalid technical reference/ATR')
            stop_distance=1.5*atr
            if stop_distance>=e:
                raise ValueError('ATR stop too large relative to reference')
            entry=e
            sl=e-stop_distance if islong else e+stop_distance
            tp=e+2*stop_distance if islong else e-2*stop_distance
            if tp<=0:raise ValueError('Invalid technical target')
            rr=2.0
            level_source='TECHNICAL ESTIMATE'
            reason='ATR-based technical preview; LuxAlgo confirmation pending'
            if equity is None or available is None or open_count is None:
                reason+='; verified account sizing unavailable'
            elif equity>0 and available>0 and open_count<5:
                # Preview BASE units only, NOT an executable Gate.io contract quantity.
                qty=min(equity*.005/stop_distance, available*5/e)
                if math.isfinite(qty) and qty>0:
                    size_hint=f'{qty:.8g} base units (technical estimate)'
            elif open_count>=5:
                reason+='; five exchange positions already open'
            else:
                reason+='; available balance not positive'
        except (ValueError,TypeError,ZeroDivisionError,OverflowError):
            reason='No valid 15m ATR technical estimate available'
        if signal:
            try:
                se=float(signal['entry_price']);ss=float(signal['stop_loss']);st=float(signal['take_profit'])
                valid=all(math.isfinite(x) and x>0 for x in (se,ss,st)) and ((ss<se<st) if islong else (st<se<ss))
                if not valid:raise ValueError('Invalid directional verified alert levels')
                entry,sl,tp=se,ss,st
                rr=round(abs(st-se)/abs(se-ss),2)
                confirmation='VALIDATED ALERT'
                level_source='STORED LUXALGO LEVELS'
                reason='Verified signal levels (within 3m); exchange execution remains BLOCKED'
                size_hint='—'
                if equity is not None and available is not None and open_count is not None and open_count<5 and equity>0 and available>0:
                    qty=min(equity*.005/abs(se-ss),available*5/se)
                    if math.isfinite(qty) and qty>0:
                        size_hint=f'{qty:.8g} base units (indicative)'
                elif open_count is not None and open_count>=5:
                    reason+='; maximum five positions'
                else:reason+='; account sizing unavailable'
                if rr<1.5:reason+='; R:R below 1.5 preference'
            except (ValueError,TypeError,ZeroDivisionError,OverflowError):
                confirmation='INVALID ALERT'
                reason='Stored LuxAlgo levels invalid; '+reason
        # Never allow a watchlist candidate to become an executable order.
        candidates.append({'symbol':sym.replace('_',''),'side':side,'timeframe':'5m + 15m',
                           'confirmation':confirmation,'level_source':level_source,'entry':entry,'sl1':sl,'tp1':tp,'rr':rr,
                           'size_hint':size_hint,'status':state,'reason':reason,'score':round(score,1),'volume':ticker['volume']})
    # Score combines 5m and 15m momentum; never label as executable.
    candidates.sort(key=lambda p:(-p['score'],-p['volume'],p['symbol']))
    candidates=candidates[:5]
    payload={'candidates':candidates,'scan':{tf:{k:v for k,v in st.items() if k!='results'} for tf,st in snapshots.items()},
             'running':any(st['running'] for st in snapshots.values()),'updated_at':int(now),
             'equity_usdt':equity,'risk_budget_usdt':round(equity*.005,8) if equity is not None else None,
             'open_positions':open_count,'orders_enabled':False,'live_trading':False,'execution_status':'BLOCKED'}
    res=jsonify(payload);res.headers['Cache-Control']='no-store';return res


# --- Read-only Gate.io order diagnostics (no order-write route) ---
def _gate_v4_authenticated_request(method, path, *, payload=None, timeout=8):
    """Strict authenticated GET for a single futures order; no write capability."""
    if (method != 'GET' or payload is not None or
            not re.fullmatch(r'/api/v4/futures/usdt/orders/[1-9][0-9]{0,19}',str(path))):
        raise ValueError('Only read-only futures order GET is authorized')
    key=os.getenv('GATE_API_KEY','').strip()
    secret=os.getenv('GATE_API_SECRET','').strip()
    if not key or not secret:
        raise RuntimeError('Gate.io credentials unavailable')
    stamp=str(int(time.time()))
    hashed=hashlib.sha512(b'').hexdigest()
    msg='\n'.join(('GET',path,'',hashed,stamp))
    sign=hmac.new(secret.encode(),msg.encode(),hashlib.sha512).hexdigest()
    req=Request('https://api.gateio.ws'+path,method='GET',
        headers={'KEY':key,'Timestamp':stamp,'SIGN':sign,'Accept':'application/json'})
    try:
        with urlopen(req,timeout=timeout) as response:
            result=json.loads(response.read(100000))
    except (HTTPError,URLError,TimeoutError,OSError,ValueError):
        raise RuntimeError('Gate.io order readback unavailable') from None
    if not isinstance(result,dict):
        raise RuntimeError('Gate.io order response invalid')
    return result


def _five_journal_verify_exchange_order(event_id, exchange_order_id):
    """Verify known order identity by authenticated GET; never submit trades.

    Production has no five-position journal yet. This diagnostic requires the
    expected event-derived text and confirms only order identity, not a fill.
    """
    if (not isinstance(event_id,str) or
            not re.fullmatch(r'[A-Za-z0-9_.:-]{4,100}',event_id) or
            not re.fullmatch(r'[1-9][0-9]{0,19}',str(exchange_order_id))):
        raise ValueError('Valid event and existing numeric order ID required')
    expected_text='t-lux-'+hashlib.sha256(event_id.encode('utf8')).hexdigest()[:24]
    order=_gate_v4_authenticated_request(
        'GET','/api/v4/futures/usdt/orders/'+str(exchange_order_id))
    return {'verified':(str(order.get('id_string') or order.get('id'))==str(exchange_order_id)
                       and order.get('text')==expected_text),
            'order_id':str(exchange_order_id),'orders_placed':0,
            'fill_verified':False,'protection_verified':False}


def _testnet_one_shot_trial_worker():
    """One-shot protected Testnet trial; requires independent opt-in flags.

    No production keys. A fixed persistent PostgreSQL reservation prevents
    duplicate entries across processes/redeploys and uncertain outcomes.
    """
    try:
        from testnet_one_shot_trial import run_one_shot_protected_testnet_trial
        result=run_one_shot_protected_testnet_trial()
        app.logger.info("One-shot Testnet trial state: %s",result.get('state','UNKNOWN'))
        if result.get('state')!='PROTECTED_VERIFIED':
            app.logger.warning('Testnet trial not verified; real trading remains disabled')
    except Exception as exc:
        app.logger.error('Testnet one-shot trial halted: %s',type(exc).__name__)

# Retired Testnet one-shot starter is intentionally absent from production.

def _testnet_startup_readiness_probe():
    """Read-only Testnet account diagnostic. No entry orders, even when armed."""
    try:
        from testnet_readiness import inspect_testnet_readiness
        mode_result=None
        if os.getenv('GATE_TESTNET_PREPARE_HEDGE_MODE','false').lower()=='true':
            from testnet_mode_prepare import prepare_testnet_hedge_mode
            mode_result=prepare_testnet_hedge_mode()
        leverage_result=None
        if os.getenv('GATE_TESTNET_PREPARE_LEVERAGE_5X','false').lower()=='true':
            from testnet_leverage_prepare import prepare_testnet_leverage_5x
            leverage_result=prepare_testnet_leverage_5x()
        report=inspect_testnet_readiness()
        # Read-only one-contract trial sizing check, persisted for safe diagnosis.
        # Never schedules or submits an exchange order.
        if report.get('ready') is True:
            from testnet_one_shot_trial import inspect_one_shot_trial_preflight
            report['one_shot_trial_preflight']=inspect_one_shot_trial_preflight()
        if mode_result is not None:
            report['mode_setup_state']=mode_result.get('state','UNKNOWN')
        if leverage_result is not None:
            report['leverage_setup_state']=leverage_result.get('state','UNKNOWN')
        with _db_connect() as con:
            _ensure_schema(con)
            con.execute("""INSERT INTO luxalgo_testnet_readiness(id,state,details,checked_at)
                VALUES(1,%s,%s::jsonb,NOW())
                ON CONFLICT(id) DO UPDATE SET state=excluded.state,
                    details=excluded.details,checked_at=excluded.checked_at""",
                ('READY' if report.get('ready') else 'BLOCKED',json.dumps(report,allow_nan=False)))
        app.logger.info("Testnet readiness checked; ready=%s; one_shot_preflight=%s",
                        report.get('ready'),(report.get('one_shot_trial_preflight') or {}).get('state','NOT_CHECKED'))
    except (psycopg.Error,RuntimeError,OSError,ValueError,TypeError) as exc:
        app.logger.warning('Testnet readiness startup probe failed safely: %s',type(exc).__name__)


# Production startup checks ONLY public live USDT Futures market feeds.
# This is deliberately independent of trading credentials and order flags.
# Probe once per deployment; never submit or prepare an exchange order.
def _probe_live_public_market_once():
    feeds=(('TICKERS','/api/v4/futures/usdt/tickers'),
           ('CANDLES','/api/v4/futures/usdt/candlesticks?contract=BTC_USDT&interval=5m&limit=60'))
    for name,path in feeds:
        try:
            data=_scan_http_json(path)
            if not data:
                raise ValueError('Live market feed returned no records')
            print('LIVE_FUTURES_MARKET_'+name+'_OK records='+str(len(data)),flush=True)
        except (HTTPError,URLError,TimeoutError,OSError,ValueError,TypeError,RuntimeError) as exc:
            app.logger.warning('LIVE_FUTURES_MARKET_%s_UNAVAILABLE reason=%s',
                               name,type(exc).__name__)
    # Publisher feeds are independent of Testnet and do not authorize trades.
    # Startup evidence is limited to counts; never log article content or secrets.
    with ThreadPoolExecutor(max_workers=len(NEWS_FEEDS)) as pool:
        news_jobs={pool.submit(_news_fetch_source,source):source[0] for source in NEWS_FEEDS}
        for task in as_completed(news_jobs):
            label=news_jobs[task]
            try:
                headlines=task.result()
                print('CRYPTO_NEWS_PROVIDER_'+label.replace(' ','_')+'_OK records='+str(len(headlines)),flush=True)
            except (HTTPError,URLError,TimeoutError,OSError,ValueError,TypeError,ET.ParseError) as exc:
                app.logger.warning('CRYPTO_NEWS_PROVIDER_%s_UNAVAILABLE reason=%s',
                                   label.replace(' ','_'),type(exc).__name__)

    # Verify real 5m/15m technical calculations with public completed BTC candles.
    # These diagnostics cannot produce LuxAlgo trade signals or exchange orders.
    for tf in ('5m','15m'):
        try:
            scan=_scan_one_pair('BTC_USDT',tf,0.0)
            print('LIVE_FUTURES_TECHNICAL_'+tf+'_OK score='+str(scan['score'])+
                  ' candle_at='+str(scan['candle_at']),flush=True)
        except (HTTPError,URLError,TimeoutError,OSError,ValueError,TypeError,RuntimeError,KeyError,ZeroDivisionError) as exc:
            app.logger.warning('LIVE_FUTURES_TECHNICAL_%s_UNAVAILABLE reason=%s',
                               tf,type(exc).__name__)
    # Authenticated GET only. Never logs private balances, keys, or positions.
    try:
        account=_gate_futures_account_summary()
        positions=_gate_read_only_positions()
        if account.get('currency')!='USDT':
            raise ValueError('Unexpected live Futures settlement currency')
        print('LIVE_FUTURES_ACCOUNT_READONLY_OK positions='+str(len(positions)),flush=True)
    except (HTTPError,URLError,TimeoutError,OSError,ValueError,TypeError,RuntimeError,KeyError) as exc:
        app.logger.warning('LIVE_FUTURES_ACCOUNT_READONLY_UNAVAILABLE reason=%s',
                           type(exc).__name__)

if os.getenv('PRODUCTION_TERMINAL_VIEW','false').strip().lower()=='true':
    from threading import Thread
    Thread(target=_probe_live_public_market_once,daemon=True,
           name='gate-live-public-market-probe').start()




# Initialize and report the authoritative operator switch on production boot.
# Safe even with zero TradingView alerts; does not affect public market data.
def _production_operator_bootstrap():
    try:
        from production_bot_control import state
        with _db_connect() as con:
            status=state(con)
        print('LIVE_BOT_SERVER_STATE_' + ('ON' if status['bot_on'] else 'OFF') +
              ' source_locked=' + str('SOURCE_LOCKED' in status['safety_gates']) +
              ' halt=' + str(status['halt_active']),flush=True)
    except (psycopg.Error,RuntimeError,OSError,ValueError,TypeError,KeyError) as exc:
        app.logger.warning('LIVE_BOT_SERVER_STATE_UNAVAILABLE error=%s',
                           type(exc).__name__)

if os.getenv('PRODUCTION_TERMINAL_VIEW','false').strip().lower()=='true':
    from threading import Thread
    Thread(target=_production_operator_bootstrap,daemon=True,
           name='luxalgo-production-operator-bootstrap').start()


# Independently reconcile the production exchange after process restart and
# periodically thereafter. This worker NEVER submits or cancels Gate orders.
def _production_readonly_recovery_monitor():
    from live_execution_coordinator import reconcile_and_halt_if_needed
    while True:
        try:
            with _db_connect() as con:
                result=reconcile_and_halt_if_needed(con)
            print("LIVE_RECONCILIATION_"+result["status"]+
                  " positions="+str(result["open_positions"])+
                  " orders="+str(result["exchange_open_orders"])+
                  " triggers="+str(result["exchange_open_triggers"]),flush=True)
        except Exception as exc:
            # Unknown is not flat. The coordinator attempts to persist HALT.
            app.logger.warning('LIVE_RECONCILIATION_BLOCKED error_type=%s',
                               type(exc).__name__)
        time.sleep(60)


def _production_execution_offline_smoke():
    """Pure fixture checks in deployed Python; never read private keys or POST."""
    try:
        from live_futures_engine import (
            GateLiveClient,LiveSafetyError,EXCHANGE_WRITE_RELEASE,
            build_plan,build_entry_order,build_protective_trigger,
            evaluate_protection,reconcile_ledger,client_text)
        from live_execution_orchestrator import (
            execute_confirmed_signal,full_close_oneway_order)
        from live_execution_coordinator import status_fallback
        assert EXCHANGE_WRITE_RELEASE is False
        now=int(time.time())
        s=dict(event_id="startup-diagnostic-BTC-5m",contract="BTC_USDT",
               side="LONG",timeframe="5m",levels_status="VALID",
               received_epoch=now,entry="80000",sl="79200",tp="81600",
               position_mode="single",leverage="5")
        meta=dict(name="BTC_USDT",quanto_multiplier="0.0001",
                  order_size_min="1",order_size_max="100000",taker_fee_rate="0.00075")
        p=build_plan(s,meta,1000,1000,[],now=now)
        assert p["contracts"]==46
        assert build_entry_order(p)["size"]==46
        stop=build_protective_trigger(p,"SL")
        take=build_protective_trigger(p,"TP")
        book=[dict(stop,status="open",id=10001),dict(take,status="open",id=10002)]
        assert evaluate_protection(p,book)["verified"] is True
        assert evaluate_protection(p,book[:1])["verified"] is False
        assert full_close_oneway_order(p)["reduce_only"] is True
        assert reconcile_ledger([],[dict(contract="BTC_USDT",size="1")],[])["requires_halt"]
        assert status_fallback("API_FAILURE")["open_positions"] is None
        try:
            GateLiveClient(key="fixture",secret="fixture").submit_entry(p)
            raise AssertionError("Order-write source lock bypass")
        except LiveSafetyError:
            pass
        try:
            execute_confirmed_signal(lambda:None,p)
            raise AssertionError("Auto-execution source lock bypass")
        except LiveSafetyError:
            pass
        print("LIVE_EXECUTION_OFFLINE_SAFETY_FIXTURES_PASS source_locked=TRUE",
              flush=True)
    except Exception as exc:
        app.logger.error('LIVE_EXECUTION_OFFLINE_SAFETY_FIXTURES_FAILED error_type=%s',
                         type(exc).__name__)

if os.getenv('PRODUCTION_TERMINAL_VIEW','false').strip().lower()=='true':
    from threading import Thread
    Thread(target=_production_execution_offline_smoke,daemon=True,
           name="luxalgo-live-safety-fixtures").start()
    Thread(target=_production_readonly_recovery_monitor,daemon=True,
           name="luxalgo-live-readonly-recovery").start()
    # Execute ALL mocked regression suites on Render startup without credentials.
    # This complements GitHub Actions, whose push runs are not visible in the
    # available connector. No POST, no payment, no market-side changes.
    def _run_nontrading_regression_suite():
        import subprocess,sys
        clean_env={k:v for k,v in os.environ.items() if
                   k in ('PATH','PYTHONPATH','PYTHONHOME','LANG','LC_ALL','HOME',
                         'VIRTUAL_ENV','PYTHONDONTWRITEBYTECODE')}
        clean_env.update(PRODUCTION_TERMINAL_VIEW='false',
                         LIVE_TRADING_ENABLED='false',ORDERS_ENABLED='false')
        try:
            finished=subprocess.run(
                [sys.executable,'-m','unittest','discover','-s','tests',
                 '-p','test_live_*.py','-v'],
                stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                timeout=45,env=clean_env,text=True)
            lines=finished.stdout.strip().splitlines()
            detail=' | '.join(lines[-14:])[-2400:]
            print('LIVE_REGRESSION_SUITE_'+
                  ('PASS' if finished.returncode==0 else 'FAIL')+
                  ' exit='+str(finished.returncode)+' evidence='+detail,flush=True)
        except (OSError,subprocess.TimeoutExpired,ValueError) as exc:
            print('LIVE_REGRESSION_SUITE_ERROR '+type(exc).__name__,flush=True)
    Thread(target=_run_nontrading_regression_suite,daemon=True,
           name='luxalgo-noorder-regression').start()



# One-shot read-only acceptance snapshot (requested by account owner).
# Persists only whitelisted USDT equity/contract metadata, never API keys or
# raw authenticated responses. No exchange writes or trading permissions.
def _capture_gate_live_acceptance_snapshot():
    try:
        from live_futures_engine import GateLiveClient,PREFIX
        now=datetime.now(timezone.utc)
        summary=_gate_futures_account_summary()
        client=GateLiveClient()
        snap=client.snapshot()
        contract=client.request('GET',PREFIX+'/contracts/BTC_USDT')
        ticks=_scan_http_json('/api/v4/futures/usdt/tickers?contract=BTC_USDT')
        ticker=next((x for x in ticks if x.get('contract')=='BTC_USDT'),None)
        if not isinstance(contract,dict) or not ticker or contract.get('name')!='BTC_USDT':
            raise ValueError('BTC USDT metadata not verified')
        # Any uncertainty over actual account position mode remains UNKNOWN.
        # Do not auto-derive single from an empty /positions list.
        item=dict(
            status='READ_ONLY_VERIFIED',contract='BTC_USDT',
            timestamp_utc=now.isoformat(),
            balance_usdt=summary['balance_usdt'],
            equity_usdt=summary['equity_usdt'],
            available_usdt=summary['available_usdt'],
            unrealized_pnl_usdt=summary['unrealized_pnl_usdt'],
            account_source=summary['source'],
            exchange_positions=len(snap['positions']),
            exchange_open_orders=len(snap['orders']),
            exchange_trigger_orders=len(snap['triggers']),
            position_mode=(snap['account'].get('position_mode')
                if snap['account'].get('position_mode') in ('single','dual','dual_plus')
                else 'UNKNOWN'),
            last_price=ticker.get('last'),
            mark_price=ticker.get('mark_price'),
            best_bid=ticker.get('highest_bid'),
            best_ask=ticker.get('lowest_ask'),
            quanto_multiplier=contract.get('quanto_multiplier'),
            order_size_min=contract.get('order_size_min'),
            order_size_max=contract.get('order_size_max'),
            market_order_size_max=contract.get('market_order_size_max'),
            order_price_round=contract.get('order_price_round'),
            taker_fee_rate=contract.get('taker_fee_rate'),
            market_order_slip_ratio=contract.get('market_order_slip_ratio'),
            leverage_max=contract.get('leverage_max'),
            in_delisting=contract.get('in_delisting'))
        with _db_connect() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS luxalgo_live_acceptance_readonly (
                captured_at TIMESTAMPTZ PRIMARY KEY,
                contract TEXT NOT NULL CHECK(contract='BTC_USDT'),
                evidence JSONB NOT NULL)""")
            con.execute("""INSERT INTO luxalgo_live_acceptance_readonly
                (captured_at,contract,evidence) VALUES (%s,%s,%s::jsonb)""",
                (now,'BTC_USDT',json.dumps(item,allow_nan=False)))
        print('LIVE_ACCEPTANCE_READONLY_SNAPSHOT_OK contract=BTC_USDT '
              'market=GATE_IO_LIVE position_mode='+
              str(item['position_mode']),flush=True)
    except Exception as exc:
        app.logger.warning('LIVE_ACCEPTANCE_READONLY_SNAPSHOT_BLOCKED type=%s',
                           type(exc).__name__)

if os.getenv('PRODUCTION_TERMINAL_VIEW','false').strip().lower()=='true':
    from threading import Thread
    Thread(target=_capture_gate_live_acceptance_snapshot,daemon=True,
           name='gate-live-acceptance-snapshot').start()


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '8080')))
