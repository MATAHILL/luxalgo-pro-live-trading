"""Self-contained, read-only dashboard for LUX DIRECT BOT.

Never embeds API credentials, webhook secrets or user access tokens in HTML.
"""
DASHBOARD_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark"><title>LUX DIRECT BOT | Trading Terminal</title>
<style>
:root{color-scheme:dark;--bg:#0b1019;--card:#141d2b;--line:#29374b;--muted:#98a7bc;--main:#edf4ff;--accent:#60e4b2;--warn:#ffd68a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;color:var(--main)}
.wrapper{max-width:1100px;margin:auto;padding:26px 18px 70px}
header{display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap;margin-bottom:26px}
.brand{font-weight:800;font-size:22px;letter-spacing:.04em}.brand small{display:block;font-size:12px;font-weight:500;letter-spacing:normal;color:var(--muted);margin-top:2px}
.pill{border:1px solid #5c4b2e;border-radius:999px;background:#332918;color:var(--warn);font-weight:700;font-size:12px;padding:7px 12px}
.card{border:1px solid var(--line);border-radius:15px;background:var(--card);padding:20px;box-shadow:0 12px 32px #0001}
.warning{padding:17px 20px;margin-bottom:20px;border-left:4px solid #f5b85c}
.warning strong{display:block;margin-bottom:4px}.warning p{margin:0;color:#c6d0df;font-size:14px}
.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin-bottom:20px}
.kpi .label{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.07em}
.kpi .value{margin:9px 0 3px;font-size:19px;font-weight:750;word-break:break-word}
.kpi .hint{font-size:12px;color:var(--muted)}
.section-head{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:14px}
h1{font-size:19px;margin:0}h2{font-size:17px;margin:0}
.actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
button{background:#1c3041;color:#eff6ff;border:1px solid #3b596a;padding:10px 15px;border-radius:9px;cursor:pointer;font-weight:650}
button.primary{background:#1b715d;border-color:#31987c}button:hover{filter:brightness(1.1)}
input{background:#101824;border:1px solid #425269;border-radius:9px;padding:11px 12px;color:white;font:inherit;min-width:230px;max-width:100%;width:260px}
p.muted,.muted{color:var(--muted)}
.login{display:flex;flex-wrap:wrap;gap:9px;align-items:center;margin:12px 0 18px}
.scroll{width:100%;overflow-x:auto}table{border-collapse:collapse;width:100%;min-width:690px;font-size:13px}
th{text-align:left;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);font-size:11px;padding:11px;border-bottom:1px solid var(--line)}
td{padding:12px 11px;border-bottom:1px solid #253244}tr:last-child td{border-bottom:0}
.buy{color:#61dfac;font-weight:800}.sell{color:#ff9496;font-weight:800}
.setup{margin-bottom:20px}.setup .muted{font-size:13px}.setup pre{background:#0e1824;border:1px solid var(--line);border-radius:8px;padding:12px;white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}
.note{margin-top:18px;font-size:12px;color:var(--muted);display:flex;justify-content:space-between;flex-wrap:wrap;gap:10px}
strong.accent{color:var(--accent)}.hidden{display:none!important}
@media(max-width:760px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.wrapper{padding:16px 12px 48px}.card{padding:15px}.kpi .value{font-size:16px}}
@media(max-width:420px){.grid{grid-template-columns:1fr}.brand{font-size:20px}}
</style></head><body>
<main class="wrapper">
<header><div class="brand">LUX DIRECT BOT<small>BINANCE:XRPUSDT.P · 5 minutes · LuxAlgo confirmation signals</small></div><span class="pill" id="mode">DRY RUN · ORDERS OFF</span></header>
<section class="card warning" aria-live="polite"><strong>Real-money trading is disabled</strong><p id="warning">Binance Futures is not connected. No orders, positions or profit figures can be shown until the exchange connection is verified.</p></section>
<section class="grid" aria-label="System status">
  <div class="card kpi"><div class="label">Service</div><div class="value" id="service">Checking…</div><div class="hint">Render web service</div></div>
  <div class="card kpi"><div class="label">LuxAlgo signals</div><div class="value" id="signals">Not verified</div><div class="hint" id="signalsHint">Waiting for actual alert</div></div>
  <div class="card kpi"><div class="label">Binance Futures</div><div class="value" id="exchange">Not connected</div><div class="hint">No API access installed</div></div>
  <div class="card kpi"><div class="label">Orders / positions</div><div class="value" id="positionValue">Unavailable</div><div class="hint" id="positionHint">Exchange connection required</div></div>
</section>
<section class="card setup" aria-labelledby="setupTitle">
  <div class="section-head"><h2 id="setupTitle">TradingView connection</h2><span class="pill">Trading OFF</span></div>
  <p class="muted">Private webhook and LuxAlgo alert JSON. Unlock using your Render operator token below. This page does not access TradingView or submit orders.</p>
  <div id="setupControls" class="hidden">
    <div class="actions">
      <button id="copyWebhook" type="button">Copy private webhook URL</button>
      <button id="copyTemplate" type="button">Copy LuxAlgo alert JSON</button><button id="safeTest" type="button">Test signal reception (dry run)</button>
      <button id="probeBinance" type="button">Check Binance public API</button><button id="readBinance" type="button">Read Binance account</button>
    </div>
    <details style="margin-top:12px"><summary>Show alert JSON template</summary>
      <pre id="setupTemplate"></pre>
    </details>
  </div>
  <p class="muted" id="setupMessage">Operator authentication required. Private webhook URL is not shown publicly.</p>
  <p class="muted" id="safeTestMessage">No simulated signal test has been run in this browser session. Real LuxAlgo alert reception remains unverified.</p>
  <p class="muted" id="connectionMessage" role="status">TradingView webhook status: no independently confirmed delivery.</p>
  <p class="muted" id="receiverMessage" role="status">Receiver diagnostics: pending operator login.</p>
  <p class="muted" id="binanceMessage">Binance account API: not connected. No live trading.</p><div id="accountBox" class="hidden"><div class="muted" id="balanceLabel" style="margin:10px 0;font-size:13px"></div><div class="scroll"><table><thead><tr><th>Symbol</th><th>Side</th><th>Amount</th><th>Entry</th><th>Mark</th><th>Unrealized P&L</th></tr></thead><tbody id="positionRows"></tbody></table></div></div>
</section>
<section class="card" aria-labelledby="signalTitle">
  <div class="section-head"><div><h1 id="signalTitle">Received LuxAlgo signals</h1><div class="muted" style="font-size:12px">Actual webhook records only · No simulated trade fills</div></div>
    <div class="actions"><button id="refresh" type="button">Refresh now</button><button id="lock" type="button" class="hidden">Lock signals</button></div>
  </div>
  <form id="login" class="login" autocomplete="off">
    <input id="token" name="token" type="password" placeholder="Operator access token" aria-label="Operator access token" required>
    <button class="primary" type="submit">Unlock signal history</button>
  </form>
  <p class="muted" id="message" role="status">Signal history is private. Unlock it using your Render ADMIN_TOKEN. The token is never included in the URL or stored by this page.</p>
  <div class="scroll hidden" id="tableBox"><table><thead><tr>
    <th>Received (IST)</th><th>Symbol</th><th>Original action</th><th>Timeframe</th><th>Status</th><th>Event</th>
  </tr></thead><tbody id="rows"></tbody></table></div>
</section>
<div class="note"><span>Read-only dashboard. No BUY/SELL buttons or live-order controls.</span><span id="checked">Health not checked yet</span></div>
</main>
<script>
(function(){
'use strict';
var token = '';
var setupConfig = null;
var $ = function(id){return document.getElementById(id);};
function show(id, visible){$(id).classList.toggle('hidden', !visible);}
function setText(id,text){$(id).textContent=text;}
function fmt(when){
  try{return new Date(when*1000).toLocaleString('en-IN',{timeZone:'Asia/Kolkata',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:true});}
  catch(_){return '—';}
}
async function health(){
  try{
    var resp=await fetch('/health',{cache:'no-store'});
    if(!resp.ok)throw new Error('HTTP '+resp.status);
    var result=await resp.json();
    setText('service','Online');
    setText('mode',String(result.mode||'UNKNOWN')+' · ORDERS OFF');
    setText('exchange','Not connected');
    setText('checked','Last checked: '+new Date().toLocaleTimeString('en-IN',{timeZone:'Asia/Kolkata',hour12:true})+' IST');
    if(result.live_trading!==false){setText('warning','Unexpected live-trading status. Stop and investigate.');}
  }catch(err){
    setText('service','Unreachable');
    setText('warning','Unable to verify bot service status. Please retry.');
    setText('checked','Health check failed');
  }
}
async function loadSetup(){
  if(!token)return;
  try{
    var res=await fetch('/setup',{cache:'no-store',headers:{'X-Admin-Token':token}});
    if(!res.ok){setText('setupMessage','Operator authentication required for setup.');return;}
    setupConfig=await res.json();
    if(!setupConfig.webhook_url){setText('setupMessage','Webhook configuration not yet available.');return;}
    show('setupControls',true);
    setText('setupTemplate',setupConfig.luxalgo_alert_message||'');
    setText('setupMessage','Ready: copy the private URL and JSON into a LuxAlgo Any alert() function call alert. Enable bullish and bearish confirmations only. Keep the webhook URL secret.');
  }catch(error){setText('setupMessage','Unable to load private alert settings: '+error.message);}
}
async function copySetup(field){
  if(!token || !setupConfig || !setupConfig[field])return;
  try{
    await navigator.clipboard.writeText(setupConfig[field]);
    setText('setupMessage',field==='webhook_url'?'Private webhook URL copied. Paste only into TradingView webhook settings.':'LuxAlgo alert message copied. Paste into the LuxAlgo custom alert message field.');
  }catch(error){
    setText('setupMessage','Clipboard permission unavailable. Copy from the operator setup page using a secure browser.');
  }
}
async function diagnoseBinance(){
  if(!token)return;
  setText('binanceMessage','Checking public Binance Futures connectivity (no private account access)...');
  try{
    var resp=await fetch('/binance/diagnostics',{cache:'no-store',headers:{'X-Admin-Token':token}});
    if(!resp.ok)throw new Error('HTTP '+resp.status);
    var value=await resp.json();
    setText('binanceMessage','Binance public API: '+value.public_api+' · XRPUSDT: '+value.xrp_usdt+
      ' · Account API: NOT YET CHECKED · Orders: OFF'+
      (value.reason?' · Reason: '+value.reason:'')+
      (value.http_status?' · Exchange HTTP '+value.http_status:'')+'.');
  }catch(error){setText('binanceMessage','Binance diagnostics unavailable: '+error.message);}
}
async function readBinanceAccount(){
  if(!token)return;
  setText('binanceMessage','Checking Binance USD-M Futures account (read-only)...');
  show('accountBox',false);
  try{
    var resp=await fetch('/binance/account',{cache:'no-store',headers:{'X-Admin-Token':token}});
    if(!resp.ok)throw new Error('HTTP '+resp.status);
    var value=await resp.json();
    if(value.status!=='CONNECTED_READ_ONLY'){
      setText('binanceMessage','Binance signed account: '+value.status+
        '. No orders can be placed. '+
        (value.http_status?'Exchange HTTP '+value.http_status:'')+
        (value.reason?' · Reason: '+value.reason:''));
      setText('exchange','Not connected');
      setText('positionValue','Unavailable');
      return;
    }
    setText('exchange','Read only');
    setText('positionValue',String(value.positions.length)+' positions');
    setText('positionHint','Verified exchange data (read only)');
    setText('balanceLabel','USDT wallet: '+String(value.usdt_balance??'not returned')+' · Available: '+String(value.usdt_available??'not returned'));
    $('positionRows').replaceChildren();
    for(var p of value.positions){
      var tr=document.createElement('tr');
      for(var field of [p.symbol,p.position_side,p.amount,p.entry_price,p.mark_price,p.unrealized_pnl]){
        var td=document.createElement('td');td.textContent=String(field??'—');tr.appendChild(td);
      }
      $('positionRows').appendChild(tr);
    }
    show('accountBox',true);
    setText('binanceMessage','Binance authenticated READ-ONLY connection verified. Live orders remain disabled.');
  }catch(error){setText('binanceMessage','Account read failed: '+error.message);}
}
async function runSafeTest(){
  if(!token)return;
  setText('safeTestMessage','Checking parser, BUY/SELL acceptance, database write, and duplicate handling...');
  try{
    var response=await fetch('/admin/selftest',{method:'POST',cache:'no-store',headers:{'X-Admin-Token':token}});
    if(!response.ok)throw new Error('HTTP '+response.status);
    var result=await response.json();
    if(result.result!=='PASS' || result.test_records_persisted!==0 || result.exchange_orders_sent!==0)
      throw new Error('Unexpected self-test outcome');
    setText('safeTestMessage','DRY-RUN SELF-TEST PASSED · BUY/SELL checks and duplicate protection succeeded · Zero simulated signals saved · Zero exchange orders. Actual TradingView alerts are NOT YET verified.');
  }catch(error){
    setText('safeTestMessage','Self-test failed: '+error.message+' · Trading remains OFF.');
  }
}
async function loadConnection(){
  if(!token)return;
  try{
    var r=await fetch('/connection/status',{cache:'no-store',headers:{'X-Admin-Token':token}});
    if(!r.ok)throw new Error('HTTP '+r.status);
    var data=await r.json();
    if(data.state==='NO_WEBHOOK_RECORDS'){
      setText('connectionMessage','TradingView webhook: no accepted webhook records observed. The real alert remains UNVERIFIED.');
    }else{
      setText('connectionMessage','Webhook receiver: '+data.records+' recorded alerts; last accepted '+fmt(data.latest.at)+'. Sender cannot be independently confirmed from this record. Trading stays OFF.');
    }
  }catch(e){setText('connectionMessage','Cannot verify webhook evidence: '+e.message);}
}
async function loadReceiverAttempts(){
  if(!token)return;
  try{
    var r=await fetch('/connection/attempts',{cache:'no-store',headers:{'X-Admin-Token':token}});
    if(!r.ok)throw new Error('HTTP '+r.status);
    var data=await r.json(), rows=data.attempts||[];
    if(!rows.length){
      setText('receiverMessage','Receiver diagnostics: no authenticated webhook attempts recorded on this instance.');
      return;
    }
    var counts={ACCEPTED:0,DUPLICATE:0,REJECTED:0};
    for(var item of rows){if(Object.prototype.hasOwnProperty.call(counts,item.result)) counts[item.result]++;}
    var recent=rows[0];
    setText('receiverMessage','Last '+rows.length+' webhook attempts: accepted '+counts.ACCEPTED+
      ', duplicate '+counts.DUPLICATE+', rejected '+counts.REJECTED+
      '. Latest: '+recent.result+' ('+recent.reason+') at '+fmt(recent.at)+' IST. Sender not independently verified.');
  }catch(error){
    setText('receiverMessage','Receiver diagnostics unavailable: '+error.message);
  }
}
function draw(records){
  $('rows').replaceChildren();
  if(!records.length){setText('message','No authenticated LuxAlgo signals recorded yet. No trade has been executed.');show('tableBox',false);setText('signals','0 received');return;}
  for(var r of records){
    var tr=document.createElement('tr');
    var values=[fmt(r.at),r.symbol,r.side,r.timeframe,r.status,r.event];
    for(var i=0;i<values.length;i++){
      var td=document.createElement('td');
      td.textContent=String(values[i]??'—');
      if(i===2)td.className=r.side==='BUY'?'buy':r.side==='SELL'?'sell':'';
      tr.appendChild(td);
    }
    $('rows').appendChild(tr);
  }
  setText('signals',String(records.length)+' recent');
  setText('signalsHint','Latest: '+fmt(records[0].at)+' IST');
  setText('message','Authorized signal history · Only actual received webhooks');
  show('tableBox',true);
}
async function signals(){
  if(!token)return;
  try{
    var r=await fetch('/signals',{cache:'no-store',headers:{'X-Admin-Token':token}});
    if(r.status===403){token='';setText('message','Invalid operator token. Check the Render secret and retry.');show('login',true);show('lock',false);show('tableBox',false);return;}
    if(!r.ok)throw new Error('HTTP '+r.status);
    var records=await r.json();
    if(!Array.isArray(records))throw new Error('Unexpected response');
    draw(records);
  }catch(e){setText('message','Unable to load signal history: '+e.message);}
}
$('login').addEventListener('submit',function(e){e.preventDefault();token=$('token').value.trim();$('token').value='';show('login',false);show('lock',true);signals();loadSetup();loadConnection();loadReceiverAttempts();});
$('refresh').addEventListener('click',function(){health();signals();loadConnection();loadReceiverAttempts();});
$('lock').addEventListener('click',function(){token='';setupConfig=null;show('setupControls',false);show('accountBox',false);setText('setupTemplate','');setText('exchange','Not connected');setText('positionValue','Unavailable');setText('positionHint','Exchange connection required');setText('setupMessage','Operator authentication required. Private webhook URL is not shown publicly.');setText('connectionMessage','TradingView webhook status: locked.');setText('receiverMessage','Receiver diagnostics: locked.');setText('signals','Locked');setText('signalsHint','Private history');setText('message','Signal history locked.');$('rows').replaceChildren();show('tableBox',false);show('login',true);show('lock',false);});
$('copyWebhook').addEventListener('click',function(){copySetup('webhook_url');});
$('copyTemplate').addEventListener('click',function(){copySetup('luxalgo_alert_message');});
$('safeTest').addEventListener('click',runSafeTest);
$('probeBinance').addEventListener('click',diagnoseBinance);
$('readBinance').addEventListener('click',readBinanceAccount);
health();setInterval(function(){health();signals();loadConnection();loadReceiverAttempts();},15000);
})();
</script></body></html>"""
