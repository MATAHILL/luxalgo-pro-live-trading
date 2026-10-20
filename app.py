"""LuxAlgo PRO: authenticated signal intake + READ-ONLY Gate.io and Binance API connectivity.

NO order submission, funds transfer, or withdrawal functions exist.
Authenticated /bot/status reports PostgreSQL-backed DRY RUN intent records only.
The /gate/plan endpoint only calculates indicative ATR levels; no orders are placed.
Exchange credentials must be supplied via Railway service variables only.
"""
import hmac
import hashlib
import json
import os
import re
import time
import math
from collections import deque
from hashlib import sha256
from datetime import datetime, timezone
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from flask import Flask, jsonify, render_template_string, request, Response

# PostgreSQL dependency: psycopg[binary] (see requirements addition).
import psycopg
from psycopg.rows import dict_row

app = Flask(__name__)
_lock = Lock()

# Durable signal-only audit store. All trade execution remains disabled.
# Do not fall back to RAM on DB failures: that would lose duplicate protection.
DB_SCHEMA = '''
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

def _ensure_schema(connection):
    # Repeated CREATE IF NOT EXISTS is safe across Railway restarts.
    connection.execute(DB_SCHEMA)

def _db_recent():
    with _db_connect() as con:
        _ensure_schema(con)
        with con.cursor() as cur:
            cur.execute('SELECT COUNT(*) AS total FROM luxalgo_signal_receipts')
            count = cur.fetchone()['total']
            cur.execute('''SELECT received_at, symbol, side FROM luxalgo_signal_receipts
                           ORDER BY id DESC LIMIT 20''')
            rows = [dict(time=r['received_at'].isoformat(),symbol=r['symbol'],side=r['side'])
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
                        (record['dt'],event_id,record['symbol'],record['side'],'15m'))
            cur.execute('''INSERT INTO luxalgo_dry_run_intents
                           (event_id,received_at,symbol,side,timeframe,status)
                           VALUES (%s,%s,%s,%s,%s,'DRY_RUN_ONLY')
                           ON CONFLICT (event_id) DO NOTHING RETURNING event_id''',
                        (event_id,record['dt'],record['symbol'],record['side'],'15m'))
            duplicate = cur.fetchone() is None
    return duplicate

PAGE = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>LUXALGO PRO · Trading Terminal</title>
<style>:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#08111f;color:#edf5ff;font:14px/1.5 system-ui,Segoe UI,Arial}.wrap{max-width:1180px;margin:auto;padding:24px}header,.panel,.metric{background:#122238;border:1px solid #29415e;border-radius:12px;padding:19px}header{display:flex;justify-content:space-between;gap:12px;align-items:center}h1{font-size:23px;margin:0}h2{font-size:15px;margin:0 0 15px}p{margin:5px 0;color:#a8bed6}.tag{color:#ffc47c;font-weight:700}.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:15px 0}.metric small{color:#9fb5cf}.metric strong{display:block;margin-top:8px;font-size:18px}.panel{margin-top:14px}.tablewrap{overflow:auto}table{border-collapse:collapse;width:100%;min-width:650px}th,td{padding:11px 9px;border-bottom:1px solid #2b435e;text-align:left}th{color:#a7c0dc;font-size:12px}td{font-size:13px}.light{color:#a8bed6}.ok{color:#47dfae}.off{color:#ffc47c}.btn{background:#203b5c;color:#eef6ff;border:1px solid #46749d;border-radius:7px;padding:8px 12px;cursor:pointer}.btn:disabled{opacity:.5}#planResults{white-space:pre-wrap;font:13px/1.7 ui-monospace,Consolas,monospace;background:#0b1a2b;border:1px solid #2b435e;border-radius:8px;padding:14px}.controls{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin:14px 0}.controls label{display:flex;flex-direction:column;color:#a8bed6;font-size:12px;gap:5px}.controls input,.controls select{background:#0b1a2b;color:#edf5ff;border:1px solid #46749d;border-radius:7px;padding:9px;font:inherit}.controls input:read-only{opacity:.7}.foot{padding:18px 2px;font-size:12px;color:#a8bed6}@media(max-width:800px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}header{display:block}.wrap{padding:12px}}</style></head><body><div class="wrap"><header><div><h1>LUXALGO PRO · TRADING TERMINAL</h1><p>Gate.io USDT Perpetual · LuxAlgo 15m · Hello, Subhasish</p></div><div class="tag">● LIVE TRADING OFF</div></header>
<div class="grid"><div class="metric"><small>WEBHOOK</small><strong class="{{ 'ok' if configured else 'off' }}">{{ 'Configured' if configured else 'Not configured' }}</strong></div><div class="metric"><small>SIGNALS STORED IN DATABASE</small><strong>{{ count }}</strong></div><div class="metric"><small>GATE.IO API</small><strong>Read-only</strong></div><div class="metric"><small>REALIZED PROFIT / LOSS</small><strong>Not available</strong></div></div>
<section class="panel"><h2>RECENT LUXALGO SIGNALS</h2><div class="tablewrap"><table><thead><tr><th>Received (UTC)</th><th>Token</th><th>Signal</th><th>Timeframe</th><th>Status</th><th>Entry / SL / TP Plan</th></tr></thead><tbody>{% for r in records %}<tr><td>{{r.time[:19]|e}}</td><td>{{r.symbol|e}}</td><td>{{r.side|e}}</td><td>15m</td><td>Received · Not executed</td><td><button class="btn plan" data-symbol="{{r.symbol|e}}" data-side="{{r.side|e}}">Calculate</button></td></tr>{% else %}<tr><td colspan="6">No signals stored in the database yet.</td></tr>{% endfor %}</tbody></table></div><p class="light">Signals are received events, not fills. Signal receipts and dry-run intent IDs are saved in PostgreSQL. Duplicate signals may not create separate dry-run intents.</p></section>
<section class="panel"><h2>AUTO TRADING SETTINGS — PLANNING ONLY</h2>
<p class="light">AUTO is the default. You may preview changes graphically, but these settings are not saved and cannot place or modify orders.</p>
<div class="controls"><label>Mode<select id="mode"><option value="AUTO">AUTO (ATR-based)</option><option value="MANUAL">MANUAL (preview overrides)</option></select></label>
<label>Leverage <input id="leverage" type="number" value="5" min="1" max="20" step="1"></label>
<label>Position notional (USDT) <input id="notional" type="number" value="100" min="1" step="1"></label>
<label>Margin<select id="margin"><option value="CROSS">Cross</option><option value="ISOLATED">Isolated</option></select></label>
<label>Entry reference <input id="entry" type="number" step="any" placeholder="Select a signal" readonly></label>
<label>Stop Loss <input id="sl" type="number" step="any" placeholder="1.5 × ATR" readonly></label>
<label>TP1 — 100% exit <input id="tp1" type="number" step="any" placeholder="2 × ATR" readonly></label></div>
<div id="planResults">Select Calculate beside a signal to obtain a read-only 15m ATR-based plan. Reference entry is not a fill; SL and TP1 are not placed orders.</div>
</section>
<section class="panel"><h2>POSITION & PROFIT SUMMARY</h2><div class="tablewrap"><table><thead><tr><th>Pair</th><th>Entry fill</th><th>SL order</th><th>TP1 order</th><th>Realized P&amp;L</th><th>State</th></tr></thead><tbody><tr><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td><td>No execution engine; no verified fills</td></tr></tbody></table></div><p class="light">Profit and win rate will appear only after a secure Gate.io fills/order synchronization and persistent record system are implemented. Nothing is simulated as realized profit.</p></section>
<div class="foot">Read-only terminal · No orders are submitted · Never share API keys · Safeguards and exchange-side SL/TP are not yet implemented.</div></div>
<script>
const mode=document.getElementById('mode'), entry=document.getElementById('entry'), sl=document.getElementById('sl'), tp1=document.getElementById('tp1');
mode.addEventListener('change',()=>{const manual=mode.value==='MANUAL';for(const el of [entry,sl,tp1])el.readOnly=!manual;});
document.querySelectorAll('.plan').forEach(b=>b.addEventListener('click',async()=>{
 const out=document.querySelector('#planResults');b.disabled=true;out.textContent='Calculating read-only ATR plan…';
 try{const r=await fetch('/terminal/plan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:b.dataset.symbol,side:b.dataset.side})});const d=await r.json();if(!r.ok)throw Error(d.error||'Plan unavailable');const p=d.plan;
 entry.value=p.reference_entry;sl.value=p.stop_loss_1_5_atr;tp1.value=p.take_profit_1_2_atr;
 out.textContent=['Contract: '+p.contract,'Signal: '+p.side,'Reference: latest completed 15m close (not a fill)','AUTO Stop Loss: 1.5 × ATR','AUTO TP1: 2 × ATR (100% close intended)','Order count: 0','Manual changes are previews only. No exchange orders or protection exist.'].join('\n');
 }catch(e){out.textContent=e.message}finally{b.disabled=false}
}));
</script></body></html>'''

