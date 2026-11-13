"""Production monitor overlay for the original LuxAlgo PRO terminal.

Transforms only the authenticated presentation. Retains existing page design
and signal/radar tooling; hides archived Testnet controls on main view.
Never creates a real-money order sender or changes exchange state.
"""
import re

PRODUCTION_BANNER='''<section class="live-ops-banner" aria-label="Production trading preparation">
<div><strong>GATE.IO FUTURES · PRODUCTION MONITOR</strong><p>Live market feeds and read-only exchange reconciliation run independently of BOT ON/OFF. Position protection must be exchange verified.</p></div>
<span class="live-ops-state">LIVE MARKET FEED</span></section>'''

PRODUCTION_CONTROL='''<section class="bot-control-panel" aria-label="Production bot control">
<div><div class="bot-status-line"><strong id="productionBotLabel">PRODUCTION BOT · OFF</strong>
<span id="productionBotIndicator" class="bot-state-indicator off" role="status" aria-live="polite">● BOT OFF</span></div>
<p id="productionBotDetail" role="status" aria-live="polite">Checking production execution safety state…</p></div>
<div class="bot-control-actions">
<button class="btn" id="productionBotOn" type="button" aria-pressed="false">▶ BOT ON</button>
<button class="btn" id="productionBotOff" type="button" aria-pressed="true">■ BOT OFF</button>
</div>
<div class="gate-unlock-row">
<div class="gate-unlock-head"><strong>UNLOCK LIVE TRADING</strong>
<p class="light">Do these in order: <b>1 CLEAR HALT</b>, then <b>2 CERTIFY</b>, then <b>3 BOT ON</b>. Each step only works once the one before it is done.</p></div>
<div class="gate-unlock-actions">
<button class="btn gate-btn" id="productionHaltClear" type="button">1 · CLEAR HALT</button>
<button class="btn gate-btn" id="productionHaltDiagnose" type="button" aria-expanded="false" aria-controls="productionHaltDiagnoseOut">WHY IS IT BLOCKED?</button>
<button class="btn gate-btn" id="productionCertifyOpen" type="button" aria-expanded="false" aria-controls="productionCertifyForm">2 · CERTIFY</button>
</div>
<div class="gate-emergency-row">
<button class="btn gate-btn gate-btn-emergency" id="productionEmergencyClear" type="button" aria-describedby="productionEmergencyHint">&#9888; EMERGENCY: CLEAR HALT NOW</button>
<p class="light gate-emergency-hint" id="productionEmergencyHint">One press. It first re-reads your Gate.io account and shows you the exact blocking reason, then clears the HALT if the account is genuinely flat. It never clears while a position, an open order or a resting trigger exists on Gate.io.</p>
</div>
<div class="gate-certify-form" id="productionEmergencyOut" hidden>
<pre id="productionEmergencyBody" style="white-space:pre-wrap;word-break:break-word;font-size:12px;margin:0"></pre>
<button class="btn gate-btn gate-btn-emergency" id="productionEmergencyConfirm" type="button" hidden>YES &mdash; CLEAR IT NOW</button>
<button class="btn gate-btn gate-btn-quiet" id="productionEmergencyCancel" type="button">Cancel</button>
</div>
<div class="gate-certify-form" id="productionHaltDiagnoseOut" hidden>
<p class="light">Read-only. This asks the server exactly what is stopping CLEAR HALT. It never changes anything on Gate.io or in the bot.</p>
<pre id="productionHaltDiagnoseBody" style="white-space:pre-wrap;word-break:break-word;font-size:12px;margin:0"></pre>
<button class="btn" id="productionHaltRelease" type="button" hidden>RELEASE STALE RECORD &amp; CLEAR HALT</button>
</div>
<div class="gate-certify-form" id="productionCertifyForm" hidden>
<p class="light">CERTIFY records your own sign-off that you have checked a real acceptance trade. It does not place an order and it does not clear the HALT.</p>
<label for="productionEvidenceSha"><strong>Evidence reference</strong>
<span class="light">A short label pointing at your proof — the acceptance trade you checked. At least 8 characters: letters, numbers, dot, underscore, colon or dash.</span></label>
<input id="productionEvidenceSha" type="text" value="acceptance-2026-11-11-run1" maxlength="128" autocomplete="off" spellcheck="false">
<label for="productionApprovalId"><strong>Your approval ID</strong>
<span class="light">Your own sign-off label, for example your initials and the date. At least 4 characters.</span></label>
<input id="productionApprovalId" type="text" value="approval-owner-1111" maxlength="128" autocomplete="off" spellcheck="false">
<label for="productionValidHours"><strong>Valid for how many hours</strong>
<span class="light">How long this permission lasts, from 1 to 168 hours (168 = one week). When it runs out, live trading stops by itself.</span></label>
<input id="productionValidHours" type="number" value="24" min="1" max="168" step="1">
<div class="gate-certify-submit">
<button class="btn gate-btn" id="productionCertifySubmit" type="button">RECORD CERTIFICATION</button>
<button class="btn gate-btn gate-btn-quiet" id="productionCertifyCancel" type="button">Cancel</button>
</div>
</div>
<p class="gate-result" id="productionGateResult" role="status" aria-live="polite"></p>
</div></section>'''

