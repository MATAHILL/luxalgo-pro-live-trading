"""Isolated production preparation UI. No Testnet or paper order controls.

Visible LIVE mode is deliberately NOT permission to send real-money orders.
Retain legacy recovery access while Testnet positions/unknown orders remain.
"""
import os

def live_preparation_mode():
    return os.getenv("LIVE_TERMINAL_PREPARATION","false").strip().lower()=="true"

def live_preparation_checks(*,account_ok,positions_ok,db_ok,legacy_count=None,
                            webhook_ok,prod_write_requested=False,
                            dynamic_sizing_preview_ready=True):
    checks=[
      {"name":"Production Gate.io account READ access","passed":bool(account_ok),
       "detail":"Verified authenticated production USDT futures GET only"},
      {"name":"Production position inventory","passed":bool(positions_ok),
       "detail":"Account's open futures positions checked using read-only API"},
      {"name":"Durable PostgreSQL signal intake","passed":bool(db_ok and webhook_ok),
       "detail":"Persistent signal receipt and unique event audit; no order dispatch"},
      {"name":"Contract risk sizing calculator","passed":bool(dynamic_sizing_preview_ready),
       "detail":"0.5% risk/entry, 2.5% aggregate cap and 5x ceiling; read-only proposal"},
      {"name":"Dedicated production sender and order readbacks","passed":False,
       "detail":"Isolated signing and order lifecycle coded but SOURCE_LOCKED; real order submission and fill verification not tested"},
      {"name":"Real-money native SL/TP plus filled-order recovery","passed":False,
       "detail":"Native stop/target builders and strict readback checks coded; real exchange placement and orphan cancellation unverified"},
      {"name":"Atomic live risk budget / emergency close / kill switch","passed":False,
       "detail":"PostgreSQL transaction risk ledger, durable halt and one-shot reduce-only recovery coded; live stress/recovery unverified"},
      {"name":"Production live execution qualification","passed":False,
       "detail":"Minimum-size real-money order acceptance test is not authorized; no production orders sent"},
      {"name":"Production market feeds independent of order execution","passed":True,
       "detail":"Scanner and radar use public live Futures endpoints; this is architectural, not feed-uptime verification"}
    ]
    return {"status":"PREPARATION_ONLY","safe_to_trade":False,"orders_enabled":False,
            "live_trading":False,"orders_submitted":0,"checks":checks,
            "real_money_write_requested":bool(prod_write_requested),
            "notice":"No real-money order submission route is exposed or authorized"}

