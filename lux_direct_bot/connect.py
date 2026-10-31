"""Private one-time TradingView to LUX DIRECT BOT setup page.

The private webhook URL is requested only from the authorized /setup endpoint.
No secrets are baked into source or served in the HTML.
"""
CONNECT_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark"><title>LUX DIRECT BOT · Connect TradingView</title>
<style>
:root{color-scheme:dark;font-family:system-ui,-apple-system,Segoe UI,sans-serif}
*{box-sizing:border-box}body{margin:0;background:#0c1220;color:#eef4ff}
main{max-width:660px;padding:22px;margin:5vh auto}
h1{font-size:23px;margin:0}h2{font-size:17px;margin:0 0 12px}p,li{color:#b4c3d7;line-height:1.6}
section{background:#152031;border:1px solid #34455c;border-radius:14px;padding:22px;margin:16px 0}
input{padding:13px;border-radius:9px;border:1px solid #52708c;background:#0b1422;color:#fff;width:100%;max-width:390px}
button{background:#236b5c;color:#fff;border:1px solid #4dbb9e;border-radius:10px;padding:12px 16px;font-weight:700;cursor:pointer;margin:6px 8px 6px 0}
button:disabled{opacity:.4;cursor:default}.hidden{display:none}.muted{font-size:12px;color:#92a8bd}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#0b1421;border:1px solid #384960;border-radius:8px;padding:12px;font-size:12px}
a{color:#7fc5ff} .status{font-size:14px;font-weight:650;margin:12px 0}
@media(max-width:470px){main{padding:12px}section{padding:17px}button{display:block;width:100%}}
</style></head><body><main>
<h1>Connect TradingView to XRP BOT</h1>
<p>Only the setup of the existing BINANCE:XRPUSDT.P · 5-minute LuxAlgo confirmation alert is covered here. This page does not place Binance orders.</p>
<section><h2>1. Unlock private webhook settings</h2>
<form id="login" autocomplete="off"><input id="key" type="password" placeholder="Render ADMIN_TOKEN" aria-label="Operator access token" required>
<button type="submit">Unlock setup</button></form>
<p class="muted">Use your secret from Render only in this secure page. Never paste it into chat.</p><p id="authMessage" role="status">Not authenticated.</p></section>
<section id="steps" class="hidden"><h2>2. Configure LuxAlgo in TradingView</h2>
<ol><li>Open the TradingView 5-minute chart <strong>BINANCE:XRPUSDT.P</strong> and add your paid <strong>LuxAlgo Signals &amp; Overlays</strong>.</li>
<li>In the indicator settings, scroll to <strong>ANY ALERT() FUNCTION CALL CONDITIONS</strong> and enable <strong>Bullish Confirmation</strong> and <strong>Bearish Confirmation</strong> only.</li>
<li>Paste the JSON from the button below into the <strong>indicator's Any alert() function call message</strong> field, then apply the indicator settings.</li>
<li>Create a TradingView alert with condition <strong>LuxAlgo Signals &amp; Overlays → Any alert() function call</strong>. Choose open-ended expiration if offered.</li>
<li>Under alert notifications, turn on <strong>Webhook URL</strong>, paste the private URL using the button below, and save the alert.</li>
</ol>
<button id="copyJson" type="button">Copy LuxAlgo message JSON</button>
<button id="copyUrl" type="button">Copy private webhook URL</button>
<details><summary>Preview LuxAlgo JSON template (not the private URL)</summary><pre id="jsonPreview"></pre></details>
<p id="copyMessage" role="status">The webhook URL is never printed in the page or added to browser history.</p>
<p class="muted">TradingView requires 2-factor authentication for webhooks. Its alert log can display the delivery status.</p></section>
<section id="verification" class="hidden"><h2>3. Verify a genuine confirmation</h2>
<p>First, test whether this browser can reach the configured private webhook URL. This is a technical route check only: no LuxAlgo event, signal record or exchange order is created.</p>
<button id="probe" type="button">Test private webhook route</button>
<p class="status" id="probeResult" role="status">Not tested yet.</p>
<p>After TradingView fires a real Bullish or Bearish Confirmation, inspect TradingView's alert log and click Check receiver below. A test probe or an ordinary chart signal does not prove genuine TradingView delivery.</p>
<button id="check" type="button">Check receiver</button>
<p class="status" id="receiver" role="status">No verification performed yet.</p>
<p class="muted">A recorded webhook proves receipt; sender identity still requires matching evidence in TradingView's alert log.</p>
</section><p class="muted"><a href="/terminal">Back to simple ON/OFF terminal</a> · Real-money orders remain disabled.</p>
</main><script>
(function(){
  'use strict';
  var token='', config=null;
  var $=function(x){return document.getElementById(x)};
  function msg(id,text){$(id).textContent=text}
  async function privateGet(path){
    var res=await fetch(path,{cache:'no-store',headers:{'X-Admin-Token':token}});
    if(!res.ok)throw new Error('Private setup authentication failed (HTTP '+res.status+')');
    return res.json()
  }
  $('login').addEventListener('submit',async function(ev){
    ev.preventDefault();
    token=$('key').value.trim();$('key').value='';
    try{
      config=await privateGet('/setup');
      if(!config || !config.webhook_url || !config.luxalgo_alert_message)throw new Error('Server-side webhook setup missing');
      if(config.mode!=='DRY_RUN' || config.real_money_enabled!==false)throw new Error('Unexpected order mode');
      msg('jsonPreview',config.luxalgo_alert_message);
      $('steps').classList.remove('hidden');
      $('verification').classList.remove('hidden');
      $('login').classList.add('hidden');
      msg('authMessage','Authenticated. Private webhook and LuxAlgo message are ready to copy.');
    }catch(err){token='';config=null;msg('authMessage',err.message)}
  });
  async function copy(field){
    if(!config)return;
    try{
      await navigator.clipboard.writeText(config[field]);
      msg('copyMessage',field==='webhook_url'?'Copied private URL. Paste it only into TradingView Webhook URL.':'Copied JSON. Paste into the LuxAlgo indicator custom message.');
    }catch(err){msg('copyMessage','Clipboard permission blocked. Open this page in HTTPS and allow clipboard access.')}
  }
  $('copyJson').addEventListener('click',function(){copy('luxalgo_alert_message')});
  $('copyUrl').addEventListener('click',function(){copy('webhook_url')});
  $('probe').addEventListener('click',async function(){
    if(!config || !config.webhook_url){
      msg('probeResult','Unlock setup first.');return;
    }
    $('probe').disabled=true;
    try{
      var response=await fetch(config.webhook_url,{
        method:'POST',cache:'no-store',referrerPolicy:'no-referrer',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({_probe:'CHECK_WEBHOOK_ROUTE_V1'})
      });
      if(!response.ok)throw new Error('Webhook returned HTTP '+response.status);
      var outcome=await response.json();
      if(outcome.result!=='PROBE_OK' || outcome.orders_enabled!==false || outcome.signal_created!==false)
        throw new Error('Unexpected diagnostic reply');
      msg('probeResult','PASS: private webhook URL accepts a safe diagnostic request. No real signal received and no trade placed.');
    }catch(err){
      msg('probeResult','FAILED: '+err.message+'. Check the private URL and Render service, then retry.');
    }finally{$('probe').disabled=false}
  });
  $('check').addEventListener('click',async function(){
    try{
      var r=await Promise.all([privateGet('/connection/status'),privateGet('/connection/attempts')]);
      var status=r[0],attempts=(r[1].attempts||[]);
      var details=attempts.length?' Last receiver outcome: '+attempts[0].result+' ('+attempts[0].reason+').':' No recorded receiver attempts on this instance.';
      if(status.records===0)msg('receiver','NO WEBHOOK RECORDS. TradingView delivery has not been verified.'+details);
      else msg('receiver','Receiver has '+status.records+' accepted webhook record(s). Latest: '+status.latest.symbol+' '+status.latest.side+' · '+status.latest.timeframe+'m · '+status.latest.status+'. Match it to TradingView alert log.'+details);
    }catch(err){msg('receiver','Unable to check receiver: '+err.message)}
  });
})();
</script></body></html>"""
