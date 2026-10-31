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
from exchange_diagnostics import get_binance_public_status
from binance_account import read_account_snapshot

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
            event TEXT NOT NULL, status TEXT NOT NULL)""")

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
    return (signal_id, symbol, EVENTS[event], tf, bartime, event)

@app.get("/", response_class=HTMLResponse)
@app.get("/terminal", response_class=HTMLResponse)
def terminal():
    return HTMLResponse(
        content=DASHBOARD_HTML,
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

@app.get("/health")
def health():
    return {"service": "LUX DIRECT BOT", "mode": MODE, "live_trading": False,
            "exchange": "Binance Futures (NOT CONNECTED)", "stage": "WEBHOOK_TEST"}

@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    if not WEBHOOK_SECRET or not hmac.compare_digest(token, WEBHOOK_SECRET):
        raise HTTPException(status_code=403, detail="Forbidden")
    body = await request.body()
    if len(body) > 4096:
        raise HTTPException(status_code=413, detail="Payload exceeds 4 KB")
    try:
        signal = parse_signal(json.loads(body))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with database() as db:
        cur = db.execute("""INSERT OR IGNORE INTO signals
            (id,at,symbol,side,timeframe,bartime,event,status)
            VALUES(?,?,?,?,?,?,?,?)""",
            (signal[0], int(time.time()), *signal[1:], "DRY_RUN"))
    return {"result": "ACCEPTED" if cur.rowcount else "DUPLICATE",
            "signal_id": signal[0], "side": signal[2], "mode": MODE}

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
            '  "bartime": {time}\n'
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
                    (id,at,symbol,side,timeframe,bartime,event,status)
                    VALUES(?,?,?,?,?,?,?,?)"""
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
        count = db.execute("SELECT COUNT(*) FROM signals WHERE status = 'DRY_RUN'").fetchone()[0]
        latest = db.execute(
            """SELECT at,symbol,side,timeframe,bartime,status FROM signals
               WHERE status='DRY_RUN' ORDER BY at DESC, rowid DESC LIMIT 1"""
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


@app.get("/signals")
def signals(request: Request):
    admin_auth(request)
    with database() as db:
        records = db.execute("""SELECT id,at,symbol,side,timeframe,bartime,event,status
            FROM signals ORDER BY at DESC, rowid DESC LIMIT 30""").fetchall()
    return [dict(r) for r in records]
