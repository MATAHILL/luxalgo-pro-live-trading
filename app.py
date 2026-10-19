"""LuxAlgo PRO Railway webhook intake: signal-only, never sends exchange orders."""
import hmac
import os
import re
from collections import deque
from datetime import datetime, timezone
from threading import Lock
from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)
_records = deque(maxlen=50)
_lock = Lock()
PAGE = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>LUXALGO PRO · LIVE TRADING TERMINAL</title><style>body{margin:0;padding:24px;background:#0b1424;color:#e9f2ff;font:16px system-ui,Arial}header,.tile{background:#13253c;border:1px solid #2c4868;border-radius:14px;padding:22px}h1{font-size:28px;margin:4px 0 8px}p{color:#acd0f3}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin-top:20px}.label{font-size:12px;color:#9db8d3}.value{font-size:21px;margin-top:12px}.off{color:#ffbf62}.ok{color:#4ce0ae}.note{margin-top:20px;line-height:1.6}</style></head><body><header><h1>LUXALGO PRO · LIVE TRADING TERMINAL</h1><p>Railway signal intake · Live Binance trading disabled</p></header><main><section class="tile"><div class="label">TRADING STATUS</div><div class="value off">LIVE TRADING OFF</div></section><section class="tile"><div class="label">BINANCE CONNECTION</div><div class="value">Not connected</div></section><section class="tile"><div class="label">TRADINGVIEW WEBHOOK</div><div class="value {{ 'ok' if configured else 'off' }}">{{ 'Receiver configured' if configured else 'Awaiting private token' }}</div></section><section class="tile"><div class="label">SIGNALS RECEIVED SINCE RESTART</div><div class="value">{{ count }}</div></section></main><p class="note">This is a signal-only receiver. It NEVER submits orders. Signal history is in memory and resets when the service restarts. No account controls are exposed on this public page.</p></body></html>'''

@app.get('/')
def home():
    with _lock:
        count = len(_records)
    return render_template_string(PAGE, configured=bool(os.getenv('LUXALGO_WEBHOOK_TOKEN')), count=count)

@app.get('/health')
def health():
    return jsonify(status='ok',live_trading=False,orders_enabled=False,kind='signal_receiver')

@app.post('/webhook')
def webhook():
    expected = os.getenv('LUXALGO_WEBHOOK_TOKEN','')
    if len(expected) < 24:
        return jsonify(error='Webhook token not configured'),503
    if not request.is_json:
        return jsonify(error='JSON required'),400
    data = request.get_json(silent=True)
    if not isinstance(data,dict):
        return jsonify(error='JSON object required'),400
    supplied = data.get('token','')
    if not isinstance(supplied,str) or not hmac.compare_digest(supplied,expected):
        return jsonify(error='Unauthorized'),401
    symbol = str(data.get('symbol',data.get('ticker',''))).upper().removeprefix('BINANCE:').removesuffix('.P')
    side = str(data.get('side',data.get('alert',''))).strip().upper()
    tf = str(data.get('tf','')).strip().lower()
    allowed = {'BUY':'LONG','LONG':'LONG','BULLISH CONFIRMATION':'LONG','SELL':'SHORT','SHORT':'SHORT','BEARISH CONFIRMATION':'SHORT'}
    if not re.fullmatch(r'[A-Z0-9]{3,25}USDT',symbol) or side not in allowed or tf not in ('15','15m','15min','15 minutes'):
        return jsonify(error='Invalid symbol, side, or 15-minute timeframe'),422
    with _lock:
        _records.append({'time':datetime.now(timezone.utc).isoformat(),'symbol':symbol,'side':allowed[side],'tf':'15m'})
    return jsonify(status='signal_received',mode='SIGNAL_ONLY',symbol=symbol,side=allowed[side],orders_placed=0),200

if __name__ == '__main__':
    app.run(host='0.0.0.0',port=int(os.getenv('PORT','8080')))