def expected_webhook_token():
    return os.getenv('LUXALGO_WEBHOOK_TOKEN', '')


def authorized(data):
    expected = expected_webhook_token()
    if len(expected) < 24:
        return False
    supplied = data.get('token', '')
    return isinstance(supplied, str) and hmac.compare_digest(supplied, expected)


def _terminal_permitted():
    # A separate dashboard password; never reuse webhook token or exchange credentials.
    expected = os.getenv('TERMINAL_PASSWORD', '')
    user = os.getenv('TERMINAL_USER', 'admin')
    auth = request.authorization
    return (len(expected) >= 24 and auth is not None
            and hmac.compare_digest(auth.username or '', user)
            and hmac.compare_digest(auth.password or '', expected))


def _terminal_guard():
    if not os.getenv('TERMINAL_PASSWORD'):
        return Response('Private terminal unavailable. Configure TERMINAL_PASSWORD in Railway variables.', status=503)
    return Response('Authentication required', status=401,
                    headers={'WWW-Authenticate': 'Basic realm="LuxAlgo Private Terminal"',
                             'Cache-Control': 'no-store'})


@app.get('/')
def home():
    if not _terminal_permitted():
        return _terminal_guard()
    try:
        count, records = _db_recent()
    except (psycopg.Error, RuntimeError, OSError):
        app.logger.exception('Persistent signal storage unavailable')
        return Response('Persistent signal database temporarily unavailable. No trading is active.', status=503)
    response = Response(render_template_string(PAGE, configured=len(expected_webhook_token()) >= 24,
                                               count=count, records=records), mimetype='text/html')
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-Content-Type-Options'] = 'nosniff'
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
    if not re.fullmatch(r'[A-Z0-9]{3,25}USDT',symbol) or side not in ('LONG','SHORT'):
        return jsonify(error='Unsupported pair or direction'),422
    try:
        plan = _gate_atr_plan(symbol,side)
        response = jsonify(plan=plan, orders_enabled=False, live_trading=False)
        response.headers['Cache-Control']='no-store'
        return response
    except (HTTPError,URLError,TimeoutError,ValueError,KeyError,TypeError,OSError):
        return jsonify(error='ATR plan temporarily unavailable'),502


