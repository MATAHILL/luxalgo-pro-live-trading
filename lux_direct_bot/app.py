"""LUX DIRECT BOT — isolated, dry-run-only LuxAlgo webhook receiver.
Production execution is intentionally locked until external Binance acceptance tests.
"""
import hashlib
import hmac
import json
import os
import re
import sqlite3
import time

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from dashboard import DASHBOARD_HTML
from lite import LITE_HTML
from connect import CONNECT_HTML
from exchange_diagnostics import get_binance_public_status
from binance_account import read_account_snapshot
from binance_preflight import snapshot_read_only
from live_readiness_policy import evaluate_readonly_binance_futures
from signal_levels import parse_luxalgo_reference_levels

MODE = "DRY_RUN"  # Code-level interlock: this build can NEVER transmit exchange orders.
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
DB_PATH = os.environ.get("DB_PATH", "/tmp/lux-direct-bot.sqlite3")
ALLOWED_SYMBOLS = {s.strip().upper() for s in os.environ.get("ALLOWED_SYMBOLS", "XRPUSDT").split(",") if s.strip()}
ALLOWED_TIMEFRAMES = {s.strip().upper() for s in os.environ.get("ALLOWED_TIMEFRAMES", "5").split(",") if s.strip()}
SYMBOL_MAP = json.loads(os.environ.get("SYMBOL_MAP_JSON", '{"XRPUSDT.P":"XRPUSDT"}'))
EVENTS = {
    "bullish confirmation signal": "BUY",
    "bullish confirmation": "BUY",
    "bullish +": "BUY",
    "bearish confirmation signal": "SELL",
    "bearish confirmation": "SELL",
    "bearish +": "SELL",
}
app = FastAPI(title="LUX DIRECT BOT", docs_url=None, redoc_url=None)

def database():
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    return db