LIVE_PREPARATION_PAGE=r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>LuxAlgo PRO | Live Futures Preparation</title>
<style>
:root{color-scheme:dark;font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial;background:#080f1d;color:#e8f2ff}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at top left,#173454 0,#080f1d 52%);min-height:100vh}
header{padding:21px max(20px,calc((100vw - 1180px)/2));background:#0d1b2edc;border-bottom:1px solid #345275}
.brand{font-weight:900;font-size:22px;letter-spacing:1px}.sub{color:#b5cce1;font-size:12px;margin-top:5px}
main{max-width:1180px;margin:auto;padding:24px 18px 65px}
.hero{border:1px solid #466b83;padding:24px;border-radius:18px;background:linear-gradient(130deg,#152b42,#0e2032);margin-bottom:18px}
.hero h1{font-size:23px;margin:0 0 10px}.hero p{color:#b4cddd;max-width:900px;line-height:1.55}
.label{display:inline-flex;padding:7px 11px;background:#543827;border:1px solid #bd8e59;color:#ffe3bb;border-radius:7px;font-size:12px;font-weight:800}
.grid{display:grid;grid-template-columns:repeat(4,minmax(150px,1fr));gap:12px;margin:15px 0}
@media(max-width:810px){.grid{grid-template-columns:repeat(2,minmax(130px,1fr))}}
@media(max-width:500px){.grid{grid-template-columns:1fr}}
.card{background:#111f31;border:1px solid #344c64;border-radius:12px;padding:17px;min-width:0}
.card small{display:block;color:#a1bad0;font-size:11px;letter-spacing:.5px}
.card strong{font-size:22px;display:block;margin-top:7px;overflow-wrap:anywhere}
section.panel{background:#101d30;border:1px solid #344c64;border-radius:14px;margin-top:17px;padding:20px}
h2{font-size:17px;margin:0 0 13px}.hint{color:#abc4d8;line-height:1.5;font-size:13px}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{border-bottom:1px solid #2c4055;padding:12px;text-align:left;vertical-align:top}
th{color:#b5d4e7}.fail{color:#ffb4ae}.pass{color:#9af4c4}.muted{color:#9fb9cb}
button,.button{cursor:pointer;border:1px solid #85b9db;border-radius:9px;background:#143e60;color:#e8f7ff;padding:11px 15px;font-weight:700}
button:disabled{opacity:.45;cursor:not-allowed}a{color:#a3d8ff}
.actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
table.positions{font-variant-numeric:tabular-nums}
</style></head>
<body><header><div class="brand">LUXALGO PRO · FUTURES</div><div class="sub">PRODUCTION PREPARATION TERMINAL · Authenticated account checks · IST time</div></header>
<main>
<div class="hero"><span class="label">REAL MONEY OFF · NO ORDER SENDER</span><h1>Live Futures Terminal — Preparation</h1>
<p>LuxAlgo webhook reception and account monitoring are separated from trade execution. The production exchange sender, native stop-loss/take-profit protection, and risk reconciliation must pass independently before any real funds may be traded.</p>
<div class="actions"><button id="reload" type="button">↻ Refresh readiness</button><span class="muted" id="updated">Not checked</span></div>
</div>
<div class="grid">
<div class="card"><small>GATE.IO PRODUCTION READ-ONLY</small><strong id="connection">Checking…</strong></div>
<div class="card"><small>USDT FUTURES EQUITY</small><strong id="equity">—</strong></div>
<div class="card"><small>OPEN PRODUCTION POSITIONS</small><strong id="positionsCount">—</strong></div>
<div class="card"><small>LUXALGO RECEIVED EVENTS</small><strong id="events">—</strong></div>
</div>
<section class="panel"><h2>Production position monitor (exchange read-only)</h2><p class="hint">Positions below are from authenticated production Gate.io GET requests, not Testnet or paper records. Close, Reverse, Buy and Sell are intentionally disabled until a protected production sender is verified.</p>
<div style="overflow:auto"><table class="positions"><thead><tr><th>Contract</th><th>Side</th><th>Contracts</th><th>Entry</th><th>Mark</th><th>Unrealized P&amp;L (USDT)</th></tr></thead><tbody id="positionRows"><tr><td colspan="6">Loading…</td></tr></tbody></table></div>
</section>
<section class="panel"><h2>Live acceptance checklist</h2>
<p class="hint">A successful Testnet trade never automatically authorizes a production order. All execution-critical checks must pass before live trading can be enabled.</p>
<div style="overflow:auto"><table><thead><tr><th>Requirement</th><th>Status</th><th>Evidence / remaining work</th></tr></thead><tbody id="checks"><tr><td colspan="3">Checking…</td></tr></tbody></table></div>
</section>
<section class="panel" id="legacyPanel" hidden><h2>Existing Testnet positions — archival safety</h2>
<p class="hint" id="legacyText">Historical exchange protection must be reconciled before the old Testnet API or audit records can be removed.</p>
<a href="/terminal/legacy-testnet">Open protected legacy position/recovery terminal</a>
</section>
<section class="panel"><h2>Live order controls</h2><p class="hint">Real-money execution remains locked. These are informational only, not functional trading buttons.</p>
<div class="actions"><button disabled>Live Buy — Locked</button><button disabled>Live Sell — Locked</button><button disabled>Close — Locked</button><button disabled>Reverse — Locked</button></div></section>
<form method="post" action="/logout" style="margin-top:20px"><input type="hidden" name="csrf_token" value="{{ csrf_token|e }}"><button type="submit">Sign out securely</button></form>
</main>
<script>
(function(){
'use strict';
const by=id=>document.getElementById(id);
const fmt=v=>v===null||v===undefined?'—':String(v);
function add(tr,text){const td=document.createElement('td');td.textContent=fmt(text);tr.appendChild(td);}
async function refresh(){
  by('reload').disabled=true;
  try{
    const response=await fetch('/terminal/live-preparation/status',{cache:'no-store'});
    const d=await response.json();
    if(!response.ok||d.status!=='PREPARATION_ONLY')throw Error(d.error||'Read-only readiness unavailable');
    by('connection').textContent=d.account_verified?'READ ONLY · OK':'UNAVAILABLE';
    by('equity').textContent=d.account_verified?fmt(d.equity_usdt)+' USDT':'—';
    by('positionsCount').textContent=d.positions_verified?fmt(d.production_position_count):'—';
    by('events').textContent=d.receipts_verified?fmt(d.received_signals):'—';
    const tbody=by('positionRows');tbody.replaceChildren();
    if(d.positions_verified&&Array.isArray(d.production_positions)&&d.production_positions.length){
       for(const p of d.production_positions){const tr=document.createElement('tr');for(const v of [p.contract,p.side,p.size_contracts,p.entry_price,p.mark_price,p.unrealized_pnl])add(tr,v);tbody.appendChild(tr);}
    }else{const tr=document.createElement('tr');add(tr,d.positions_verified?'No open production futures positions':'Production positions cannot be verified');tr.firstChild.colSpan=6;tbody.appendChild(tr);}
    const checks=by('checks');checks.replaceChildren();
    for(const c of d.checks||[]){const tr=document.createElement('tr');add(tr,c.name);const cell=tr.insertCell();cell.textContent=c.passed?'PASS':'BLOCKED';cell.className=c.passed?'pass':'fail';add(tr,c.detail);checks.appendChild(tr);}
    // The production readiness view does not query or rely on archived Testnet records.
    by('legacyPanel').hidden=true;
    by('updated').textContent='Last checked: '+new Date().toLocaleTimeString('en-IN',{timeZone:'Asia/Kolkata'})+' IST · real-money orders OFF';
  }catch(e){by('updated').textContent='Readiness unavailable — real-money trading remains OFF';}
  finally{by('reload').disabled=false;}
}
by('reload').addEventListener('click',refresh);refresh();setInterval(()=>{if(!document.hidden)refresh();},60000);
})();
</script></body></html>'''