@app.get('/health')
def health():
    return jsonify(status='ok', live_trading=False, orders_enabled=False, kind='signal_receiver_with_readonly_exchange_checks')


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
                   signals_since_restart=signal_count,
                   dry_run_intents=recent,
                   persistent_history=True,
                   warning='No positions, order submissions, fills, SL or TP orders exist.')


@app.post('/webhook')
def webhook():
    if len(expected_webhook_token()) < 24:
        return jsonify(error='Webhook token not configured'), 503
    if not request.is_json:
        return jsonify(error='JSON required'), 400
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error='JSON object required'), 400
    if not authorized(data):
        return jsonify(error='Unauthorized'), 401
    symbol = str(data.get('symbol', data.get('ticker', ''))).upper().removeprefix('BINANCE:').removesuffix('.P')
    side = str(data.get('side', data.get('alert', ''))).strip().upper()
    tf = str(data.get('tf', '')).strip().lower()
    allowed = {'BUY': 'LONG', 'LONG': 'LONG', 'BULLISH CONFIRMATION': 'LONG', 'SELL': 'SHORT', 'SHORT': 'SHORT', 'BEARISH CONFIRMATION': 'SHORT'}
    if not re.fullmatch(r'[A-Z0-9]{3,25}USDT', symbol) or side not in allowed or tf not in ('15', '15m', '15min', '15 minutes'):
        return jsonify(error='Invalid symbol, side, or 15-minute timeframe'), 422
    received_at = datetime.now(timezone.utc)
    record = {'dt':received_at,'symbol':symbol,'side':allowed[side]}
    # This event key is based on RECEIVE time, not a verified candle ID.
    # For live orders, require a TradingView event/bar identifier and
    # exchange reconciliation before accepting any automated trades.
    bucket = int(received_at.timestamp()) // 900
    event_id = sha256(f'{symbol}|{allowed[side]}|15m|{bucket}'.encode()).hexdigest()[:20]
    try:
        duplicate = _db_record(record,event_id)
    except (psycopg.Error,RuntimeError,OSError):
        app.logger.exception('Cannot durably record incoming LuxAlgo signal')
        return jsonify(error='Signal not stored; retry later',orders_placed=0),503
    return jsonify(status='signal_received', mode='SIGNAL_ONLY', symbol=symbol,
                   side=allowed[side], duplicate_in_candle=duplicate,
                   dry_run_intent_id=event_id, orders_placed=0), 200


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