PRODUCTION_ORDERS=r'''<section class="panel production-live-orders-panel" id="productionOrdersPanel" aria-label="Current live Futures orders read only">
<div class="v4-head"><div><strong>LIVE FUTURES ORDERS</strong>
<p class="light">Current authenticated exchange inventory only · No historical test-order details · Read only</p></div>
<button class="btn gloss-action" type="button" id="productionOrdersRefresh">↻ Refresh Orders</button></div>
<div class="production-orders-grid">
<div><span class="production-orders-key">OPEN EXCHANGE ORDERS</span><strong id="productionOpenOrders">CHECKING…</strong></div>
<div><span class="production-orders-key">PROTECTIVE TRIGGERS</span><strong id="productionOpenTriggers">CHECKING…</strong></div>
<div><span class="production-orders-key">OPEN POSITIONS</span><strong id="productionOpenPositions">CHECKING…</strong></div>
</div>
<p class="light" id="productionOrdersStatus" role="status" aria-live="polite">Waiting for authenticated live exchange readback. Orders are never hidden if they exist.</p>
</section>
<script>(function(){'use strict';
const orders=document.getElementById('productionOpenOrders'),
 triggers=document.getElementById('productionOpenTriggers'),
 positions=document.getElementById('productionOpenPositions'),
 status=document.getElementById('productionOrdersStatus'),
 refresh=document.getElementById('productionOrdersRefresh');
if(!orders||!triggers||!positions||!status||!refresh)return;
const validCount=value=>typeof value==='number'&&Number.isSafeInteger(value)&&value>=0;
let busy=false;
async function update(){
 if(busy)return;busy=true;refresh.disabled=true;
 const controller=new AbortController();
 const timer=setTimeout(()=>controller.abort(),12000);
 try{
  const response=await fetch('/terminal/live-execution/snapshot',{
    credentials:'same-origin',cache:'no-store',signal:controller.signal
  });
  if(!response.ok)throw Error('Exchange readback unavailable');
  const snapshot=await response.json();
  const o=snapshot.exchange_open_orders,t=snapshot.exchange_open_triggers,p=snapshot.open_positions;
  if(!Array.isArray(snapshot.positions)||!validCount(o)||!validCount(t)||
     !validCount(p)||p!==snapshot.positions.length)
    throw Error('Incomplete authenticated order inventory');
  orders.textContent=o===0?'0 · EMPTY':String(o)+' · OPEN';
  triggers.textContent=t===0?'0 · EMPTY':String(t)+' · OPEN';
  positions.textContent=p===0?'0 · EMPTY':String(p)+' · OPEN';
  const updated=typeof snapshot.fetched_at==='number'?
    new Date(snapshot.fetched_at*1000):new Date(snapshot.fetched_at);
  const timestamp=Number.isFinite(updated.getTime())?
   updated.toLocaleString('en-IN',{timeZone:'Asia/Kolkata'})+' IST':'time unknown';
  status.textContent=(o===0&&t===0&&p===0?
    'Exchange confirms no open orders, protective triggers or positions.':
    'Active exchange exposure detected. Review live positions and orders directly on Gate.io.')+
    ' Readback: '+timestamp+'. Real-money order controls remain subject to production safety gates.';
 }catch(error){
  // Never show stale zeros as current when the authenticated API readback fails.
  orders.textContent='UNVERIFIED';
  triggers.textContent='UNVERIFIED';
  positions.textContent='UNVERIFIED';
  status.textContent='Unable to verify live exchange inventory. This is NOT an empty-order confirmation. Execution remains subject to safety locks.';
 }finally{clearTimeout(timer);busy=false;refresh.disabled=false;}
}
refresh.addEventListener('click',update);update();
setInterval(()=>{if(!document.hidden)update();},60000);
})();</script>'''
# Keep the original separate control panel above Radar, with original two-button
# appearance and both banner/state indicators. Never route it to retired Testnet.
PRODUCTION_BOT_SCRIPT=r'''<script>(function(){'use strict';
const on=document.getElementById('productionBotOn'),
      off=document.getElementById('productionBotOff'),
      label=document.getElementById('productionBotLabel'),
      indicator=document.getElementById('productionBotIndicator'),
      detail=document.getElementById('productionBotDetail');
if(!on||!off||!label||!indicator||!detail)return;
let busy=false,actualOn=false,lastState=null;
const url='/terminal/production-bot-control';
// Plain-words explanation of the stored HALT reason. The server now returns it,
// so the operator can tell a button press from a failed exchange readback.
const HALT_REASON_WORDS={
 OPERATOR_OFF_NO_NEW_ENTRIES:'you pressed BOT OFF',
 OPERATOR_PERSISTENT_HALT:'the emergency halt was pressed',
 MANUAL_EMERGENCY_HALT:'the emergency halt button was pressed',
 UNPROTECTED_OR_UNKNOWN_EXCHANGE_EXPOSURE:'the 60-second exchange check found a position, order or trigger it cannot match',
 UNKNOWN_OR_UNPROTECTED_EXPOSURE:'the exchange check found a position, order or trigger it cannot match',
 EXCHANGE_RECONCILIATION_FAILED:'the exchange could not be read (network, API error or timeout)',
 OPERATOR_RECONCILED_CLEAR:'cleared by the operator'
};
function haltWords(reason){
 const key=String(reason||'').trim().toUpperCase();
 if(!key)return '';
 return HALT_REASON_WORDS[key]||String(reason).replaceAll('_',' ').toLowerCase();
}
function display(state){
 const enabled=state&&state.bot_on===true;
 actualOn=enabled;
 lastState=state||null;
 // Keep the gate panel in step with the real server state. Guarded: the gate
 // elements are wired further down, so this hook may not exist yet.
 if(typeof window.__luxGateSync==='function'){try{window.__luxGateSync(state);}catch(_){}}
 const why=state&&state.halt_reason?haltWords(state.halt_reason):'';
 label.textContent=enabled?'PRODUCTION BOT · ON':'PRODUCTION BOT · OFF';
 indicator.textContent=enabled?'● BOT ON':'● BOT OFF';
 indicator.className='bot-state-indicator '+(enabled?'on':'off');
 on.setAttribute('aria-pressed',String(enabled));
 off.setAttribute('aria-pressed',String(!enabled));
 on.disabled=enabled;off.disabled=!enabled;
 detail.textContent=enabled?'Automatic entries permitted · Gate.io live Futures':
  'New entries paused · '+(why?('HALT because '+why):String(state?.reason||state?.error||'Unverified safety state').replaceAll('_',' '));
}
async function refresh(){
 if(busy)return;
 try{
  const resp=await fetch(url,{cache:'no-store',credentials:'same-origin'});
  const state=await resp.json();
  if(!resp.ok)throw Error(state.error||'Server-side state unavailable');
  display(state);
 }catch(e){
  actualOn=false;on.disabled=true;off.disabled=false;
  label.textContent='PRODUCTION BOT · OFF';
  indicator.textContent='● BOT OFF';
  indicator.className='bot-state-indicator off';
  detail.textContent='Execution state unverified · no new orders';
 }
}
async function update(enabled){
 if(busy)return;
 busy=true;on.disabled=true;off.disabled=true;
 detail.textContent=enabled?'Verifying production safety gates…':'Persisting BOT OFF / emergency HALT…';
 try{
  const csrf=document.querySelector('input[name="csrf_token"]')?.value;
  if(!csrf)throw Error('Session CSRF unavailable');
  const resp=await fetch(url,{method:'POST',credentials:'same-origin',cache:'no-store',
    headers:{'Content-Type':'application/json','X-Terminal-CSRF':csrf},
    body:JSON.stringify({enabled})});
  const state=await resp.json();
  display(state);
  if(!resp.ok)detail.textContent='BOT ON denied · '+String(state.reason||state.error||'Safety gates not passed').replaceAll('_',' ');
 }catch(e){
  label.textContent='PRODUCTION BOT · OFF';
  indicator.textContent='● BOT OFF';
  indicator.className='bot-state-indicator off';
  detail.textContent='Execution request not confirmed · BOT OFF';
 }finally{busy=false;await refresh();}
}
on.addEventListener('click',()=>update(true));
off.addEventListener('click',()=>update(false));
refresh();setInterval(()=>{if(!document.hidden)refresh();},15000);

/* ---- Gate unlock controls: CLEAR HALT and CERTIFY ----------------------
   These call the SAME endpoints with the SAME validation as the API.
   Nothing here weakens a safety check: the server still re-reads the live
   exchange and still refuses on any ambiguity. This only removes the need
   to paste JavaScript into the browser console. */
const haltBtn=document.getElementById('productionHaltClear'),
      certOpen=document.getElementById('productionCertifyOpen'),
      certForm=document.getElementById('productionCertifyForm'),
      certSubmit=document.getElementById('productionCertifySubmit'),
      certCancel=document.getElementById('productionCertifyCancel'),
      evInput=document.getElementById('productionEvidenceSha'),
      apInput=document.getElementById('productionApprovalId'),
      hrInput=document.getElementById('productionValidHours'),
      diagBtn=document.getElementById('productionHaltDiagnose'),
      diagOut=document.getElementById('productionHaltDiagnoseOut'),
      diagBody=document.getElementById('productionHaltDiagnoseBody'),
      releaseBtn=document.getElementById('productionHaltRelease'),
      emgBtn=document.getElementById('productionEmergencyClear'),
      emgOut=document.getElementById('productionEmergencyOut'),
      emgBody=document.getElementById('productionEmergencyBody'),
      emgConfirm=document.getElementById('productionEmergencyConfirm'),
      emgCancel=document.getElementById('productionEmergencyCancel'),
      gateOut=document.getElementById('productionGateResult');
if(!haltBtn||!certOpen||!certForm||!certSubmit||!gateOut)return;

const GATE_WORDS={
 SOURCE_LOCKED:'the code release switch is still off',
 ENVIRONMENT_LOCKED:'a Render environment variable is still missing or not exactly "true"',
 EMERGENCY_HALT:'the emergency HALT is still on',
 ACCEPTANCE_AND_FINAL_AUTHORIZATION_MISSING:'no certification has been recorded yet',
 OPERATOR_OFF:'the bot has not been switched on yet'
};
let lastGateText='',lastGateKind='';
function say(text,kind){
 lastGateText=text;lastGateKind=kind||'';
 gateOut.textContent=text;
 gateOut.className='gate-result'+(kind?' gate-'+kind:'');
}
// Re-render the guidance line from the LIVE state so the panel never tells the
// operator to do a step they already finished, and shouts when a HALT re-latches
// after it was cleared. Driven by STATE TRANSITIONS, not by the last message
// text: CERTIFY overwrites the "HALT CLEARED" line, so matching on text alone
// would silently miss a re-latch (which is exactly the failure we are fixing).
let prevHalted=null,prevArmed=null,refusalUntil=0;
window.__luxGateSync=function(state){
 const gates=Array.isArray(state&&state.safety_gates)?state.safety_gates:[];
 const halted=gates.indexOf('EMERGENCY_HALT')!==-1;
 const needsCert=gates.indexOf('ACCEPTANCE_AND_FINAL_AUTHORIZATION_MISSING')!==-1;
 const armed=state&&state.bot_on===true;
 if(!busy){
  haltBtn.disabled=!halted;
  haltBtn.textContent=halted?'1 \u00b7 CLEAR HALT':'1 \u00b7 HALT CLEARED \u2713';
  certOpen.disabled=!needsCert;
  certOpen.textContent=needsCert?'2 \u00b7 CERTIFY':'2 \u00b7 CERTIFIED \u2713';
 }
 if(busy)return;
 // A refusal the operator just triggered stays on screen for a while.
 if(Date.now()<refusalUntil)return;
 const wasHalted=prevHalted,wasArmed=prevArmed;
 prevHalted=halted;prevArmed=armed;
 if(armed){
  say('LIVE \u00b7 all gates open. New entries are permitted and every trade carries an exchange stop loss and take profit. Certification ends '+
      (state.certified_until?'at '+new Date(state.certified_until).toLocaleString('en-IN',{timeZone:'Asia/Kolkata'})+' IST':'when it expires')+
      '; after that the bot stops opening new trades by itself.','good');
  return;
 }
 // Re-latch: it was running (or the halt had been cleared) and now it is not.
 if(halted&&(wasArmed===true||wasHalted===false)){
  say('HALT RE-LATCHED \u2014 '+(haltWords(state.halt_reason)||'the server re-latched it')+
      '. New entries are blocked. Check Gate.io for an open position, resting order or trigger, then press 1 \u00b7 CLEAR HALT again.','bad');
  return;
 }
 if(halted){
  say('New entries are blocked. The HALT is on because '+(haltWords(state.halt_reason)||'the server has not cleared it')+
      '. Press 1 \u00b7 CLEAR HALT to re-check Gate.io and clear it.','bad');
  return;
 }
 if(needsCert){
  say('The HALT is clear. Next step: press 2 \u00b7 CERTIFY to record your sign-off.','good');
  return;
 }
 say('Certification is recorded and the HALT is clear. Press \u25b6 BOT ON to start live entries.','good');
};
function gateWords(list){
 if(!Array.isArray(list)||!list.length)return 'All gates are open.';
 return 'Still closed: '+list.map(g=>GATE_WORDS[g]||String(g).replaceAll('_',' ').toLowerCase()).join('; ')+'.';
}
function csrfToken(){
 const el=document.querySelector('input[name="csrf_token"]');
 return el?el.value:'';
}
async function postJson(url,body){
 const csrf=csrfToken();
 if(!csrf)throw Error('Your sign-in token is missing. Reload the page and sign in again.');
 const resp=await fetch(url,{method:'POST',credentials:'same-origin',cache:'no-store',
   headers:{'Content-Type':'application/json','X-Terminal-CSRF':csrf},
   body:JSON.stringify(body||{})});
 let data=null;
 try{data=await resp.json();}catch(_){data=null;}
 return {ok:resp.ok,status:resp.status,data:data||{}};
}
function lockButtons(state){
 haltBtn.disabled=state;certSubmit.disabled=state;certOpen.disabled=state;
}
// Prime the gate panel with whatever the BOT ON/OFF poll already knows.
if(lastState)window.__luxGateSync(lastState);

haltBtn.addEventListener('click',async()=>{
 lockButtons(true);
 say('Checking your Gate.io account for open positions, orders and triggers\u2026');
 try{
  const r=await postJson('/api/live/halt-clear',{});
  if(r.status===401){say('Your session expired. Reload the page and sign in again.','bad');return;}
  if(r.status===403){say('The page security token was rejected. Reload the page and try again.','bad');return;}
  if(r.ok&&r.data.halted===false){
   say('HALT CLEARED. Your Gate.io account was re-checked and showed no open positions, no open orders and no protective triggers. Next step: press 2 \u00b7 CERTIFY.','good');
  }else if(r.data&&r.data.cleared===false){
   refusalUntil=Date.now()+45000;
   say('HALT NOT CLEARED \u2014 the server refused. '+(r.data.reason?(' Server said: '+r.data.reason):'')+
       ' Open the HALT DIAGNOSE panel to see exactly what it is blocking on: a Gate.io position, an open order, a resting trigger, or a leftover trade record that was never sent. The HALT stays on until it is clean.','bad');
  }else{
   say('HALT NOT CLEARED \u2014 unexpected reply (HTTP '+r.status+'). '+(r.data.error||'No reason given.')+' The HALT is still on.','bad');
  }
 }catch(e){
  say('Could not reach the server: '+e.message+' The HALT is still on.','bad');
 }finally{
  lockButtons(false);
  try{await refresh();}catch(_){}
 }
});

// Read-only diagnostic: name the exact blocker instead of guessing.
if(diagBtn&&diagOut&&diagBody){
 diagBtn.addEventListener('click',async()=>{
  const opening=diagOut.hidden;
  diagOut.hidden=!opening;
  diagBtn.setAttribute('aria-expanded',String(opening));
  if(!opening)return;
  diagBody.textContent='Checking\u2026';
  if(releaseBtn)releaseBtn.hidden=true;
  try{
   const r=await fetch('/api/live/halt-diagnose',{credentials:'same-origin',headers:{'Accept':'application/json'}});
   const d=await r.json();
   if(!r.ok){diagBody.textContent='Diagnostic unavailable: '+(d.error||('HTTP '+r.status));return;}
   const lines=[];
   lines.push('Gate.io reachable: '+(d.exchange_reachable===true?'YES':d.exchange_reachable===false?'NO \u2014 the account could not be read':'unknown'));
   if(d.exchange_reachable===true){
    lines.push('Open positions on Gate.io: '+d.exchange_positions);
    lines.push('Open orders on Gate.io: '+d.exchange_open_orders);
    lines.push('Open stop/take triggers on Gate.io: '+d.exchange_open_triggers);
   }
   lines.push('Unfinished trade records in the bot: '+d.unresolved_ledger_rows);
   if(d.stale_reservation_rows&&d.stale_reservation_rows.length){
    lines.push('');
    lines.push('LEFTOVER RECORDS THAT WERE NEVER SENT ('+d.stale_reservation_rows.length+'):');
    d.stale_reservation_rows.forEach(x=>lines.push('  '+x.event_id+'  '+x.contract+'  state='+x.state+'  entry_order_id=none'));
   }
   if(d.unrecoverable_rows&&d.unrecoverable_rows.length){
    lines.push('');
    lines.push('RECORDS THAT MAY HAVE REACHED THE EXCHANGE ('+d.unrecoverable_rows.length+') \u2014 these need manual review:');
    d.unrecoverable_rows.forEach(x=>lines.push('  '+x.event_id+'  '+x.contract+'  state='+x.state+'  entry_order_id='+(x.entry_order_id||'none')));
   }
   if(d.blocking_reasons&&d.blocking_reasons.length){
    lines.push('');
    lines.push('BLOCKING: '+d.blocking_reasons.join(', '));
   }
   diagBody.textContent=lines.join('\n');
   if(releaseBtn)releaseBtn.hidden=!(d.can_clear_with_stale_release===true);
  }catch(e){diagBody.textContent='Could not reach the server: '+e.message;}
 });
}
// Explicit, operator-confirmed release of provably-unsent leftover records.
if(releaseBtn){
 releaseBtn.addEventListener('click',async()=>{
  releaseBtn.disabled=true;
  try{
   const r=await postJson('/api/live/halt-clear',{release_stale_reservations:true});
   if(r.ok&&r.data.halted===false){
    say('HALT CLEARED. '+(r.data.note||'')+' Next step: press 2 \u00b7 CERTIFY.','good');
    if(diagOut)diagOut.hidden=true;
    releaseBtn.hidden=true;
   }else{
    say('Still refused. '+(r.data.reason||r.data.error||'No reason given.')+' The HALT is still on.','bad');
   }
  }catch(e){say('Could not reach the server: '+e.message+' The HALT is still on.','bad');}
  finally{releaseBtn.disabled=false;try{await refresh();}catch(_){}}
 });
}

// ---- EMERGENCY: one press, diagnose first, then clear -------------------
// The button never clears blind. Press 1 runs the READ-ONLY diagnostic and
// prints the real blocking reason. Only when the server says the account is
// genuinely flat does the confirm button appear. The server re-checks the
// exchange again on the actual clear, so this cannot be used to bypass it.
function emgLines(d){
 const L=[];
 L.push('Gate.io reachable: '+(d.exchange_reachable===true?'YES':d.exchange_reachable===false?'NO \u2014 the account could not be read':'unknown'));
 if(d.exchange_reachable===true){
  L.push('Open positions on Gate.io: '+d.exchange_positions);
  L.push('Open orders on Gate.io: '+d.exchange_open_orders);
  L.push('Open stop/take triggers on Gate.io: '+d.exchange_open_triggers);
 }
 L.push('Unfinished trade records in the bot: '+d.unresolved_ledger_rows);
 if(d.stale_reservation_rows&&d.stale_reservation_rows.length){
  L.push('');
  L.push('NEVER SENT \u2014 safe to close ('+d.stale_reservation_rows.length+'):');
  d.stale_reservation_rows.forEach(x=>L.push('  '+x.event_id+'  '+x.contract+'  state='+x.state));
 }
 if(d.flat_reconciled_rows&&d.flat_reconciled_rows.length){
  L.push('');
  L.push('FINISHED TRADES \u2014 Gate.io is flat, only the record is open ('+d.flat_reconciled_rows.length+'):');
  d.flat_reconciled_rows.forEach(x=>L.push('  '+x.event_id+'  '+x.contract+'  state='+x.state+'  entry_order_id='+(x.entry_order_id||'none')));
 }
 if(d.unrecoverable_rows&&d.unrecoverable_rows.length){
  L.push('');
  L.push('REAL EXPOSURE OR UNREADABLE \u2014 will NOT be cleared ('+d.unrecoverable_rows.length+'):');
  d.unrecoverable_rows.forEach(x=>L.push('  '+x.event_id+'  '+x.contract+'  state='+x.state));
 }
 if(d.blocking_reasons&&d.blocking_reasons.length){
  L.push('');
  L.push('BLOCKING: '+d.blocking_reasons.join(', '));
 }
 L.push('');
 if(d.can_clear===true){
  L.push('VERDICT: nothing is blocking. The HALT can be cleared now.');
 }else if(d.can_clear_with_flat_release===true){
  L.push('VERDICT: Gate.io is flat. The HALT is held only by the bot\u2019s own finished-trade record(s) above. Safe to clear.');
 }else if(d.can_clear_with_stale_release===true){
  L.push('VERDICT: Gate.io is flat. The HALT is held only by record(s) that were never sent. Safe to clear.');
 }else{
  L.push('VERDICT: NOT safe to clear. Resolve the exposure above on Gate.io first. The HALT stays on.');
 }
 return L;
}
if(emgBtn&&emgOut&&emgBody&&emgConfirm){
 emgBtn.addEventListener('click',async()=>{
  const opening=emgOut.hidden;
  emgOut.hidden=!opening;
  if(!opening)return;
  emgBody.textContent='Re-reading your Gate.io account\u2026';
  emgConfirm.hidden=true;
  emgBtn.disabled=true;
  try{
   const r=await fetch('/api/live/halt-diagnose',{credentials:'same-origin',headers:{'Accept':'application/json'}});
   const d=await r.json();
   if(!r.ok){emgBody.textContent='Diagnostic unavailable: '+(d.error||('HTTP '+r.status))+' The HALT is still on.';return;}
   emgBody.textContent=emgLines(d).join('\n');
   // Only offer the confirm when the server itself says it is safe.
   emgConfirm.hidden=!(d.can_clear===true||d.can_clear_with_flat_release===true||d.can_clear_with_stale_release===true);
  }catch(e){emgBody.textContent='Could not reach the server: '+e.message+' The HALT is still on.';}
  finally{emgBtn.disabled=false;}
 });
 emgConfirm.addEventListener('click',async()=>{
  emgConfirm.disabled=true;emgBtn.disabled=true;
  emgBody.textContent='Clearing\u2026 the server is re-checking Gate.io one more time.';
  try{
   // Both flags: the server still refuses on any real exposure.
   const r=await postJson('/api/live/halt-clear',{release_stale_reservations:true,release_flat_reconciled:true});
   if(r.ok&&r.data.halted===false){
    const cleared=[];
    if(r.data.stale_reservations_released&&r.data.stale_reservations_released.length)
     cleared.push(r.data.stale_reservations_released.length+' never-sent record(s) closed');
    if(r.data.flat_account_reconciled&&r.data.flat_account_reconciled.length)
     cleared.push(r.data.flat_account_reconciled.length+' finished-trade record(s) reconciled');
    emgBody.textContent='HALT CLEARED.\n\nWhat was cleared: '+(cleared.length?cleared.join('; '):'nothing needed closing \u2014 the ledger was already clean')+'.\nGate.io: 0 positions, 0 open orders, 0 resting triggers.\n\nNext step: press 2 \u00b7 CERTIFY, then \u25b6 BOT ON.';
    emgConfirm.hidden=true;
    say('HALT CLEARED via the emergency button. '+(r.data.note||'')+' Next step: press 2 \u00b7 CERTIFY.','good');
   }else{
    emgBody.textContent='STILL REFUSED \u2014 the HALT is on.\n\nServer said: '+(r.data.reason||r.data.error||'no reason given')+'\n\nNothing was changed. Resolve the exposure on Gate.io, then press the emergency button again.';
    say('Emergency clear refused. '+(r.data.reason||r.data.error||'')+' The HALT is still on.','bad');
   }
  }catch(e){emgBody.textContent='Could not reach the server: '+e.message+' The HALT is still on.';}
  finally{emgConfirm.disabled=false;emgBtn.disabled=false;try{await refresh();}catch(_){}}
 });
 if(emgCancel)emgCancel.addEventListener('click',()=>{emgOut.hidden=true;emgConfirm.hidden=true;});
}

certOpen.addEventListener('click',()=>{
 const opening=certForm.hidden;
 certForm.hidden=!opening;
 certOpen.setAttribute('aria-expanded',String(opening));
 if(opening)evInput.focus();
});
certCancel.addEventListener('click',()=>{
 certForm.hidden=true;certOpen.setAttribute('aria-expanded','false');
});

certSubmit.addEventListener('click',async()=>{
 const evidence=(evInput.value||'').trim();
 const approval=(apInput.value||'').trim();
 const hoursRaw=(hrInput.value||'').trim();
 const hours=Number(hoursRaw);
 // Same rules the server enforces, checked here first so the message is clear.
 if(!/^[A-Za-z0-9_.:-]{8,128}$/.test(evidence)){
  say('The evidence reference must be 8 to 128 characters, using only letters, numbers, dot, underscore, colon or dash.','bad');return;
 }
 if(!/^[A-Za-z0-9_.:-]{4,128}$/.test(approval)){
  say('Your approval ID must be 4 to 128 characters, using only letters, numbers, dot, underscore, colon or dash.','bad');return;
 }
 if(!Number.isInteger(hours)||hours<1||hours>168){
  say('The validity window must be a whole number of hours from 1 to 168.','bad');return;
 }
 lockButtons(true);
 say('Recording your certification\u2026');
 try{
  const r=await postJson('/api/live/certify',
    {evidence_sha:evidence,signed_approval_id:approval,valid_hours:hours});
  if(r.status===401){say('Your session expired. Reload the page and sign in again.','bad');return;}
  if(r.status===403){say('The page security token was rejected. Reload the page and try again.','bad');return;}
  if(r.ok&&r.data.certified===true){
   const until=r.data.approved_until?(' It is valid until '+new Date(r.data.approved_until).toLocaleString('en-IN',{timeZone:'Asia/Kolkata'})+' IST.'):'';
   say('CERTIFIED for '+hours+' hour'+(hours===1?'':'s')+'.'+until+' '+gateWords(r.data.safety_gates)+
       (r.data.can_arm?' Next step: press \u25b6 BOT ON.':' Finish the remaining steps first.'),'good');
   certForm.hidden=true;certOpen.setAttribute('aria-expanded','false');
  }else{
   refusalUntil=Date.now()+45000;
   say('CERTIFICATION NOT RECORDED \u2014 the server refused (HTTP '+r.status+'). '+
       (r.data.error||'No reason given.')+' Check the three fields and try again.','bad');
  }
 }catch(e){
  say('Could not reach the server: '+e.message+' Nothing was recorded.','bad');
 }finally{
  lockButtons(false);
  try{await refresh();}catch(_){}
 }
});
})();</script>'''