@app.on_event("startup")
def initialize():
    # Unconfigured deployment is health-only. /webhook and /signals remain forbidden.
    if WEBHOOK_SECRET and ADMIN_TOKEN and WEBHOOK_SECRET == ADMIN_TOKEN:
        raise RuntimeError("Webhook and admin secrets must differ")
    with database() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS signals (
            id TEXT PRIMARY KEY, at INTEGER NOT NULL, symbol TEXT NOT NULL,
            side TEXT NOT NULL, timeframe TEXT NOT NULL, bartime TEXT NOT NULL,
            event TEXT NOT NULL, status TEXT NOT NULL,
            entry_reference TEXT, stop_reference TEXT, take_profit_reference TEXT,
            levels_status TEXT NOT NULL DEFAULT 'NOT_PROVIDED')""")
        # In-place additive migration: preserves prior webhook IDs and records.
        existing={row[1] for row in db.execute("PRAGMA table_info(signals)")}
        for name,definition in (
            ("entry_reference","TEXT"),("stop_reference","TEXT"),
            ("take_profit_reference","TEXT"),
            ("levels_status","TEXT NOT NULL DEFAULT 'NOT_PROVIDED'")):
            if name not in existing:
                db.execute("ALTER TABLE signals ADD COLUMN "+name+" "+definition)
        db.execute("""CREATE TABLE IF NOT EXISTS webhook_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at INTEGER NOT NULL, result TEXT NOT NULL, reason TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS operator_control (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            enabled INTEGER NOT NULL CHECK(enabled IN (0,1)))""")
        db.execute("INSERT OR IGNORE INTO operator_control(id,enabled) VALUES(1,0)")

def operator_enabled():
    with database() as db:
        row = db.execute("SELECT enabled FROM operator_control WHERE id=1").fetchone()
        return bool(row and row["enabled"])


def record_webhook_attempt(result, reason):
    """Log only a sanitized status, never payload or the webhook token."""
    known = {
        "ACCEPTED", "DUPLICATE", "REJECTED", "PROBE"
    }
    allowed_reasons = {
        "OK", "ALREADY_SEEN", "INVALID_JSON", "EXPECTED_JSON_OBJECT",
        "UNSUPPORTED_LUXALGO_SIGNAL", "SYMBOL_NOT_ALLOWED",
        "TIMEFRAME_NOT_ALLOWED", "BAD_BAR_TIME", "TEST_OK"
    }
    if result not in known or reason not in allowed_reasons:
        raise ValueError("Unexpected diagnostic result")
    with database() as db:
        db.execute(
            "INSERT INTO webhook_attempts(at,result,reason) VALUES(?,?,?)",
            (int(time.time()), result, reason),
        )
        db.execute("""DELETE FROM webhook_attempts WHERE id NOT IN (
            SELECT id FROM webhook_attempts ORDER BY id DESC LIMIT 40
        )""")


def reason_code(error):
    message = str(error)
    return {
        "Expected a JSON object": "EXPECTED_JSON_OBJECT",
        "Only LuxAlgo bullish/bearish confirmation signals accepted":
            "UNSUPPORTED_LUXALGO_SIGNAL",
        "Symbol is not allowlisted": "SYMBOL_NOT_ALLOWED",
        "Timeframe not allowlisted": "TIMEFRAME_NOT_ALLOWED",
        "LuxAlgo millisecond bar time required": "BAD_BAR_TIME",
    }.get(message, "INVALID_JSON")


def parse_signal(payload):
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object")
    event = str(payload.get("alert", "")).strip().lower()
    if event not in EVENTS:
        raise ValueError("Only LuxAlgo bullish/bearish confirmation signals accepted")
    ticker = str(payload.get("ticker", "")).strip().upper()
    symbol = SYMBOL_MAP.get(ticker, ticker)
    if not re.fullmatch(r"[A-Z0-9]{5,20}", symbol) or symbol not in ALLOWED_SYMBOLS:
        raise ValueError("Symbol is not allowlisted")
    tf = str(payload.get("tf", "")).strip()
    bartime = str(payload.get("bartime", "")).strip()
    if tf.upper() not in ALLOWED_TIMEFRAMES:
        raise ValueError("Timeframe not allowlisted")
    if not re.fullmatch(r"[0-9]{13}", bartime):
        raise ValueError("LuxAlgo millisecond bar time required")
    fingerprint = f"{ticker}|{event}|{tf}|{bartime}"
    signal_id = hashlib.sha256(fingerprint.encode()).hexdigest()[:28]
    entry,stop,target,level_status=parse_luxalgo_reference_levels(payload,EVENTS[event])
    return (signal_id, symbol, EVENTS[event], tf, bartime, event,
            entry,stop,target,level_status)

@app.get("/", response_class=HTMLResponse)
@app.get("/terminal", response_class=HTMLResponse)
def terminal():
    return HTMLResponse(
        content=LITE_HTML,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": (
                "default-src 'none'; script-src 'unsafe-inline'; "
                "style-src 'unsafe-inline'; connect-src 'self'; "
                "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
            ),
        },
    )

@app.get("/advanced", response_class=HTMLResponse)
def advanced():
    return HTMLResponse(
        content=DASHBOARD_HTML,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                 "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
                 "Content-Security-Policy": (
                     "default-src 'none'; script-src 'unsafe-inline'; "
                     "style-src 'unsafe-inline'; connect-src 'self'; "
                     "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
                 )}
    )


@app.get("/connect", response_class=HTMLResponse)
def connect_tradingview():
    """One-time setup page. HTML never includes any private webhook credentials."""
    return HTMLResponse(
        content=CONNECT_HTML,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": (
                "default-src 'none'; script-src 'unsafe-inline'; "
                "style-src 'unsafe-inline'; connect-src 'self'; "
                "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
            ),
        },
    )


@app.get("/operator/readiness")
def operator_readiness(request: Request, response: Response):
    """Minimal visible blockers; do not disclose API key values or webhook secrets."""
    admin_auth(request)
    response.headers["Cache-Control"] = "no-store"
    blockers = []
    if not os.environ.get("BINANCE_API_KEY") or not os.environ.get("BINANCE_API_SECRET"):
        blockers.append("BINANCE_KEYS_NOT_CONFIGURED")
    if DB_PATH.startswith("/tmp/") or DB_PATH == ":memory:":
        blockers.append("PERSISTENT_SIGNAL_LEDGER_REQUIRED")
    if not os.environ.get("FUTURES_ORDER_QTY"):
        blockers.append("ORDER_QUANTITY_NOT_CONFIGURED")
    if not os.environ.get("FUTURES_MAX_NOTIONAL_USDT"):
        blockers.append("NOTIONAL_LIMIT_NOT_CONFIGURED")
    if not os.environ.get("FUTURES_MAX_LOSS_USDT"):
        blockers.append("LOSS_LIMIT_NOT_CONFIGURED")
    if not os.environ.get("FUTURES_MAX_LEVERAGE"):
        blockers.append("LEVERAGE_LIMIT_NOT_CONFIGURED")
    blockers.extend((
        "REAL_TRADINGVIEW_ALERT_NOT_INDEPENDENTLY_VERIFIED",
        "EXCHANGE_ORDER_AND_PROTECTION_ACCEPTANCE_NOT_VERIFIED",
        "LIVE_ORDER_SENDER_HARD_LOCKED",
    ))
    return {"ready": False, "operator_on": operator_enabled(),
            "mode": MODE, "live_orders_enabled": False, "blockers": blockers}


@app.get("/operator/status")
def operator_status(request: Request, response: Response):
    admin_auth(request)
    response.headers["Cache-Control"] = "no-store"
    return {"enabled": operator_enabled(), "mode": MODE,
            "orders_enabled": False,
            "note": "BOT ON enables DRY_RUN intake only. Live orders are hard-disabled."}


@app.post("/operator/switch")
async def operator_switch(request: Request, response: Response):
    admin_auth(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        payload = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(422, "Expected JSON")
    if not isinstance(payload, dict) or type(payload.get("enabled")) is not bool:
        raise HTTPException(422, "enabled must be a boolean")
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE operator_control SET enabled=? WHERE id=1",
                   (int(payload["enabled"]),))
    return {"enabled": operator_enabled(), "mode": MODE, "orders_enabled": False}


@app.get("/health")
def health():
    return {"service": "LUX DIRECT BOT", "mode": MODE, "live_trading": False,
            "exchange": "Binance Futures (NOT CONNECTED)", "stage": "WEBHOOK_TEST",
            "allowed_symbols": sorted(ALLOWED_SYMBOLS),
            "allowed_timeframes": sorted(ALLOWED_TIMEFRAMES)}

@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    if not WEBHOOK_SECRET or not hmac.compare_digest(token, WEBHOOK_SECRET):
        raise HTTPException(status_code=403, detail="Forbidden")
    body = await request.body()
    if len(body) > 4096:
        raise HTTPException(status_code=413, detail="Payload exceeds 4 KB")
    try:
        payload = json.loads(body)
        # Explicit private transport probe: validates HTTPS/token routing without
        # saving a signal, invoking the operator switch, or placing any order.
        if payload == {"_probe": "CHECK_WEBHOOK_ROUTE_V1"}:
            record_webhook_attempt("PROBE", "TEST_OK")
            return {"result": "PROBE_OK", "mode": MODE,
                    "orders_enabled": False, "signal_created": False}
        signal = parse_signal(payload)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        record_webhook_attempt("REJECTED", reason_code(exc))
        raise HTTPException(status_code=422, detail=reason_code(exc)) from exc
    status = "DRY_RUN" if operator_enabled() else "PAUSED"
    with database() as db:
        cur = db.execute("""INSERT OR IGNORE INTO signals
            (id,at,symbol,side,timeframe,bartime,event,
             entry_reference,stop_reference,take_profit_reference,levels_status,status)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (signal[0], int(time.time()), *signal[1:], status))
    result = "ACCEPTED" if cur.rowcount else "DUPLICATE"
    record_webhook_attempt(result, "OK" if cur.rowcount else "ALREADY_SEEN")
    return {"result": result, "signal_id": signal[0],
            "side": signal[2], "mode": MODE}

