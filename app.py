"""LuxAlgo PRO: authenticated signal intake + READ-ONLY Gate.io and Binance API connectivity.

NO order submission, funds transfer, or withdrawal functions exist.
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
from datetime import datetime, timezone
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)
_records = deque(maxlen=50)
_lock = Lock()

PAGE = '''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>LUXALGO PRO · TRADING TERMINAL</title>
<style>
:root{color-scheme:dark;--bg:#080f1c;--surface:#121f32;--surface2:#172942;--border:#273b55;--text:#eaf3ff;--dim:#9fb3cb;--muted:#718aa6;--teal:#39d6b1;--red:#ff7188;--gold:#ffc576}
*{box-sizing:border-box}body{margin:0;background:var(--bg);font:14px/1.5 system-ui,-apple-system,Segoe UI,Arial,sans-serif;color:var(--text)}
.shell{max-width:1580px;padding:24px;margin:auto}h1,h2,h3,p{margin:0}h1{font-size:27px;letter-spacing:.7px}h2{font-size:15px;letter-spacing:.55px}h3{font-size:13px}small,.small{font-size:12px;color:var(--dim)}
.top{padding:23px 25px;background:linear-gradient(115deg,#182c49,#101f33);border:1px solid var(--border);border-radius:16px}.topline,.subhead,.row{display:flex;align-items:center;justify-content:space-between;gap:15px;flex-wrap:wrap}.tag{border:1px solid #885d36;color:var(--gold);background:#47321c55;border-radius:999px;padding:5px 12px;font-size:12px;font-weight:700;white-space:nowrap}.subhead{margin-top:12px;color:var(--dim)}.grid{display:grid;gap:14px}.stats{grid-template-columns:repeat(5,minmax(0,1fr));margin-top:15px}.panel,.stat{border:1px solid var(--border);background:var(--surface);border-radius:13px;min-width:0}.stat{padding:18px}.stat .label{color:var(--dim);font-size:11px;letter-spacing:.45px}.stat .value{font-size:18px;margin-top:12px;font-weight:650}.ok{color:var(--teal)}.off{color:var(--gold)}.muted{color:var(--dim)}.content{grid-template-columns:minmax(0,1.75fr) minmax(310px,1fr);margin-top:15px}.panel{padding:19px}.paneltitle{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:17px;flex-wrap:wrap}.pills{display:flex;gap:7px;flex-wrap:wrap}.pill{border:1px solid var(--border);background:var(--surface2);border-radius:6px;padding:6px 9px;font-size:12px;color:var(--dim)}.btn,select{background:#203855;border:1px solid #365476;color:#e8f4ff;border-radius:7px;padding:7px 11px;cursor:pointer;font:inherit}.btn:hover{background:#294465}.btn:focus-visible,select:focus-visible{outline:2px solid var(--teal)}.btn:disabled{opacity:.5;cursor:not-allowed}.chartbox{height:308px;position:relative;background:linear-gradient(#101c2d,#111e32);border-radius:9px;border:1px solid #213751;overflow:hidden}.chartbox canvas{display:block;width:100%;height:100%}.empty{padding:25px 15px;min-height:95px;display:flex;align-items:center;justify-content:center;text-align:center;color:var(--dim);background:#101c2c;border:1px dashed #38506c;border-radius:8px}.summary{display:grid;grid-template-columns:1fr 1fr;gap:10px}.mini{background:#0d1929;border:1px solid var(--border);border-radius:9px;padding:13px}.mini span{font-size:11px;color:var(--dim)}.mini strong{display:block;font-size:17px;margin-top:5px}.stack{display:grid;gap:13px}.below{margin-top:14px;grid-template-columns:minmax(0,1.45fr) minmax(330px,1fr)}.tablewrap{overflow:auto}table{border-collapse:collapse;width:100%;min-width:610px;text-align:left}th,td{padding:11px 10px;border-bottom:1px solid #2a4057;font-size:12px}th{color:#9bb4cf;font-weight:600;white-space:nowrap}td{color:#d4e4f5}.note{margin-top:12px;padding:11px 13px;background:#11253a;border-left:3px solid #6c9ac9;color:#b9cee1;border-radius:5px;font-size:12px}.sectionfoot{margin-top:7px;color:var(--dim);font-size:12px}.statusdot{display:inline-block;height:8px;width:8px;background:var(--teal);border-radius:50%;margin-right:6px}.foot{font-size:12px;color:var(--dim);margin:18px 2px 8px}.placeholder{color:var(--muted)}
@media(max-width:1100px){.stats{grid-template-columns:repeat(3,minmax(0,1fr))}.content,.below{grid-template-columns:1fr}}@media(max-width:620px){.shell{padding:12px}.stats{grid-template-columns:repeat(2,minmax(0,1fr))}.top{padding:17px}.stat{padding:13px}.stat .value{font-size:15px}h1{font-size:21px}.panel{padding:14px}.chartbox{height:240px}}
</style></head><body><div class="shell">
<header class="top"><div class="topline"><h1>LUXALGO PRO · TRADING TERMINAL</h1><span class="tag">● LIVE TRADING OFF</span></div><div class="subhead"><span>Gate.io Futures · LuxAlgo signals · Cloud trading monitors · Coded by SUBHASISH</span><span>Read-only dashboard · No exchange orders placed</span></div></header>
<section class="grid stats" aria-label="Trading overview">
<div class="stat"><div class="label">GATE.IO FUTURES API</div><div class="value ok"><span class="statusdot"></span>Configured</div><div class="small">Previous private check succeeded; not continuously monitored</div></div>
<div class="stat"><div class="label">TRADINGVIEW WEBHOOK</div><div class="value {{ 'ok' if configured else 'off' }}">{{ 'Configured' if configured else 'Not configured' }}</div><div class="small">Receiver status</div></div>
<div class="stat"><div class="label">SIGNALS SINCE RESTART</div><div class="value">{{ count }}</div><div class="small">Stored in server memory</div></div>
<div class="stat"><div class="label">OPEN BOT POSITIONS</div><div class="value muted">Not connected</div><div class="small">No exchange position sync</div></div>
<div class="stat"><div class="label">REALIZED P&amp;L</div><div class="value muted">Not connected</div><div class="small">No execution records</div></div>
</section>
<section class="grid content"><article class="panel"><div class="paneltitle"><h2>RECENT CANDLE CHART</h2><div class="row"><label class="small" for="pair">Market</label><select id="pair"><option value="BTC_USDT">BTC / USDT</option><option value="ETH_USDT">ETH / USDT</option><option value="SOL_USDT">SOL / USDT</option><option value="XRP_USDT">XRP / USDT</option></select><span class="pill">15 MINUTES</span><button class="btn" id="refresh" type="button">↻ Refresh</button></div></div><div class="chartbox"><canvas id="candles" aria-label="Gate.io public 15-minute candlestick chart" role="img"></canvas></div><div class="sectionfoot" id="marketStatus" aria-live="polite">Loading public Gate.io candle data…</div><p class="note">Public market prices only. Chart is informational, not a trading signal, entry fill or execution record.</p></article>
<aside class="stack"><section class="panel"><div class="paneltitle"><h2>LIVE PAPER / REAL POSITION</h2><span class="pill">EXECUTION DISABLED</span></div><div class="empty">No synchronized bot positions.<br>Actual Gate.io position monitoring is not connected yet.</div><div class="summary" style="margin-top:12px"><div class="mini"><span>Entry</span><strong>—</strong></div><div class="mini"><span>Position side</span><strong>—</strong></div><div class="mini"><span>Stop loss</span><strong>—</strong></div><div class="mini"><span>Take profit 1 / 2</span><strong>—</strong></div></div></section>
<section class="panel"><div class="paneltitle"><h2>RISK CONFIGURATION</h2><span class="pill">PLANNED · NOT ACTIVE</span></div><div class="summary"><div class="mini"><span>Timeframe</span><strong>15m</strong></div><div class="mini"><span>Leverage</span><strong>5×</strong></div><div class="mini"><span>Risk per trade</span><strong>0.5%</strong></div><div class="mini"><span>Maximum positions</span><strong>1</strong></div><div class="mini"><span>Stop-loss plan</span><strong>1.5 ATR</strong></div><div class="mini"><span>TP1 / TP2 plan</span><strong>2 / 3 ATR</strong></div></div><p class="sectionfoot">Cross margin · Hedge mode. Values are targets for future controls, not active exchange protections.</p></section></aside></section>
<section class="grid below"><article class="panel"><div class="paneltitle"><h2>RECENT LUXALGO SIGNALS</h2><span class="pill">PRIVACY PROTECTED</span></div><div class="tablewrap"><table><thead><tr><th>Received (UTC)</th><th>Trading pair</th><th>Direction</th><th>Timeframe</th><th>Status</th></tr></thead><tbody><tr><td colspan="5" style="text-align:center;color:#94aec7;padding:26px">{{ count }} signal(s) received since restart. Individual signal details are not shown on the public page.</td></tr></tbody></table></div><p class="sectionfoot">Signal records remain in memory only and are lost at Railway restart. Private authenticated monitoring is required before showing pair/side history.</p></article>
<article class="panel"><div class="paneltitle"><h2>AI FUTURES OPPORTUNITY SCANNER</h2><span class="pill">NOT CONNECTED</span></div><div class="empty">Scanner integration pending.<br>No pump/bearish predictions are being generated.</div><p class="sectionfoot">Market chart above is live public data, not a recommendation.</p></article></section>
<section class="grid below"><article class="panel"><div class="paneltitle"><h2>COMPLETED PAPER / LIVE TRADES</h2><span class="pill">NO EXECUTION ENGINE</span></div><div class="tablewrap"><table><thead><tr><th>Pair</th><th>Side</th><th>Entry</th><th>Exit</th><th>SL / TP</th><th>Realized P&amp;L</th><th>Status</th></tr></thead><tbody><tr><td colspan="7" style="text-align:center;color:#94aec7;padding:26px">No verified executed trades are available in this application.</td></tr></tbody></table></div></article><article class="panel"><div class="paneltitle"><h2>PERFORMANCE ANALYTICS</h2><span class="pill">AWAITING VERIFIED TRADES</span></div><div class="summary"><div class="mini"><span>Win rate</span><strong>—</strong></div><div class="mini"><span>Total trades</span><strong>—</strong></div><div class="mini"><span>Net P&amp;L</span><strong>—</strong></div><div class="mini"><span>Maximum drawdown</span><strong>—</strong></div></div><p class="sectionfoot">Analytics require verified order fills and persistent trade history.</p></article></section>
<p class="foot">LUXALGO PRO · Railway monitor · Gate.io public market feed · Private API diagnostics preserved · Webhook preserved · Orders disabled. Protect this page with authentication before adding account or position details.</p>
</div><script>
(()=>{'use strict';const canvas=document.getElementById('candles'),pair=document.getElementById('pair'),status=document.getElementById('marketStatus'),button=document.getElementById('refresh');let candles=[];
function draw(){const rect=canvas.getBoundingClientRect(),w=Math.max(300,rect.width),h=Math.max(190,rect.height),scale=Math.min(devicePixelRatio||1,2);canvas.width=Math.round(w*scale);canvas.height=Math.round(h*scale);const ctx=canvas.getContext('2d');ctx.scale(scale,scale);ctx.clearRect(0,0,w,h);ctx.fillStyle='#101c2d';ctx.fillRect(0,0,w,h);if(!candles.length){ctx.fillStyle='#9db8d3';ctx.font='13px system-ui';ctx.fillText('No candle data available',16,30);return;}const left=13,right=65,top=13,bottom=27,plotW=w-left-right,plotH=h-top-bottom;const lo=Math.min(...candles.map(c=>c.l)),hi=Math.max(...candles.map(c=>c.h)),pad=Math.max((hi-lo)*.07,hi*.000001),min=lo-pad,max=hi+pad;const y=v=>top+(max-v)/(max-min)*plotH;ctx.strokeStyle='#213955';ctx.lineWidth=1;ctx.font='11px system-ui';ctx.fillStyle='#92a9c3';for(let i=0;i<=4;i++){const value=max-(max-min)*i/4,py=top+plotH*i/4;ctx.beginPath();ctx.moveTo(left,py);ctx.lineTo(w-right+5,py);ctx.stroke();ctx.fillText(value.toLocaleString(undefined,{maximumFractionDigits:value<1?5:value<100?3:1}),w-right+8,py+4)}let step=plotW/candles.length;let bar=Math.max(2,Math.min(11,step*.57));candles.forEach((c,i)=>{const x=left+step*(i+.5);ctx.strokeStyle=ctx.fillStyle=c.c>=c.o?'#35d2ab':'#f46b88';ctx.beginPath();ctx.moveTo(x,y(c.h));ctx.lineTo(x,y(c.l));ctx.stroke();const y1=y(Math.max(c.o,c.c)),y2=y(Math.min(c.o,c.c));ctx.fillRect(x-bar/2,y1,bar,Math.max(1,y2-y1))});ctx.fillStyle='#9db8d3';let indexes=[0,Math.floor((candles.length-1)/2),candles.length-1];indexes.forEach((i)=>{let d=new Date(candles[i].t*1000);ctx.fillText(d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}),left+step*i,h-7)})}
async function update(){button.disabled=true;status.textContent='Loading Gate.io public candles…';try{const response=await fetch('/market/candles?contract='+encodeURIComponent(pair.value),{cache:'no-store'});if(!response.ok)throw new Error('Market data unavailable (HTTP '+response.status+')');const data=await response.json();if(!Array.isArray(data.candles))throw new Error('Unexpected market response');candles=data.candles;draw();status.textContent=pair.value.replace('_',' / ')+' · 15m public candles · '+candles.length+' completed bars · updated '+new Date().toLocaleTimeString()}catch(err){candles=[];draw();status.textContent=err.message+' · Retry with Refresh'}finally{button.disabled=false}}
pair.addEventListener('change',update);button.addEventListener('click',update);window.addEventListener('resize',draw);update();})();
</script></body></html>'''


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
    return jsonify(status='ok', live_trading=False, orders_enabled=False, kind='signal_receiver_with_readonly_exchange_checks')


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
        sl, tp1, tp2 = entry - 1.5*atr, entry + 2*atr, entry + 3*atr
    else:
        sl, tp1, tp2 = entry + 1.5*atr, entry - 2*atr, entry - 3*atr
    if min(entry,sl,tp1,tp2,atr) <= 0:
        raise ValueError('Plan would produce nonpositive prices')
    return {
        'contract':contract, 'side':direction, 'timeframe':'15m',
        'reference':'most_recent_completed_15m_close_NOT_order_fill',
        'last_candle_start_utc':datetime.fromtimestamp(complete[-1][0], timezone.utc).isoformat(),
        'reference_entry':entry, 'atr_14_wilder':atr,
        'stop_loss_1_5_atr':sl, 'take_profit_1_2_atr':tp1,
        'take_profit_2_3_atr':tp2, 'planned_leverage':5,
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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '8080')))
