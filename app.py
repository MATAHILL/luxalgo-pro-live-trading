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
from collections import deque
from hashlib import sha256
from datetime import datetime, timezone
from threading import Lock
from concurrent.futures import ThreadPoolExecutor
from xml.etree import ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
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
            cur.execute('''SELECT received_at, event_id, symbol, side, timeframe FROM luxalgo_signal_receipts
                           ORDER BY id DESC LIMIT 20''')
            rows = [dict(time=r['received_at'].isoformat(),symbol=r['symbol'],side=r['side'],tf=r['timeframe'],event_id=r['event_id'])
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
    return duplicate

PAGE = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>LUXALGO PRO · Trading Terminal</title><script defer src="https://cdn.jsdelivr.net/npm/lightweight-charts@5.0.8/dist/lightweight-charts.standalone.production.js"></script>
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
.metric-grid{grid-template-columns:repeat(6,minmax(0,1fr));gap:11px;margin:13px 0}
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
#newsFeedStatus{color:#99b5cd;font-size:10px;font-weight:500;white-space:nowrap}
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
.bottom-grid>section:first-child th:last-child,.bottom-grid>section:first-child td:last-child{min-width:352px;width:44%;white-space:nowrap}
.bottom-grid>section:first-child td:last-child .btn{display:inline-block;vertical-align:middle;white-space:nowrap;padding:5px 6px;margin:0 2px 0 0;font-size:10px;line-height:1.4;border-radius:5px;letter-spacing:0}
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
</style></head><body><div class="wrap"><header class="terminal-header"><div class="terminal-brand"><h1>LUXALGO PRO · TRADING TERMINAL</h1><p>Gate.io USDT Perpetual · LuxAlgo 5m / 15m · Coded by · SUBHASISH</p></div><section class="news-slot" aria-label="Latest crypto news"><div class="news-heading"><span>● CRYPTO NEWS</span><span id="newsFeedStatus">Loading headlines…</span></div><div class="news-viewport" id="newsViewport"><div class="news-stack" id="newsStack"><span class="news-empty">Retrieving publisher headlines…</span></div></div></section><div class="tag trading-status">● LIVE TRADING OFF</div></header>
<div class="grid metric-grid">
<div class="metric"><small>GATE.IO CONNECTION</small><strong class="gloss-cyan">READ-ONLY</strong><span id="gatePositionStatus">Checking position feed…</span></div>
<div class="metric"><small>OPEN POSITIONS</small><strong id="openPositionCount">—</strong><span>Verified Gate.io positions only</span></div>
<div class="metric"><small>UNREALIZED P&amp;L (USDT)</small><strong id="unrealizedPositionPnl">—</strong><span>Read-only, refreshed every 30s</span></div>
<div class="metric"><small>TOTAL REALIZED P&amp;L</small><strong>—</strong><span>Verified fills not available</span></div>
<div class="metric"><small>WIN RATE</small><strong>—</strong><span>Requires completed trades</span></div>
<div class="metric"><small>DATABASE SIGNALS</small><strong>{{ count }}</strong><span>Webhook: {{ 'configured' if configured else 'not configured' }}</span></div>
</div><section class="panel position-panel"><div class="panel-heading"><h2>OPEN POSITIONS <span class="gloss-cyan">· GATE.IO FUTURES</span></h2><button class="btn btn-danger" disabled title="Verified exchange positions and safe close-order execution are not connected">MANUALLY CLOSE SELECTED POSITION · DISABLED</button><button class="btn" id="positionRefresh" type="button">↻ Refresh Positions</button></div>
<div class="tablewrap"><table><thead><tr><th>PAIR</th><th>SIDE / SIZE</th><th>ENTRY</th><th>MARK PRICE</th><th>UNREALIZED P&amp;L</th><th>LEVERAGE</th><th>SL</th><th>TP1</th><th>STATE</th></tr></thead><tbody id="gatePositionsBody"><tr><td colspan="9" class="empty-state">Checking read-only Gate.io open positions…</td></tr></tbody></table></div><p id="gatePositionsUpdated" class="light">Read-only Gate.io account data; manual close disabled. SL/TP order status is not synchronized.</p></section><section class="panel"><h2>IN-TERMINAL MARKET CHART</h2>
<p class="light">Select a signal above, or enter a Gate.io USDT perpetual pair. Charts use public market candles only (not position/fill data).</p>
<div class="controls"><label>Trading pair <input id="chartSymbol" value="BTCUSDT" maxlength="29" autocomplete="off"></label><label>Chart timeframe<select id="chartTf"><option value="15m">15 minutes</option><option value="5m">5 minutes</option></select></label><label>Chart refresh <button class="btn" id="chartRefresh" type="button">Refresh Chart</button></label></div>
<div class="chart-toolbar" style="display:flex;flex-wrap:wrap;gap:8px;margin:10px 0;align-items:center"><button class="btn" id="zoomIn" type="button">＋ Zoom In</button><button class="btn" id="zoomOut" type="button">－ Zoom Out</button><button class="btn" id="resetChart" type="button">Reset View</button><button class="btn" id="olderChart" type="button">Load Older Candles</button><button class="btn" id="fullChart" type="button">⛶ Fullscreen</button><button class="btn" id="openTradingView" type="button">Open in TradingView ↗</button><span class="light" style="font-size:12px">Wheel: zoom · drag: pan · scroll back for history</span></div>
<div id="chartShell" style="position:relative;background:#0b1a2b;border:1px solid #29415e;border-radius:8px;overflow:hidden"><div id="tradeChart" style="height:410px;width:100%;min-height:320px" role="img" aria-label="Interactive candlestick market chart"></div></div>
<p class="light" id="chartStatus" aria-live="polite">Select a token to load its market chart.</p>
</section>
<div class="market-grid">
<section class="panel market-panel"><div class="panel-heading"><h2>GATE.IO FUTURES RADAR <span class="gloss-gold">● MARKET LIVE</span></h2><button class="btn" id="radarRefresh" type="button">↻ Refresh</button></div>
<p class="light">All available Gate.io USDT perpetual tickers · 24h rankings · public market data.</p>
<div class="market-tabs" id="radarTabs"><button class="btn selected" data-radar="gainers">TOP GAINERS</button><button class="btn" data-radar="losers">TOP LOSERS</button><button class="btn" data-radar="volume">TOP VOLUME</button><button class="btn" data-radar="all">ALL PAIRS</button></div>
<div class="market-filters"><input id="radarSearch" placeholder="Search pair (e.g. SOL)" maxlength="30"><select id="radarLimit"><option value="24">Top 24</option><option value="50">Top 50</option><option value="all">Show all</option></select></div>
<p class="light" id="radarStatus">Loading Gate.io perpetual market data…</p>
<div class="tablewrap market-scroll"><table><thead><tr><th>PAIR · CHART ↗</th><th>PRICE</th><th>24H %</th><th>24H VOL (USDT)</th></tr></thead><tbody id="radarBody"><tr><td colspan="4">Loading…</td></tr></tbody></table></div>
</section>
<section class="panel market-panel"><div class="panel-heading"><h2>AI FUTURES OPPORTUNITY SCANNER <span class="gloss-cyan">● TECHNICAL</span></h2><button class="btn" id="scannerRefresh" type="button">↻ Scan</button></div>
<p class="light">Rule-based opportunity watchlist · all accessible USDT perpetual pairs scanned progressively; not predictive AI.</p>
<div class="market-tabs" id="scannerTabs"><button class="btn selected" data-scan="bullish">BULLISH BREAKOUT</button><button class="btn" data-scan="mixed">EARLY / MIXED</button><button class="btn" data-scan="bearish">BEARISH BREAKDOWN</button><button class="btn" data-scan="all">ALL RESULTS</button></div>
<div class="market-filters"><select id="scannerTf"><option value="15m">15m</option><option value="5m">5m</option></select><input id="scannerSearch" placeholder="Search pair" maxlength="30"></div>
<p class="light" id="scannerStatus">Loading scanner…</p>
<div class="tablewrap market-scroll"><table><thead><tr><th>PAIR · CHART ↗</th><th>BIAS</th><th>SCORE</th><th>RSI</th><th>VOL RATIO</th></tr></thead><tbody id="scannerBody"><tr><td colspan="5">Scanning…</td></tr></tbody></table></div>
<p class="light">Technical labels reflect EMA trend, RSI, recent price-range break and candle volume. They are not LuxAlgo orders.</p>
</section></div>
<div class="bottom-grid"><section class="panel"><h2>RECENT LUXALGO SIGNALS · 5m / 15m</h2><div class="tablewrap"><table><thead><tr><th>Received (UTC)</th><th>Token</th><th>Signal</th><th>Timeframe</th><th>Status</th><th>Entry / SL / TP Plan</th></tr></thead><tbody>{% for r in records %}<tr><td>{{r.time[:19]|e}}</td><td>{{r.symbol|e}}</td><td>{{r.side|e}}</td><td>{{r.tf|e}}</td><td>Received · Not executed</td><td><button class="btn plan" data-symbol="{{r.symbol|e}}" data-side="{{r.side|e}}" data-tf="{{r.tf|e}}">Plan + Chart</button> <button class="btn signal-risk" type="button" data-received="{{r.time|e}}" data-symbol="{{r.symbol|e}}" data-side="{{r.side|e}}" data-tf="{{r.tf|e}}">Signal Risk Check</button> <button class="btn execution-check" type="button" data-event-id="{{r.event_id|e}}">Execution Precheck</button> <button class="btn simulate-order" type="button" data-event-id="{{r.event_id|e}}">Simulate Order</button></td></tr>{% else %}<tr><td colspan="6">No signals stored in the database yet.</td></tr>{% endfor %}</tbody></table></div><p id="executionCheckStatus" class="light" role="status" aria-live="polite">Execution engine foundation: protected prechecks only; exchange orders disabled.</p><p id="simulatedOrderStatus" class="light" role="status" aria-live="polite">Stage 2: explicit simulated order requests only; no actual exchange positions or protective orders.</p><p class="light">Signals are received events, not fills. Signal receipts and dry-run intent IDs are saved in PostgreSQL. Duplicate signals may not create separate dry-run intents.</p></section>
<section class="panel performance-panel"><div class="panel-heading"><h2>PERFORMANCE ANALYTICS</h2><span class="label-muted">VERIFIED FILLS ONLY</span></div><div class="perf-grid"><div><span>Total Closed Trades</span><strong>—</strong></div><div><span>Winning Trades</span><strong>—</strong></div><div><span>Losing Trades</span><strong>—</strong></div><div><span>Win Rate</span><strong>—</strong></div><div><span>Total Realized P&amp;L</span><strong>—</strong></div><div><span>Unrealized P&amp;L</span><strong>—</strong></div></div><p class="light">Waiting for authenticated position and fill synchronization. No synthetic P&amp;L or win statistics.</p></section></div><section class="panel"><h2>TRADE PLAN · SL · TP1 <span class="tag">READ ONLY</span></h2>
<div class="controls"><label>Entry reference <input id="entry" type="number" step="any" placeholder="Choose a signal" readonly></label><label>Stop Loss · 1.5 ATR <input id="sl" type="number" step="any" readonly></label><label>TP1 · 2 ATR · 100% exit <input id="tp1" type="number" step="any" readonly></label></div>
<div id="planResults">Select Plan + Chart beside a signal to calculate illustrative entry, SL and TP1. No orders are placed.</div></section>
<section class="panel"><div class="panel-heading"><h2>COMPLETED TRADES · EXIT RECORD</h2><span class="label-muted">PENDING ACCOUNT HISTORY INTEGRATION</span></div><div class="tablewrap"><table><thead><tr><th>TIME (UTC)</th><th>PAIR</th><th>DIRECTION</th><th>ENTRY FILL</th><th>EXIT FILL</th><th>NET REALIZED P&amp;L</th><th>RESULT</th></tr></thead><tbody><tr><td colspan="7" class="empty-state">No verified exchange fills have been synchronized. Signal receipts are not executed trades.</td></tr></tbody></table></div></section><section class="panel"><div class="panel-heading"><h2>PAPER TRADE MONITOR <span class="tag">SIMULATION ONLY</span></h2><button class="btn" id="paperRefresh" type="button">↻ Check Paper Trades</button></div><p class="light" id="paperStatus" role="status" aria-live="polite">Tracks completed candles only when you click refresh. It is not a live order or an exchange fill.</p><div class="tablewrap"><table><thead><tr><th>CREATED (UTC)</th><th>PAIR</th><th>SIDE</th><th>SIM ENTRY</th><th>SIM SL</th><th>SIM TP</th><th>STATE</th><th>EST. P&amp;L (USDT)</th></tr></thead><tbody id="paperBody"><tr><td colspan="8">No simulated positions loaded.</td></tr></tbody></table></div><p class="light">Simulation outcomes assume the reference entry was achievable and exclude fees, slippage and funding. If both SL and TP are touched in one candle, SL is assumed first. Historical-data gaps are not guessed. Simulated P&amp;L is never included in verified real-trade analytics.</p></section><section class="panel"><div class="panel-heading"><h2>EXECUTION SAFETY PREVIEW <span class="tag">DRY RUN ONLY</span></h2><span class="label-muted">NO EXCHANGE ORDERS</span></div><p class="light">Preview illustrative risk and contract-size checks using a hypothetical USDT equity. This does not validate your actual available margin, fee-adjusted fills, current hedge settings, or protective order placement.</p><div class="controls"><label>Trading pair<input id="previewSymbol" value="BTCUSDT" maxlength="29" autocomplete="off"></label><label>Direction<select id="previewSide"><option value="LONG">LONG / BUY</option><option value="SHORT">SHORT / SELL</option></select></label><label>Timeframe<select id="previewTf"><option value="15m">15 minutes</option><option value="5m">5 minutes</option></select></label><label>Hypothetical equity (USDT)<input id="previewEquity" type="number" value="100" min="1" max="10000000" step="any"></label><label>Risk per trade (%)<select id="previewRisk"><option value="0.5">0.5%</option><option value="1" selected>1% (default)</option><option value="1.5">1.5%</option><option value="2">2%</option><option value="2.5">2.5%</option><option value="3">3%</option><option value="3.5">3.5%</option><option value="4">4%</option><option value="4.5">4.5%</option><option value="5">5% (maximum)</option></select></label></div><button class="btn" id="previewButton" type="button">Generate Read-Only Risk Preview</button><p id="signalPreviewContext" class="light" aria-live="polite">Manual calculator input; not a trading recommendation.</p><div id="previewResult" role="status" aria-live="polite" style="white-space:pre-wrap;margin-top:12px;background:#0b1a2b;border:1px solid #2b435e;border-radius:8px;padding:14px;font:12px/1.8 ui-monospace,Consolas,monospace">Ready for a simulated preview. Nothing can place or close orders.</div></section><div class="foot">LUXALGO PRO · Private terminal · Market data is public Gate.io data. Scanners are technical watchlists, not LuxAlgo entry signals. No account positions, fills, profit or loss are available until authenticated exchange sync is implemented. LIVE TRADING OFF; no orders are placed.</div></div><script>
const entry=document.getElementById('entry'), sl=document.getElementById('sl'), tp1=document.getElementById('tp1');
// Use the genuine stored LuxAlgo direction to populate the existing risk calculator.
// This is a manual, read-only action: no webhook or trading API is invoked.
document.querySelectorAll('.signal-risk').forEach(b=>b.addEventListener('click',()=>{
 const symbol=b.dataset.symbol,side=b.dataset.side,tf=b.dataset.tf;
 const received=Date.parse(b.dataset.received);
 const ageSeconds=Number.isFinite(received)?Math.max(0,(Date.now()-received)/1000):null;
 document.getElementById('previewSymbol').value=symbol;
 document.getElementById('previewSide').value=side;
 document.getElementById('previewTf').value=tf;
 const out=document.getElementById('previewResult');
 const freshness=ageSeconds===null?'Signal time could not be verified.':ageSeconds>120?'STALE SIGNAL ('+Math.round(ageSeconds)+' seconds old); this is NOT an executable entry.':'Recently received signal ('+Math.round(ageSeconds)+' seconds old); this is still NOT an executable quote.';
 document.getElementById('signalPreviewContext').textContent='LuxAlgo '+symbol+' '+side+' '+tf+' · '+freshness+' Preview uses NEW completed market candles, not the historical signal entry. No orders.';
 document.getElementById('previewButton').click();
 document.getElementById('previewResult').scrollIntoView({behavior:'smooth',block:'center'});
}));


// Stage 1: record an immutable, fail-closed readiness check for stored alerts.
// No exchange order routes, signing of writes, or position changes exist.
document.querySelectorAll('.execution-check').forEach(b=>b.addEventListener('click',async()=>{
 const out=document.getElementById('executionCheckStatus');
 b.disabled=true;out.textContent='Recording a read-only execution safety precheck…';
 try{
  const r=await fetch('/terminal/execution-precheck',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({event_id:b.dataset.eventId}),cache:'no-store'});
  const d=await r.json();if(!r.ok)throw Error(d.error||'Precheck unavailable');
  const p=d.precheck;out.textContent=p.symbol+' '+p.side+' '+p.timeframe+' | '+p.status+' | Risk default: '+p.risk_percent+'% | '+(d.duplicate?'Existing persisted check. ':'Saved to PostgreSQL. ')+'No orders placed. Blockers: '+p.blockers.join('; ');
 }catch(e){out.textContent='Execution precheck blocked: '+e.message+'; no orders placed.'}
 finally{b.disabled=false}
}));

