"""One-screen multi-coin DRY_RUN operator terminal, without additional trading features."""
LITE_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark">
<title>LUX DIRECT BOT · USDT Futures confirmations</title>
<style>
:root{color-scheme:dark;font-family:system-ui,-apple-system,Segoe UI,sans-serif}
*{box-sizing:border-box}body{margin:0;background:#0b1018;color:#e9f1fd}
main{margin:6vh auto;padding:20px;max-width:1100px}
section{background:#131e2d;border:1px solid #28394d;border-radius:16px;padding:25px;margin-top:17px}
h1{font-size:22px;margin:0}h2{font-size:15px;color:#9dafc2;margin:6px 0 0;font-weight:500}
p{line-height:1.5;color:#a6b7c9;font-size:13px}#mode{display:inline-block;color:#f7d695;font-size:12px;background:#302819;padding:7px 12px;border-radius:999px;margin-top:14px;font-weight:800}
.row{display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap}
.state{font-size:20px;font-weight:750}.warn{border-left:4px solid #e5af69}
button{background:#1f6d60;border:1px solid #56bc9d;color:white;border-radius:12px;font-weight:750;padding:14px 26px;cursor:pointer}
button:disabled{opacity:.4;cursor:default}button.off{background:#713c4a;border-color:#ba677a}
input{background:#090f19;border:1px solid #486078;border-radius:10px;padding:13px;color:white;width:100%;max-width:360px}
small{color:#b9c6d8} .buy{color:#67e0b4}.sell{color:#ff9696}
.footer{margin-top:15px;color:#748ba2;font-size:11px}
.records{max-width:100%;overflow-x:auto}
table{border-collapse:collapse;width:100%;min-width:580px;font-size:12px}
th,td{text-align:left;border-bottom:1px solid #29394b;padding:8px 7px;white-space:nowrap}
th{color:#a9bad0;font-weight:600}
.reference-note{font-size:12px;color:#b9c6d8}
@media(max-width:450px){main{padding:12px;margin:3vh auto}section{padding:18px}button{width:100%}}
</style></head><body><main>
<h1>LUX DIRECT BOT</h1><h2>Binance USDT-M Futures · 5-minute LuxAlgo confirmations</h2>
<div id="mode">DRY RUN · LIVE ORDERS DISABLED</div>
<section class="warn"><strong>Real-money orders remain OFF</strong>
<p id="binanceSummary">Binance account readback not checked yet. Sign in to view read-only balance and positions. The ON/OFF button controls dry-run signals only.</p></section>
<section><div class="row"><div><small>Bot operator</small><div class="state" id="state">LOCKED</div></div><button id="switch" disabled>BOT OFF</button></div>
<p id="notice">Enter the operator token below to control signal processing. No order can be sent to Binance from this build.</p>
<p id="readiness">Live execution: LOCKED until verified TradingView delivery, Binance connection, persistent records, risk settings and protective orders.</p>
<form id="auth"><input id="token" type="password" placeholder="Render operator token" required autocomplete="off" aria-label="Operator token"><button type="submit" style="margin-top:10px">Unlock</button></form>
<button id="lock" class="off" style="display:none;margin-top:12px">Lock terminal</button></section>
<section><small>Most recent webhook signal</small><div class="state" id="last">Not yet verified</div><p id="summary">No authenticated signal records loaded.</p>
<p class="reference-note" id="latestLevels">LuxAlgo reference entry / SL1 / TP1: not supplied.</p></section>
<section><small>Recent signals</small><p class="reference-note">Read only · entry is chart reference, not Binance fill · unverified prices display —</p>
<div class="records"><table aria-label="Recent LuxAlgo alerts, not executed exchange trades">
<thead><tr><th>Time (IST)</th><th>Received symbol</th><th>Original BUY / SELL</th><th>Entry price (signal)</th><th>Unrealized P&amp;L</th><th>SL1</th><th>TP1</th><th>Status</th></tr></thead><tbody id="recentRows"></tbody></table></div>
<p class="reference-note" id="historyInfo">Unlock to view signal history.</p></section>
<section><small>Binance positions · read only</small><p class="reference-note">Actual exchange entry and unrealized P&amp;L (USDT). These positions are not attributed to received signals.</p><div class="records"><table><thead><tr><th>Symbol</th><th>Position side</th><th>Amount</th><th>Entry price (fill)</th><th>Mark price</th><th>Unrealized P&amp;L (USDT)</th></tr></thead><tbody id="positionRows"></tbody></table></div><p id="positionInfo">Unlock to read positions.</p></section>
<div class="footer">No extra indicators · No independent AI trading decisions · Live order pathway locked</div>
</main><script>
(function(){
'use strict';
var secret=''; var $=function(id){return document.getElementById(id);};
function write(id,value){$(id).textContent=value;}
async function checkBinanceReadOnly(){
  var currentSecret=secret;
  $('positionRows').replaceChildren();
  write('positionInfo','Checking exchange positions…');
  try{
    var account=await call('/binance/account');
    if(!secret || secret!==currentSecret)return;
    if(account.status==='CONNECTED_READ_ONLY'&&Array.isArray(account.positions)){
      for(var p of account.positions){
        var tr=document.createElement('tr');
        for(var value of [p.symbol,p.position_side,p.amount,p.entry_price,p.mark_price,p.unrealized_pnl]){
          var td=document.createElement('td');td.textContent=fmtPrice(value);tr.appendChild(td);
        }
        $('positionRows').appendChild(tr);
      }
      write('positionInfo',account.positions.length?'Exchange snapshot: '+new Date().toLocaleTimeString('en-IN',{timeZone:'Asia/Kolkata'})+' IST · Refreshes every 15 seconds.':'No open exchange positions · Unrealized P&L: N/A.');
      write('binanceSummary','Binance USD-M: CONNECTED READ-ONLY · Available USDT: '+
        String(account.usdt_available??'unavailable')+' · Open positions: '+
        String(account.positions.length)+' · Real-money orders OFF.');
    }else{
      write('positionInfo','Position data unavailable · P&L: N/A.');
      write('binanceSummary','Binance USD-M: '+String(account.status||'UNVERIFIED')+
        ' · Signed account readback not verified · Orders OFF.');
    }
  }catch(err){
    if(!secret || secret!==currentSecret)return;
    write('positionInfo','Position data unavailable · P&L: N/A.');
    write('binanceSummary','Binance account readback unavailable · Orders OFF. '+err.message);
  }
}
function fmtPrice(v){return v===null||v===undefined||v===''?'—':String(v);}
function fmtTime(v){var d=new Date(Number(v)*1000);return Number.isFinite(d.getTime())?
  d.toLocaleTimeString('en-IN',{timeZone:'Asia/Kolkata',hour:'2-digit',minute:'2-digit'}):'—';}
function showRecent(records){
  $('recentRows').replaceChildren();
  var limited=records.slice(0,8);
  for(var item of limited){
    var tr=document.createElement('tr');
    var fields=[fmtTime(item.at),item.symbol,item.side,
      fmtPrice(item.entry_reference),"N/A — no verified bot fill",fmtPrice(item.stop_reference),
      fmtPrice(item.take_profit_reference),item.status];
    for(var v of fields){var td=document.createElement('td');td.textContent=String(v);tr.appendChild(td);}
    $('recentRows').appendChild(tr);
  }
  if(limited.length) {
    var x=limited[0];
    write('latestLevels','LuxAlgo signal reference · Entry: '+fmtPrice(x.entry_reference)+
      ' · SL1: '+fmtPrice(x.stop_reference)+' · TP1: '+fmtPrice(x.take_profit_reference)+
      ' · Levels: '+(x.levels_status||'NOT_PROVIDED')+
      '. No exchange fill verified.');
  }else{
    write('latestLevels','LuxAlgo reference entry / SL1 / TP1: not supplied.');
  }
  write('historyInfo',limited.length+' recent webhook record(s) · No exchange trades are recorded by this terminal.');
}
async function call(path,body){
  var options={cache:'no-store',headers:{'X-Admin-Token':secret}};
  if(body!==undefined){options.method='POST';options.headers['Content-Type']='application/json';options.body=JSON.stringify(body);}
  var res=await fetch(path,options);
  if(res.status===403)throw new Error('Invalid operator token');
  if(!res.ok)throw new Error('Server error '+res.status);
  return res.json();
}
async function refresh(){
  if(!secret)return;
  try{
    var results=await Promise.all([call('/operator/status'),call('/connection/status'),call('/operator/readiness'),call('/signals')]);
    var op=results[0],conn=results[1],readiness=results[2];
    if(!Array.isArray(results[3]))throw new Error('Signal history unavailable');
    showRecent(results[3]);
    await checkBinanceReadOnly();
    write('readiness',readiness.ready?'Live readiness: VERIFIED':'Live readiness: BLOCKED ('+readiness.blockers.length+' outstanding checks). BOT ON remains dry-run only.');
    write('state',op.enabled?'ON (DRY RUN)':'OFF');
    write('switch',op.enabled?'BOT OFF':'BOT ON');
    $('switch').classList.toggle('off',op.enabled);
    $('switch').disabled=false;
    if(conn.latest){
      write('last',conn.latest.side+' · '+conn.latest.symbol);
      $('last').className='state '+(conn.latest.side==='BUY'?'buy':'sell');
      write('summary','Timeframe '+conn.latest.timeframe+'m · '+conn.latest.status+' · Received webhooks: '+conn.records+'. TradingView sender not independently verified.');
    }else{write('last','No webhook signals received');write('summary','Waiting for a genuine LuxAlgo confirmation on a configured 5-minute pair.');}
  }catch(e){write('notice','Cannot verify state: '+e.message);$('switch').disabled=true;}
}
$('auth').addEventListener('submit',async function(e){
  e.preventDefault();secret=$('token').value.trim();$('token').value='';
  try{await call('/operator/status');$('auth').style.display='none';$('lock').style.display='block';write('notice','Authorized. Bot switch is DRY RUN ONLY.');await refresh();}
  catch(e){secret='';write('notice',e.message);}
});
$('switch').addEventListener('click',async function(){
  if(!secret)return;
  $('switch').disabled=true;
  try{
    var state=await call('/operator/status');
    var next=await call('/operator/switch',{enabled:!state.enabled});
    write('notice',next.enabled?'BOT ON: accepts signals for dry-run only. No Binance orders.':'BOT OFF: new signals recorded as paused. No Binance orders.');
  }catch(e){write('notice','Switch failed: '+e.message);}
  await refresh();
});
$('lock').addEventListener('click',function(){
  secret='';$('switch').disabled=true;$('auth').style.display='block';$('lock').style.display='none';
  write('state','LOCKED');write('last','Locked');write('notice','Operator session locked.');write('summary','Private records hidden.');
  $('positionRows').replaceChildren();write('positionInfo','Unlock to read positions.');
  $('recentRows').replaceChildren();write('historyInfo','Unlock to view signal history.');
  write('latestLevels','LuxAlgo reference entry / SL1 / TP1: private until unlocked.');
  write('binanceSummary','Binance account balance hidden until operator unlock. Real-money orders OFF.');
});
setInterval(refresh,15000);
})();
</script></body></html>"""