PRODUCTION_CSS='''
/* Production mode applies to the existing original dashboard only. */
body.production-view #v4Workspace,
body.production-view #testnetReadinessPanel,
body.production-view #gateTestnetPositionPanel,
body.production-view #testnetOrdersPanel,
body.production-view #liveReadinessPanel,
body.production-view #paperClosedHistoryPanel,
body.production-view #v4RecentWrapper,
body.production-view #v4LegacyGateHost,
body.production-view #v4LegacyPaperHost{display:none!important}
/* Restore original stand-alone ON and OFF button panel, above Radar. */
body.production-view .bot-control-panel{display:flex!important;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap}
body.production-view #productionBotOn{color:#eefff5;border:1px solid #6effb6;background:linear-gradient(160deg,#5ff3ae,#16aa68 48%,#086c44 50%,#20c87f 100%)}
body.production-view #productionBotOff{color:#fff0f0;border:1px solid #ff8da3;background:linear-gradient(160deg,#ff8ba4,#de335e 48%,#921f3e 50%,#e84b72 100%)}
body.production-view .bot-control-actions .btn:disabled{opacity:.38;cursor:not-allowed}
/* Gate unlock controls: CLEAR HALT / CERTIFY, beside the BOT ON/OFF pair. */
body.production-view .gate-unlock-row{flex-basis:100%;margin-top:12px;padding-top:12px;border-top:1px solid #2c4a68}
body.production-view .gate-unlock-head strong{display:block;font-size:12px;letter-spacing:.06em;color:#9de7ff}
body.production-view .gate-unlock-head p{margin:4px 0 0;font-size:12px}
body.production-view .gate-unlock-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
body.production-view .gate-btn{color:#eaf7ff;border:1px solid #4bb8f2;background:linear-gradient(160deg,#2a7fb8,#12507c 50%,#0b3a5c 52%,#1d6f9f 100%)}
body.production-view .gate-btn:disabled{opacity:.38;cursor:not-allowed}
body.production-view .gate-btn-quiet{color:#cfe6f5;border-color:#3d5f7d;background:#16324b}
body.production-view .gate-certify-form{margin-top:12px;padding:12px;border:1px solid #355a7b;border-radius:10px;background:rgba(13,33,56,.55);max-width:640px}
body.production-view .gate-certify-form[hidden]{display:none!important}
body.production-view .gate-certify-form label{display:block;margin-top:10px;font-size:12px}
body.production-view .gate-certify-form label span{display:block;font-weight:400;margin-top:2px;font-size:11px;line-height:1.45}
body.production-view .gate-certify-form input{display:block;width:100%;margin-top:5px;padding:8px 10px;border-radius:7px;
  border:1px solid #3d5f7d;background:#0a1d32;color:#eaf7ff;font-size:13px;box-sizing:border-box}
body.production-view .gate-certify-form input:focus{outline:2px solid #66e6ff;outline-offset:1px}
body.production-view .gate-certify-submit{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
body.production-view .gate-result{margin:12px 0 0;padding:10px 12px;border-radius:9px;font-size:12.5px;line-height:1.5;
  border:1px solid #355a7b;background:rgba(13,33,56,.55);color:#cfe6f5}
body.production-view .gate-result:empty{display:none}
body.production-view .gate-result.gate-good{border-color:#2f9e6b;background:rgba(20,80,55,.5);color:#c8ffe4}
body.production-view .gate-result.gate-bad{border-color:#c0455f;background:rgba(90,25,40,.5);color:#ffd7de}
body.production-view .gate-emergency-row{margin-top:14px;padding:12px;border:1px solid #b8455f;border-radius:10px;background:rgba(70,18,30,.42);max-width:640px}
body.production-view .gate-btn-emergency{color:#fff3f5;border:1px solid #ff6b86;background:linear-gradient(160deg,#b32b46,#7d1428 52%,#5c0d1c);font-weight:700}
body.production-view .gate-btn-emergency:disabled{opacity:.38;cursor:not-allowed}
body.production-view .gate-emergency-hint{margin:8px 0 0;font-size:11.5px;line-height:1.5}
body.production-view .production-live-orders-panel{margin-top:12px;padding-bottom:12px}
body.production-view .production-live-orders-panel .v4-head{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
body.production-view .production-orders-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;padding:13px 18px}
body.production-view .production-orders-grid>div{padding:12px;border:1px solid #355a7b;border-radius:10px;background:rgba(13,33,56,.55);min-width:0}
body.production-view .production-orders-grid strong{display:block;font-size:13px;overflow-wrap:anywhere;margin-top:6px}
body.production-view .production-orders-key{display:block;font-size:11px;opacity:.8}
body.production-view #productionOrdersStatus{padding:3px 18px}
@media(max-width:650px){body.production-view .production-orders-grid{grid-template-columns:1fr}}
@media(max-width:550px){body.production-view .production-position-card .v4-numbers{grid-template-columns:repeat(2,minmax(0,1fr))}}
'''