// Stage 2: simulate a hypothetical market entry from a stored fresh event; no exchange writes.
document.querySelectorAll('.simulate-order').forEach(b=>b.addEventListener('click',async()=>{
 const out=document.getElementById('simulatedOrderStatus');
 b.disabled=true;out.textContent='Creating a simulated order record (no actual orders)…';
 try{
  const r=await fetch('/terminal/simulate-order',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({event_id:b.dataset.eventId}),cache:'no-store'});
  const d=await r.json();if(!r.ok)throw Error(d.error||'Simulation rejected');
  const x=d.simulation;
  out.textContent=x.symbol+' '+x.side+' '+x.timeframe+' | '+x.status+' | Entry reference: '+x.reference_entry+' | Illustrative SL: '+x.stop_loss+' | Illustrative TP: '+x.take_profit+' | Contracts: '+x.size_contracts+' | Risk: '+x.risk_percent+'% | '+(d.duplicate?'Existing simulation; not duplicated.':'Saved to PostgreSQL.')+' | Exchange orders: 0. No real protection.';
 }catch(e){out.textContent='Simulation not created: '+e.message+' | Orders placed: 0.';}
 finally{b.disabled=false;}
}));

document.querySelectorAll('.plan').forEach(b=>b.addEventListener('click',async()=>{
 const out=document.querySelector('#planResults');b.disabled=true;out.textContent='Calculating read-only ATR plan…';
 try{const r=await fetch('/terminal/plan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:b.dataset.symbol,side:b.dataset.side,tf:b.dataset.tf})});const d=await r.json();if(!r.ok)throw Error(d.error||'Plan unavailable');const p=d.plan;
 entry.value=p.reference_entry;sl.value=p.stop_loss_1_5_atr;tp1.value=p.take_profit_1_2_atr;document.getElementById('chartSymbol').value=b.dataset.symbol;document.getElementById('chartTf').value=b.dataset.tf;window.loadChart();
 out.textContent=['Contract: '+p.contract,'Signal: '+p.side,'Timeframe: '+p.timeframe,'Reference: latest completed candle close (not a fill)','AUTO Stop Loss: 1.5 × ATR','AUTO TP1: 2 × ATR (100% close intended)','Order count: 0','Manual changes are previews only. No exchange orders or protection exist.'].join('\\n');
 }catch(e){out.textContent=e.message}finally{b.disabled=false}
}));

(function(){
'use strict';
const container=document.getElementById('tradeChart'),shell=document.getElementById('chartShell');
const symbol=document.getElementById('chartSymbol'),tf=document.getElementById('chartTf'),status=document.getElementById('chartStatus'),refresh=document.getElementById('chartRefresh');
let chart=null, series=null, bars=[],loading=false,olderLoading=false,loadedPair='',loadedTf='',pending=0;
const validPair=()=>symbol.value.trim().toUpperCase().replaceAll(' ','');
// Choose display precision from market price magnitude, not the default 2 decimals.
// This controls axis/crosshair labels only; it does NOT assert the exchange tick size.
function chartPriceFormat(candles){
 const prices=candles.flatMap(b=>[b.open,b.high,b.low,b.close]).filter(p=>Number.isFinite(p)&&p>0);
 if(!prices.length)return {type:'price',precision:2,minMove:0.01};
 const minPrice=Math.min(...prices);
 let precision=minPrice>=1?2:minPrice>=0.01?4:minPrice>=0.0001?6:minPrice>=0.000001?8:minPrice>=0.00000001?10:minPrice>=0.0000000001?12:14;
 return {type:'price',precision,minMove:Math.pow(10,-precision)};
}
function applyChartPriceFormat(candles){
 if(series&&candles.length)series.applyOptions({priceFormat:chartPriceFormat(candles)});
}
function initialize(){
 if(chart)return true;
 if(typeof LightweightCharts==='undefined'){
  status.textContent='Chart library did not load. Check network or browser content blockers, then refresh the page.';return false;
 }
 chart=LightweightCharts.createChart(container,{autoSize:true,layout:{background:{type:'solid',color:'#0b1a2b'},textColor:'#b8d5ed',attributionLogo:true},grid:{vertLines:{color:'#193047'},horzLines:{color:'#193047'}},rightPriceScale:{borderColor:'#365471'},timeScale:{borderColor:'#365471',timeVisible:true,secondsVisible:false,rightOffset:5},crosshair:{mode:LightweightCharts.CrosshairMode.Normal},handleScroll:{mouseWheel:true,pressedMouseMove:true,horzTouchDrag:true,vertTouchDrag:true},handleScale:{mouseWheel:true,pinch:true,axisPressedMouseMove:true}});
 series=chart.addSeries(LightweightCharts.CandlestickSeries,{upColor:'#3fe6ad',downColor:'#ef6983',borderVisible:false,wickUpColor:'#3fe6ad',wickDownColor:'#ef6983'});
 return true;
}
async function readCandles(before){
 const q=new URLSearchParams({symbol:validPair(),tf:tf.value});if(before)q.set('before',String(before));
 const r=await fetch('/terminal/chart?'+q,{cache:'no-store'}),d=await r.json();
 if(!r.ok)throw Error(d.error||'Market data unavailable');
 return (d.candles||[]).map(b=>({time:b.t,open:b.o,high:b.h,low:b.l,close:b.c})).sort((a,b)=>a.time-b.time);
}
async function loadChart(){
 const pair=validPair();if(!/^[A-Z0-9]{3,25}USDT$/.test(pair)){status.textContent='Enter a valid USDT pair, such as BTCUSDT';return;}
 if(!initialize())return;
 if(loading)return;
 const ticket=++pending;loading=true;refresh.disabled=true;
 const same=(loadedPair===pair&&loadedTf===tf.value);
 status.textContent='Loading '+pair+' · '+tf.value+' candles…';
 try{
  const batch=await readCandles();if(!batch.length)throw Error('No completed candles');
  if(ticket!==pending)return;
  if(!same){bars=batch;loadedPair=pair;loadedTf=tf.value;applyChartPriceFormat(bars);series.setData(bars);chart.timeScale().fitContent();}
  else{
   const range=chart.timeScale().getVisibleLogicalRange();
   bars=Array.from(new Map([...bars,...batch].map(b=>[b.time,b])).values()).sort((a,b)=>a.time-b.time);
   applyChartPriceFormat(bars);
   series.setData(bars);
   if(range)chart.timeScale().setVisibleLogicalRange(range);
  }
  status.textContent=pair+' · '+tf.value+' · '+bars.length+' historical completed candles · '+new Date().toLocaleTimeString()+' · Wheel to zoom, drag to pan. Read-only market chart.';
 }catch(e){status.textContent='Chart unavailable: '+e.message;}finally{loading=false;refresh.disabled=false;}
}
async function loadOlder(){
 if(olderLoading||!bars.length||!initialize())return;
 olderLoading=true;document.getElementById('olderChart').disabled=true;
 const pair=validPair(),oldest=bars[0].time,range=chart.timeScale().getVisibleLogicalRange();
 try{
  const batch=await readCandles(oldest);
  if(pair!==loadedPair||tf.value!==loadedTf)return;
  const earlier=batch.filter(b=>b.time<oldest);if(!earlier.length){status.textContent='No older candles were returned for this contract.';return;}
  bars=Array.from(new Map([...earlier,...bars].map(b=>[b.time,b])).values()).sort((a,b)=>a.time-b.time);
  applyChartPriceFormat(bars);
  series.setData(bars);
  if(range)chart.timeScale().setVisibleLogicalRange({from:range.from+earlier.length,to:range.to+earlier.length});
  status.textContent='Loaded '+earlier.length+' older candles · '+bars.length+' total · drag to browse history.';
 }catch(e){status.textContent='Historical data unavailable: '+e.message;}finally{olderLoading=false;document.getElementById('olderChart').disabled=false;}
}
refresh.addEventListener('click',loadChart);tf.addEventListener('change',loadChart);symbol.addEventListener('change',loadChart);
document.getElementById('zoomIn').addEventListener('click',()=>{if(initialize())chart.timeScale().setBarSpacing(Math.min(55,chart.timeScale().options().barSpacing*1.35));});
document.getElementById('zoomOut').addEventListener('click',()=>{if(initialize())chart.timeScale().setBarSpacing(Math.max(2,chart.timeScale().options().barSpacing/1.35));});
document.getElementById('resetChart').addEventListener('click',()=>{if(initialize())chart.timeScale().fitContent();});
document.getElementById('olderChart').addEventListener('click',loadOlder);
document.getElementById('fullChart').addEventListener('click',async()=>{try{if(document.fullscreenElement)await document.exitFullscreen();else await shell.requestFullscreen();}catch(e){status.textContent='Fullscreen blocked by browser: '+e.message;}});
document.getElementById('openTradingView').addEventListener('click',()=>{
 const pair=validPair();if(!/^[A-Z0-9]{3,25}USDT$/.test(pair))return;
 // TradingView account workspace opens separately: indicator availability is account-dependent.
 const tv='https://www.tradingview.com/chart/?symbol='+encodeURIComponent('GATEIO:'+pair+'.P');
 window.open(tv,'_blank','noopener,noreferrer');
});
window.loadChart=loadChart;
// Defer initialization until the external TradingView Lightweight Charts script has loaded.
function start(){if(typeof LightweightCharts==='undefined'){status.textContent='Loading interactive chart library…';setTimeout(start,400);return;}loadChart();}
start();
setInterval(()=>{if(!document.hidden)loadChart();},30000);
})();



// Independent dry-run calculator: illustrative sizing, never submits orders.
(function(){
 'use strict';
 const btn=document.getElementById('previewButton'),out=document.getElementById('previewResult');
 btn.addEventListener('click',async()=>{
  btn.disabled=true;out.textContent='Checking public contract rules and calculating preview…';
  try{
   const payload={symbol:document.getElementById('previewSymbol').value.trim().toUpperCase(),side:document.getElementById('previewSide').value,tf:document.getElementById('previewTf').value,hypothetical_equity_usdt:Number(document.getElementById('previewEquity').value),risk_percent:Number(document.getElementById('previewRisk').value)};
   const res=await fetch('/terminal/execution-preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload),cache:'no-store'});
   const d=await res.json();if(!res.ok)throw Error(d.error||'Preview unavailable');
   const v=d.preview;
   const fmt=x=>x===null||x===undefined?'Unavailable':String(x);
   out.textContent=['MODE: SIMULATION ONLY — NO ORDER SUBMISSION',
      'Pair / direction / timeframe: '+v.contract+' / '+v.side+' / '+v.timeframe,
      'Hypothetical equity: '+v.hypothetical_equity_usdt+' USDT',
      'Budgeted risk ('+v.risk_percent+'%): '+v.risk_budget_usdt+' USDT',
      'Reference entry (completed candle): '+v.reference_entry,
      'Stop loss (1.5 ATR): '+v.stop_loss,
      'TP1 (2 ATR; 100% planned exit): '+v.take_profit,
      'Contract multiplier: '+v.quanto_multiplier,
      'Conservative integer size: '+fmt(v.size_contracts)+' contracts',
      'Estimated price-only loss at SL: '+fmt(v.estimated_sl_loss_usdt)+' USDT (excludes fees/slippage)',
      'Estimated notional / 5x margin: '+fmt(v.notional_usdt)+' / '+fmt(v.estimated_margin_usdt)+' USDT',
      'Gate minimum / maximum contracts: '+v.order_size_min+' / '+v.order_size_max,
      'Result: '+v.result,
      ...v.blockers.map(x=>'BLOCKER: '+x),
      'Trading permissions: READ ONLY. Orders submitted: 0.',
      'Actual equity, available margin, leverage/hedge setting, protective orders, fees, execution and reconciliation are NOT verified.'].join('\\n');
  }catch(e){out.textContent='Preview unavailable: '+e.message+' · No orders submitted.'}finally{btn.disabled=false;}
 });
})();

// Private read-only Gate.io position monitor. No write endpoints or trading actions.
(function(){
'use strict';
const body=document.getElementById('gatePositionsBody'),updated=document.getElementById('gatePositionsUpdated');
const count=document.getElementById('openPositionCount'),pnl=document.getElementById('unrealizedPositionPnl');
const connection=document.getElementById('gatePositionStatus'),button=document.getElementById('positionRefresh');
let busy=false;
function cell(tr,value,cl){const td=tr.insertCell();td.textContent=value;if(cl)td.className=cl;return td;}
function money(value){if(value===null||value===undefined)return '—';const v=Number(value);return Number.isFinite(v)?v.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:4}):'—';}
function price(value){const v=Number(value);if(!Number.isFinite(v)||v<=0)return '—';return v.toLocaleString(undefined,{maximumSignificantDigits:10});}
function empty(msg){body.replaceChildren();const tr=body.insertRow();cell(tr,msg).colSpan=9;tr.className='empty-state';}
async function loadPositions(){
 if(busy)return;busy=true;button.disabled=true;
 try{
  const r=await fetch('/terminal/positions',{cache:'no-store'});
  const d=await r.json();if(!r.ok)throw Error(d.error||'Gate.io account temporarily unavailable');
  count.textContent=String(d.count);pnl.textContent=money(d.unrealized_pnl_usdt);
  pnl.classList.toggle('positive',d.unrealized_pnl_usdt>0);pnl.classList.toggle('negative',d.unrealized_pnl_usdt<0);
  connection.textContent='Position feed connected · READ ONLY';body.replaceChildren();
  if(!d.positions.length)empty('Gate.io reports no open USDT perpetual positions. No trades are being placed by LUXALGO PRO.');
  for(const p of d.positions){
   const tr=body.insertRow();cell(tr,p.contract);cell(tr,p.side+' · '+p.size_contracts+' contracts');
   cell(tr,price(p.entry_price));cell(tr,price(p.mark_price));
   cell(tr,money(p.unrealized_pnl),p.unrealized_pnl>=0?'positive':'negative');
   cell(tr,p.leverage_label);cell(tr,'Not verified');cell(tr,'Not verified');cell(tr,p.position_mode);
  }
  updated.textContent='Gate.io position snapshot · '+new Date(d.fetched_at*1000).toLocaleTimeString()+' · read-only · SL/TP order status not verified · manual close disabled.';
 }catch(e){
  count.textContent='—';pnl.textContent='—';pnl.classList.remove('positive','negative');
  connection.textContent='Position feed unavailable';empty('Unable to verify open positions. No zero balance or empty positions assumed.');
  updated.textContent='Position data unavailable: '+e.message;
 }finally{busy=false;button.disabled=false;}
}
button.addEventListener('click',loadPositions);loadPositions();setInterval(()=>{if(!document.hidden)loadPositions();},30000);
})();