def _gate_atr_plan(symbol, direction):
    """Read ONLY public, completed 15m candles. Return an illustrative plan.

    Not order sizing: 0.5% risk requires verified available funds, instrument
    contract multiplier, minimum contract count, open positions, and margin rules.
    """
    contract = symbol[:-4] + '_USDT'
    url = ('https://api.gateio.ws/api/v4/futures/usdt/candlesticks?' +
           urlencode({'contract': contract, 'interval': '15m', 'limit': 60}))
    req = Request(url, headers={'Accept': 'application/json', 'User-Agent': 'LuxAlgo-PRO-Readonly/1.0'}, method='GET')
    with urlopen(req, timeout=10) as res:
        candles = json.loads(res.read(131072))
    if not isinstance(candles, list):
        raise ValueError('Invalid Gate candle response')
    now = int(time.time())
    complete = []
    for c in candles:
        start = int(c['t'])
        if start + 900 > now:  # Exclude unfinished interval
            continue
        high, low, close = float(c['h']), float(c['l']), float(c['c'])
        if not all(math.isfinite(v) and v > 0 for v in (high,low,close)) or high < low:
            raise ValueError('Invalid OHLC data')
        complete.append((start, high, low, close))
    complete.sort(key=lambda x:x[0])
    complete = list({c[0]: c for c in complete}.values())
    complete.sort(key=lambda x:x[0])
    if len(complete) < 16 or any(b[0]-a[0] != 900 for a,b in zip(complete[-16:-1],complete[-15:])):
        raise ValueError('Insufficient consecutive completed 15m candles')
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
        'contract':contract, 'side':direction, 'timeframe':'15m',
        'reference':'most_recent_completed_15m_close_NOT_order_fill',
        'last_candle_start_utc':datetime.fromtimestamp(complete[-1][0], timezone.utc).isoformat(),
        'reference_entry':entry, 'atr_14_wilder':atr,
        'stop_loss_1_5_atr':sl, 'take_profit_1_2_atr':tp1,
        'planned_leverage':5,
        'intended_risk_fraction':0.005,
        'margin_mode':'CROSS', 'position_mode':'HEDGE',
        'position_size':'NOT_CALCULATED', 'exchange_orders_sent':0,
        'warning':'Planning only; prices are not executable quotes. No SL/TP orders exist.'
    }


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
    req = Request('https://api.gateio.ws' + path,
                  headers={'Accept':'application/json','User-Agent':'LuxAlgo-PRO-PublicScanner/1.0'},method='GET')
    with urlopen(req, timeout=8) as res:
        return json.loads(res.read(2000000))


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
        except (HTTPError,URLError,TimeoutError,ValueError,KeyError,TypeError,OSError):
            return jsonify(error='Gate.io public market scan temporarily unavailable',orders_enabled=False),502


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '8080')))