def production_monitor_page(page):
    """Fail closed if expected original dashboard anchors change."""
    if not isinstance(page,str):
        raise ValueError("Invalid dashboard template")
    must_have=('<body><div class="wrap">','<h1>LUXALGO PRO · TESTNET FUTURES TERMINAL</h1>',
               '<section class="live-ops-banner"', '<section class="bot-control-panel"',
               '<section class="panel" id="v4Workspace"', '</style><script type="module"')
    if any(page.count(x)!=1 for x in must_have):
        raise ValueError("Dashboard structure changed; do not silently render partial cutover")
    out=page.replace('</style><script type="module"',
                     PRODUCTION_CSS+'</style><script type="module"',1)
    out=out.replace('<body><div class="wrap">','<body class="production-view"><div class="wrap">',1)
    out=out.replace('<h1>LUXALGO PRO · TESTNET FUTURES TERMINAL</h1>',
                    '<h1>LUXALGO PRO · LIVE FUTURES TERMINAL</h1>',1)
    out,n=re.subn(r'<section class="live-ops-banner"[^>]*>.*?</section>',
                  lambda _:PRODUCTION_BANNER,out,count=1,flags=re.S)
    if n!=1:raise ValueError("Missing operations banner")
    out,n=re.subn(r'<section class="bot-control-panel"[^>]*>.*?</section>',
                  lambda _:PRODUCTION_CONTROL,out,count=1,flags=re.S)
    if n!=1:raise ValueError("Missing bot control banner")
    # Neutral live order inventory replaces the expired BTC test-order panel.
    # This never conceals nonzero exchange orders or affects safety/audit state.
    if out.count(PRODUCTION_CONTROL)!=1:
        raise ValueError("Restored production BOT controls not unique")
    out=out.replace(PRODUCTION_CONTROL,PRODUCTION_CONTROL+'\n'+PRODUCTION_ORDERS,1)
    # Keep the Radar heading unchanged; BOT buttons are above in the old panel.
    if out.count('</body>')!=1:raise ValueError("Dashboard closing body missing")
    out=out.replace('</body>',PRODUCTION_BOT_SCRIPT+'</body>',1)
    # Remove legacy TESTNET panels from the rendered production page entirely.
    # Original PAGE and authenticated /terminal/legacy-testnet remain intact
    # for active exchange-side protection/recovery until positions are flat.
    for tag,anchor in (("section","v4Workspace"),("section","testnetReadinessPanel"),
                       ("details","gateTestnetPositionPanel"),("details","testnetOrdersPanel")):
        pattern=r'<'+tag+r'\b[^>]*\bid="'+anchor+r'"[^>]*>.*?</'+tag+r'>'
        out,removed=re.subn(pattern,"",out,count=1,flags=re.S)
        if removed!=1:
            raise ValueError("Legacy panel missing: "+anchor)
    # Signals are STILL received and recorded, but no production order
    # execution or Testnet card status may be inferred from their receipt.
    out=out.replace('Fresh LuxAlgo signals only, after your saved dashboard reset. LIVE requires an open Gate.io Testnet position with reverified exchange-side TP/SL; CLOSED requires a verified closing journal. Unexecuted or uncertain calls are never labeled LIVE. Real-money trading OFF.',
                    'Fresh LuxAlgo webhook signals only. This production monitor does not place orders; received signals are NOT production exchange fills. Real-money trading remains OFF.',1)
    # Signal receipts are not exchange orders. Label them accurately without
    # deleting source records or invalidating webhook deduplication and audit.
    old_heading="INCOMING LUXALGO ORDERS · WEBHOOK SIGNALS"
    if out.count(old_heading)!=1:
        raise ValueError("Incoming signal label changed unexpectedly")
    out=out.replace(old_heading,"INCOMING LUXALGO SIGNALS · NOT EXCHANGE ORDERS",1)
    # The same signal table remains, but production must never use stale
    # Testnet fill/SL/TP states as evidence of real-money execution.
    old="fetch('/terminal/incoming-signals',{cache:'no-store'})"
    if out.count(old)!=1:
        raise ValueError("Signal monitor script changed unexpectedly")
    out=out.replace(old,"fetch('/terminal/incoming-signals?production=1',{cache:'no-store'})",1)
    return out