(function(){
'use strict';
const radarBody=document.getElementById('radarBody'), radarStatus=document.getElementById('radarStatus');
const scanBody=document.getElementById('scannerBody'), scanStatus=document.getElementById('scannerStatus');
let tickerData=[],radarMode='gainers',scanMode='bullish',scanData=[],scanTf='15m';
const fmtPrice=n=>Number(n).toLocaleString(undefined,{maximumSignificantDigits:7});
const fmtVol=n=>n>=1e9?(n/1e9).toFixed(2)+'B':n>=1e6?(n/1e6).toFixed(2)+'M':n>=1e3?(n/1e3).toFixed(1)+'K':n.toFixed(0);
function addCell(tr,value,className){const td=document.createElement('td');td.textContent=value;if(className)td.className=className;tr.appendChild(td);return td;}
function addPair(tr,sym,tf){const td=tr.insertCell(),btn=document.createElement('button');btn.className='pairlink';btn.textContent=sym.replace('_','/');btn.addEventListener('click',()=>{document.getElementById('chartSymbol').value=sym.replace('_','');document.getElementById('chartTf').value=tf;window.loadChart();document.getElementById('chartShell').scrollIntoView({behavior:'smooth',block:'center'});});td.appendChild(btn);}
function renderRadar(){let list=tickerData.slice();const q=document.getElementById('radarSearch').value.trim().toUpperCase();if(q)list=list.filter(x=>x.contract.includes(q));list.sort((a,b)=>radarMode==='gainers'?b.change-a.change:radarMode==='losers'?a.change-b.change:radarMode==='volume'?b.volume-a.volume:a.contract.localeCompare(b.contract));const lim=document.getElementById('radarLimit').value;if(lim!=='all')list=list.slice(0,Number(lim));radarBody.replaceChildren();for(const x of list){const tr=document.createElement('tr');addPair(tr,x.contract,document.getElementById('chartTf').value);addCell(tr,fmtPrice(x.last));addCell(tr,(x.change>0?'+':'')+x.change.toFixed(2)+'%',x.change>=0?'positive':'negative');addCell(tr,fmtVol(x.volume));radarBody.appendChild(tr);}if(!list.length){const tr=radarBody.insertRow();addCell(tr,'No pairs match').colSpan=4;}}
async function loadRadar(){const b=document.getElementById('radarRefresh');b.disabled=true;radarStatus.textContent='Updating public Gate.io tickers…';try{const r=await fetch('/terminal/radar',{cache:'no-store'}),d=await r.json();if(!r.ok)throw Error(d.error||'Radar error');tickerData=d.results;radarStatus.textContent=d.total+' USDT perpetual pairs · updated '+new Date(d.generated_at*1000).toLocaleTimeString()+' · refresh every 60s';renderRadar();}catch(e){radarStatus.textContent='Radar unavailable: '+e.message;}finally{b.disabled=false}}
function renderScanner(){let list=scanData.filter(x=>scanMode==='all'||x.bias===scanMode);const q=document.getElementById('scannerSearch').value.trim().toUpperCase();if(q)list=list.filter(x=>x.contract.includes(q));list.sort((a,b)=>Math.abs(b.score)-Math.abs(a.score)||a.contract.localeCompare(b.contract));scanBody.replaceChildren();for(const x of list.slice(0,150)){const tr=document.createElement('tr');addPair(tr,x.contract,scanTf);addCell(tr,x.bias.toUpperCase(),x.bias==='bullish'?'positive':x.bias==='bearish'?'negative':'');addCell(tr,String(x.score));addCell(tr,x.rsi.toFixed(1));addCell(tr,x.volume_ratio.toFixed(2)+'x');scanBody.appendChild(tr);}if(!list.length){const tr=scanBody.insertRow();addCell(tr,'No matching scans yet').colSpan=5;}}
async function loadScanner(force=false){const b=document.getElementById('scannerRefresh');b.disabled=true;try{const r=await fetch('/terminal/scanner?tf='+scanTf+(force?'&refresh=1':''),{cache:'no-store'}),d=await r.json();if(!r.ok)throw Error(d.error||'Scanner error');scanData=d.results||[];scanStatus.textContent=(d.running?'Scanning… ':'Scan complete · ')+d.scanned+'/'+d.total+' pairs · '+d.failed+' unavailable · '+(d.generated_at?new Date(d.generated_at*1000).toLocaleTimeString():'pending')+' · technical watchlist';renderScanner();}catch(e){scanStatus.textContent='Scanner unavailable: '+e.message;}finally{b.disabled=false}}
for(const b of document.querySelectorAll('#radarTabs button'))b.addEventListener('click',()=>{radarMode=b.dataset.radar;document.querySelectorAll('#radarTabs button').forEach(x=>x.classList.toggle('selected',x===b));renderRadar();});
for(const b of document.querySelectorAll('#scannerTabs button'))b.addEventListener('click',()=>{scanMode=b.dataset.scan;document.querySelectorAll('#scannerTabs button').forEach(x=>x.classList.toggle('selected',x===b));renderScanner();});
document.getElementById('radarRefresh').addEventListener('click',loadRadar);document.getElementById('radarSearch').addEventListener('input',renderRadar);document.getElementById('radarLimit').addEventListener('change',renderRadar);document.getElementById('scannerSearch').addEventListener('input',renderScanner);document.getElementById('scannerRefresh').addEventListener('click',()=>loadScanner(true));document.getElementById('scannerTf').addEventListener('change',e=>{scanTf=e.target.value;scanData=[];loadScanner();});
loadRadar();loadScanner();setInterval(()=>{if(!document.hidden)loadRadar()},60000);setInterval(()=>{if(!document.hidden)loadScanner()},15000);
})();

