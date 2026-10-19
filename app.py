"""LuxAlgo PRO: authenticated signal intake + READ-ONLY Binance API connectivity.

NO trading endpoints, orders, funds transfers, or withdrawal functions exist.
Binance credentials must be supplied via Railway service variables only.
"""
import hmac
import hashlib
import json
import os
import re
import time
from collections import deque
from datetime import datetime, timezone
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)
_records = deque(maxlen=50)
_lock = Lock()

PAGE = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>LUXALGO PRO · LIVE TRADING TERMINAL</title><style>body{margin:0;padding:24px;background:#0b1424;color:#e9f2ff;font:16px system-ui,Arial}header,.tile{background:#13253c;border:1px solid #2c4868;border-radius:14px;padding:22px}h1{font-size:28px;margin:4px 0 8px}p{color:#acd0f3}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin-top:20px}.label{font-size:12px;color:#9db8d3}.value{font-size:21px;margin-top:12px}.off{color:#ffbf62}.ok{color:#4ce0ae}.note{margin-top:20px;line-height:1.6}</style></head><body><header><h1>LUXALGO PRO · LIVE TRADING TERMINAL</h1><p>Railway signal intake · Live Binance trading disabled</p></header><main><section class="tile"><div class="label">TRADING STATUS</div><div class="value off">LIVE TRADING OFF</div></section><section class="tile"><div class="label">BINANCE API</div><div class="value">Read-only check available · not publicly exposed</div></section><section class="tile"><div class="label">TRADINGVIEW WEBHOOK</div><div class="value {{ 'ok' if configured else 'off' }}">{{ 'Receiver configured' if configured else 'Awaiting private token' }}</div></section><section class="tile"><div class="label">SIGNALS RECEIVED SINCE RESTART</div><div class="value">{{ count }}</div></section></main><p class="note">This app NEVER submits orders. To check Binance API authentication use the private /binance/check endpoint with your webhook token; do not expose your credentials.</p></body></html>'''


def expected_webhook_token():
    return os.getenv('LUXALGO_WEBHOOK_TOKEN', '')


def authorized(data):
    expected = expected_webhook_token()
    if len(expected) < 24:
        return False
    supplied = data.get('token', '')
    return isinstance(supplied, str) and hmac.compare_digest(supplied, expected)


@app.get('/')
def home():
    with _lock:
        count = len(_records)
    return render_template_string(PAGE, configured=len(expected_webhook_token()) >= 24, count=count)


@app.get('/health')
def health():
    return jsonify(status='ok', live_trading=False, orders_enabled=False, kind='signal_receiver_with_readonly_binance_check')


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
    with _lock:
        _records.append({'time': datetime.now(timezone.utc).isoformat(), 'symbol': symbol, 'side': allowed[side], 'tf': '15m'})
    return jsonify(status='signal_received', mode='SIGNAL_ONLY', symbol=symbol, side=allowed[side], orders_placed=0), 200


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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '8080')))