def admin_auth(request: Request):
    token = request.headers.get("X-Admin-Token", "")
    if not ADMIN_TOKEN or not hmac.compare_digest(token, ADMIN_TOKEN):
        raise HTTPException(status_code=403, detail="Forbidden")


@app.get("/setup")
def setup(request: Request, response: Response):
    """Only an authenticated operator can retrieve the private webhook URL."""
    admin_auth(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return {
        "connection": "CONFIGURED" if WEBHOOK_SECRET else "NOT_CONFIGURED",
        "webhook_url": (
            "https://lux-direct-bot-dryrun.onrender.com/webhook/" + WEBHOOK_SECRET
            if WEBHOOK_SECRET else None
        ),
        "luxalgo_alert_message": (
            '{\n'
            '  "alert": "{default}",\n'
            '  "ticker": "{ticker}",\n'
            '  "tf": "{tf}",\n'
            '  "bartime": {time},\n'
            '  "entry": "{close}",\n'
            '  "sl": "{sl1}",\n'
            '  "tp1": "{tp1}"\n'
            '}'
        ),
        "indicator": "LuxAlgo Signals & Overlays",
        "selected_chart": "BINANCE:XRPUSDT.P",
        "alert_condition": "Any alert() function call",
        "enable_conditions": ["Bullish Confirmation", "Bearish Confirmation"],
        "allowed_symbols": sorted(ALLOWED_SYMBOLS),
        "allowed_timeframes": sorted(ALLOWED_TIMEFRAMES),
        "mode": MODE,
        "real_money_enabled": False,
        "warning": "Webhook URL is private; never post or share it publicly.",
    }


@app.get("/binance/diagnostics")
def binance_diagnostics(request: Request):
    """Only public USDT-M Binance metadata; no orders or account access."""
    admin_auth(request)
    result = get_binance_public_status()
    return result


@app.get("/binance/account")
def binance_account(request: Request, response: Response):
    """Authenticated read-only Binance USD-M Futures account snapshot."""
    admin_auth(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return read_account_snapshot()


@app.get("/binance/preflight")
def binance_preflight(request: Request, response: Response):
    """Private, authenticated signed GET inventory only; cannot authorize trading."""
    admin_auth(request)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return snapshot_read_only()


@app.get("/operator/live-readiness")
def operator_live_readiness(request: Request, response: Response):
    """On-demand signed GET preflight and conservative blockers. NO ORDERS."""
    admin_auth(request)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    relevant=("FUTURES_ORDER_QTY","FUTURES_MAX_NOTIONAL_USDT",
              "FUTURES_MAX_LOSS_USDT","FUTURES_MAX_LEVERAGE")
    return evaluate_readonly_binance_futures(
        snapshot_read_only(), db_path=DB_PATH,
        settings={name:os.environ.get(name) for name in relevant})


@app.post("/admin/selftest")
def dry_run_signal_test(request: Request, response: Response):
    """Parse, write and deduplicate realistic LuxAlgo signals, then ROLLBACK.

    Never enters the real signal ledger or sends exchange orders.
    """
    admin_auth(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    if not ALLOWED_SYMBOLS or not ALLOWED_TIMEFRAMES:
        raise HTTPException(503, "No allowed symbol/timeframe")
    symbol = sorted(ALLOWED_SYMBOLS)[0]
    timeframe = sorted(ALLOWED_TIMEFRAMES)[0]
    millis = str(int(time.time() * 1000))
    cases = [
        {"alert": "Bullish Confirmation Signal", "ticker": symbol,
         "tf": timeframe, "bartime": int(millis)},
        {"alert": "Bearish Confirmation Signal", "ticker": symbol,
         "tf": timeframe, "bartime": int(millis)},
    ]
    parsed = [parse_signal(item) for item in cases]
    results = []
    with database() as db:
        before = db.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        db.execute("SAVEPOINT dry_run_selftest")
        try:
            for signal in parsed:
                sql = """INSERT OR IGNORE INTO signals
                    (id,at,symbol,side,timeframe,bartime,event,
             entry_reference,stop_reference,take_profit_reference,levels_status,status)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"""
                args = (signal[0], int(time.time()), *signal[1:], "TEST_ONLY")
                first = db.execute(sql, args).rowcount
                duplicate = db.execute(sql, args).rowcount
                results.append({"side": signal[2], "write": first,
                                "duplicate_write": duplicate})
            if not all(x["write"] == 1 and x["duplicate_write"] == 0 for x in results):
                raise RuntimeError("Self-test insert/dedup failed")
        finally:
            db.execute("ROLLBACK TO SAVEPOINT dry_run_selftest")
            db.execute("RELEASE SAVEPOINT dry_run_selftest")
        after = db.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    if before != after:
        raise HTTPException(503, "Self-test record isolation failed")
    return {"result": "PASS", "checks": results, "test_records_persisted": 0,
            "actual_luxalgo_alert_verified": False,
            "exchange_orders_sent": 0, "mode": MODE}


@app.get("/connection/status")
def connection_status(request: Request, response: Response):
    """Authenticated evidence of accepted webhook records, not a claim of origin."""
    admin_auth(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    with database() as db:
        count = db.execute("SELECT COUNT(*) FROM signals WHERE status IN ('DRY_RUN','PAUSED')").fetchone()[0]
        latest = db.execute(
            """SELECT at,symbol,side,timeframe,bartime,status,
                      entry_reference,stop_reference,take_profit_reference,levels_status FROM signals
               WHERE status IN ('DRY_RUN','PAUSED') ORDER BY at DESC, rowid DESC LIMIT 1"""
        ).fetchone()
    return {
        "state": "WEBHOOK_RECORDS_PRESENT" if count else "NO_WEBHOOK_RECORDS",
        "records": count,
        "latest": dict(latest) if latest else None,
        "origin_independently_verified": False,
        "tradingview_alert_confirmed": False,
        "mode": MODE,
        "orders_enabled": False,
        "explanation": "Accepted webhook records do not establish that TradingView sent them.",
    }


@app.get("/connection/attempts")
def webhook_attempts(request: Request, response: Response):
    """Recent authenticated receiver outcomes; excludes token and payload."""
    admin_auth(request)
    response.headers["Cache-Control"] = "no-store"
    with database() as db:
        rows = db.execute(
            "SELECT at,result,reason FROM webhook_attempts ORDER BY id DESC LIMIT 20"
        ).fetchall()
    return {"attempts": [dict(r) for r in rows], "mode": MODE,
            "tradingview_sender_verified": False, "orders_enabled": False}


@app.get("/signals")
def signals(request: Request):
    admin_auth(request)
    with database() as db:
        records = db.execute("""SELECT id,at,symbol,side,timeframe,bartime,event,status,
                entry_reference,stop_reference,take_profit_reference,levels_status
            FROM signals ORDER BY at DESC, rowid DESC LIMIT 30""").fetchall()
    return [dict(r) for r in records]