// News is display-only; has no access to trading functions or signals.
(function(){
 'use strict';
 const viewport=document.getElementById('newsViewport');
 const stack=document.getElementById('newsStack');
 const status=document.getElementById('newsFeedStatus');
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
 async function load(){
   try{
     const ctl=new AbortController();const t=setTimeout(()=>ctl.abort(),9500);
     let res;
     try{res=await fetch('/terminal/news',{cache:'no-store',signal:ctl.signal})}finally{clearTimeout(t)}
     if(!res.ok)throw Error('News unavailable');
     const data=await res.json();render(data.items||[],data.fetched_at);
   }catch(e){status.textContent='News temporarily unavailable';if(!stack.querySelector('.news-item'))stack.textContent='News unavailable · trading terminal unaffected';}
 }
 load();setInterval(()=>{if(!document.hidden)load()},300000);
})();

// Stage 3: manual, read-only, completed-candle paper review; no trading API writes.
(function(){
 const btn=document.getElementById('paperRefresh'), body=document.getElementById('paperBody'), label=document.getElementById('paperStatus');
 if(!btn||!body)return;
 async function refresh(){
  btn.disabled=true;label.textContent='Checking completed candles for hypothetical trades…';
  try{
   const res=await fetch('/terminal/paper-trades',{cache:'no-store'});const data=await res.json();
   if(!res.ok)throw Error(data.error||'Paper review unavailable');
   body.replaceChildren();
   const rows=data.trades||[];
   if(!rows.length){const r=body.insertRow(),c=r.insertCell();c.colSpan=8;c.textContent='No simulated orders stored yet.';}
   for(const x of rows){
    const r=body.insertRow();
    for(const v of [x.created_at,x.symbol,x.side,x.entry,x.stop,x.take,x.state,x.estimated_pnl_usdt===null?'—':x.estimated_pnl_usdt]){
     const c=r.insertCell();c.textContent=String(v===null?'—':v);
    }
   }
   label.textContent=rows.length+' stored simulated intents checked · '+(data.errors||0)+' unverified; no orders or exchange fills.';
  }catch(e){label.textContent='Paper review unavailable: '+e.message+' (real trading remains OFF)';}
  finally{btn.disabled=false;}
 }
 btn.addEventListener('click',refresh);
})();
</script></body></html>'''


# Curated public publisher feeds. This UI is editorial news, not verified trade signals.
NEWS_FEEDS = (
    ('CoinDesk', 'https://www.coindesk.com/arc/outboundfeeds/rss/', 'www.coindesk.com'),
    ('The Block', 'https://www.theblock.co/rss.xml', 'www.theblock.co'),
    ('Decrypt', 'https://decrypt.co/feed', 'decrypt.co'),
)
_news_cache = {'expires_at': 0, 'items': [], 'fetched_at': None}
_news_lock = Lock()

def _news_fetch_source(source):
    label, url, host = source
    req = Request(url, headers={'User-Agent':'LuxAlgoPRO-NewsTicker/1.0 (+RSS reader)', 'Accept':'application/rss+xml,application/atom+xml,application/xml,text/xml'})
    with urlopen(req, timeout=4) as res:
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
                      'published': datetime.fromtimestamp(date_ts,timezone.utc).strftime('%d %b %H:%M UTC'),
                      '_timestamp':date_ts})
    return items

@app.get('/terminal/news')
def terminal_news():
    if not _terminal_permitted():
        return _terminal_guard()
    now=time.time()
    with _news_lock:
        cached=dict(_news_cache)
    if cached['expires_at']>now:
        response=jsonify(items=cached['items'],fetched_at=cached['fetched_at'],status='cached')
        response.headers['Cache-Control']='private, no-store'
        return response
    found=[]
    # Independent timeouts; publisher outages must never break the trading dashboard.
    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs=[pool.submit(_news_fetch_source, source) for source in NEWS_FEEDS]
        for job in jobs:
            try: found.extend(job.result(timeout=5))
            except Exception: pass
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
    # A separate dashboard password; never reuse webhook token or exchange credentials.
    expected = os.getenv('TERMINAL_PASSWORD', '')
    user = os.getenv('TERMINAL_USER', 'admin')
    auth = request.authorization
    return (len(expected) >= 24 and auth is not None
            and hmac.compare_digest(auth.username or '', user)
            and hmac.compare_digest(auth.password or '', expected))


def _terminal_guard():
    if not os.getenv('TERMINAL_PASSWORD'):
        return Response('Private terminal unavailable. Configure TERMINAL_PASSWORD in Render environment variables.', status=503)
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
    if not re.fullmatch(r'[A-Z0-9]{3,25}USDT', symbol) or side not in allowed or tf not in ('5', '5m', '5min', '5 minutes', '15', '15m', '15min', '15 minutes'):
        return jsonify(error='Invalid symbol, side, or timeframe'), 422
    received_at = datetime.now(timezone.utc)
    tf = '5m' if tf.startswith('5') else '15m'
    record = {'dt':received_at,'symbol':symbol,'side':allowed[side],'tf':tf}
    # This event key is based on RECEIVE time, not a verified candle ID.
    # For live orders, require a TradingView event/bar identifier and
    # exchange reconciliation before accepting any automated trades.
    bucket = int(received_at.timestamp()) // (300 if tf == '5m' else 900)
    event_id = sha256(f'{symbol}|{allowed[side]}|{tf}|{bucket}'.encode()).hexdigest()[:20]
    try:
        duplicate = _db_record(record,event_id)
    except (psycopg.Error,RuntimeError,OSError):
        app.logger.exception('Cannot durably record incoming LuxAlgo signal')
        return jsonify(error='Signal not stored; retry later',orders_placed=0),503
    return jsonify(status='signal_received', mode='SIGNAL_ONLY', symbol=symbol,
                   side=allowed[side], timeframe=tf, duplicate_in_candle=duplicate,
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




def _gate_read_only_positions():
    """Authenticated read-only GET; return only sanitized open position fields."""
    key = os.getenv('GATE_API_KEY', '').strip()
    secret = os.getenv('GATE_API_SECRET', '').strip()
    if not key or not secret:
        raise RuntimeError('Gate.io API credentials not configured')
    path = '/api/v4/futures/usdt/positions'
    timestamp = str(int(time.time()))
    payload_hash = hashlib.sha512(b'').hexdigest()
    message = '\n'.join(('GET', path, '', payload_hash, timestamp))
    signature = hmac.new(secret.encode(), message.encode(), hashlib.sha512).hexdigest()
    req = Request('https://api.gateio.ws' + path, method='GET', headers={
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
        size = number(p.get('size', 0))
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
        entry = number(p['entry_price'])
        mark = number(p['mark_price'])
        unrealized = number(p['unrealised_pnl'])
        if entry <= 0 or mark <= 0:
            raise ValueError('Invalid position prices')
        margin_mode = str(p.get('pos_margin_mode') or '').lower()
        leverage = str(p.get('lever') or '').strip()
        if not leverage or leverage == '0':
            leverage = str(p.get('cross_leverage_limit') if margin_mode == 'cross' else p.get('leverage') or '0')
        leverage_label = (leverage + '×' if leverage not in ('0','None','') else '—')
        if margin_mode in ('cross','isolated'):
            leverage_label += ' · ' + margin_mode.title()
        positions.append(dict(contract=contract, side=side, size_contracts=abs(size),
                              entry_price=entry, mark_price=mark, unrealized_pnl=unrealized,
                              leverage_label=leverage_label, position_mode=mode))
    return positions


@app.get('/terminal/positions')
def terminal_positions():
    if not _terminal_permitted():
        return _terminal_guard()
    try:
        positions = _gate_read_only_positions()
        response = jsonify(positions=positions, count=len(positions),
                           unrealized_pnl_usdt=round(sum(p['unrealized_pnl'] for p in positions), 8),
                           fetched_at=int(time.time()), orders_enabled=False, live_trading=False)
        response.headers['Cache-Control'] = 'no-store'
        return response
    except RuntimeError:
        return jsonify(error='Gate.io read-only credentials unavailable', orders_enabled=False),503
    except HTTPError as exc:
        app.logger.warning('Gate positions HTTP status %s', exc.code)
        return jsonify(error='Gate.io rejected the read-only position request', orders_enabled=False),502
    except (URLError, TimeoutError, ValueError, KeyError, TypeError, OSError):
        app.logger.warning('Gate positions response unavailable or invalid')
        return jsonify(error='Position data temporarily unavailable or invalid', orders_enabled=False),502

def _gate_atr_plan(symbol, direction, tf="15m"):
    """Read ONLY public, completed timeframe candles. Return an illustrative plan.

    Not order sizing: 0.5% risk requires verified available funds, instrument
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
        risk_percent = Decimal(os.getenv('DEFAULT_RISK_PERCENT', '1'))
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
           urlencode({'contract':contract,'interval':tf,'from':max(0,start-seconds), 'to':now, 'limit':1000}))
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


@app.get('/terminal/paper-trades')
def terminal_paper_trades():
    if not _terminal_permitted():return _terminal_guard()
    if os.getenv('LIVE_TRADING_ENABLED','false').strip().lower() != 'false':
        return jsonify(error='Paper monitor requires LIVE_TRADING_ENABLED=false'),503
    output=[];errors=0
    try:
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
        response=jsonify(trades=output,errors=errors,mode='HISTORICAL_PAPER_ONLY',orders_placed=0)
        response.headers['Cache-Control']='no-store'
        return response
    except (psycopg.Error,RuntimeError,OSError):
        app.logger.exception('Paper tracking database unavailable')
        return jsonify(error='Paper tracking DB unavailable',orders_placed=0),503


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
    except (HTTPError,URLError,OSError,TimeoutError,ValueError,TypeError):
        return jsonify(error='Gate.io ticker data unavailable'),502


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
            'score':score,'rsi':round(rsi,2),'volume_ratio':round(vr,2),'timeframe':tf}


def _run_scan(tf):
    try:
        tickers = _radar_data()['results']
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
                except (HTTPError,URLError,TimeoutError,OSError,KeyError,ValueError,TypeError,ZeroDivisionError):
                    with _scan_state_lock:
                        _scan_state[tf]['failed'] += 1
    except (HTTPError,URLError,TimeoutError,OSError,KeyError,ValueError,TypeError):
        app.logger.warning('Technical scanner could not access market data')
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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '8080')))
