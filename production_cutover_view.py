"""Production monitor overlay for the original LuxAlgo PRO terminal.

Transforms only the authenticated presentation. Retains existing page design
and signal/radar tooling; hides archived Testnet controls on main view.
Never creates a real-money order sender or changes exchange state.
"""
import re

PRODUCTION_CARDS=r'''<section class="panel" id="productionCardsPanel" aria-label="Gate.io live futures position cards">
<div class="v4-head"><div><strong>LUXALGO PRO · LIVE FUTURES POSITION CARDS</strong>
<p class="light">Authenticated Gate.io positions · Click a token to expand its TradingView chart</p></div></div>
<div class="v4-tabs"><strong id="productionTabCount">Checking production exchange positions…</strong></div>
<div style="display:flex;align-items:center;justify-content:space-between;gap:8px;padding:12px 18px 0;flex-wrap:wrap">
<strong>GATE.IO PRODUCTION · POSITION MONITOR</strong>
<button class="btn gloss-action" type="button" id="productionCardsRefresh">↻ Refresh Live Positions</button></div>
<p class="light" id="productionCardsStatus" role="status" style="padding:6px 18px">Production position readback pending.</p>
<div id="productionPositionCards" class="production-position-cards" aria-live="polite">
<div class="v4-prompt">Loading authenticated production positions…</div></div>
</section>
<script>(function(){'use strict';
const cards=document.getElementById('productionPositionCards'),
  status=document.getElementById('productionCardsStatus'),
  count=document.getElementById('productionTabCount'),btn=document.getElementById('productionCardsRefresh');
if(!cards||!status||!count||!btn)return;
const CARD_RE=/^[A-Z0-9]{2,28}_USDT$/;
function node(tag,cls,value){
 const n=document.createElement(tag);if(cls)n.className=cls;
 if(value!==undefined)n.textContent=value;return n;
}
function safe(v){return v===null||v===undefined||v===''?'—':String(v);}
function field(grid,label,key){
 const wrap=node('div','v4-number'),name=node('div','v4-number-label',label);
 const value=node('div','v4-number-value','—');value.dataset.productionField=key;
 wrap.append(name,value);grid.appendChild(wrap);
}
function makeCard(p,key){
 const card=node('article','v4-trade production-position-card');
 card.dataset.productionKey=key;
 const top=node('div','v4-trade-top');
 const title=node('button','v4-token production-token-toggle',p.contract.replace('_',''));
 title.type='button';title.setAttribute('aria-expanded','false');
 title.title='Show or hide this position’s TradingView chart below the card';
 const tag=node('span','v4-tag',safe(p.side));top.append(title,tag);card.appendChild(top);
 const grid=node('div','v4-numbers');
 for(const [key,label] of [['pnl','EXCHANGE UNREALIZED P&L (USDT)'],
 ['contracts','SIZE (GATE CONTRACTS)'],['entry','ENTRY PRICE'],
 ['mark','GATE MARK PRICE'],['sl','STOP LOSS'],['tp','TAKE PROFIT']]){
   field(grid,label,key);
 }
 card.appendChild(grid);
 card.appendChild(node('p','v45-refresh-state'));
 const controls=node('div','v4-controls');
 for(const action of ['Market Close — LOCKED','Close — LOCKED','Reverse — LOCKED']){
   const b=node('button','btn gloss-safe-disabled',action);b.type='button';b.disabled=true;
   b.title='Manual live trading order submission is not certified; use Gate.io to close an open position';
   controls.appendChild(b);
 }
 const manage=node('a','btn production-exchange-manage','Manage / Close on Gate.io ↗');
 manage.href='https://www.gate.com/futures/USDT/'+encodeURIComponent(p.contract);
 manage.target='_blank';manage.rel='noopener noreferrer';
 manage.title='Open Gate.io to manage this existing position; this does not send an API order';
 controls.appendChild(manage);
 card.appendChild(controls);
 const chart=node('div','production-chart');chart.hidden=true;card.appendChild(chart);
 title.addEventListener('click',()=>{
   const opening=chart.hidden;
   // Only one live chart stays open, without removing any position cards.
   cards.querySelectorAll('.production-chart').forEach(panel=>{
      if(panel!==chart){panel.hidden=true;const previous=panel.parentElement.querySelector('.production-token-toggle');
       if(previous)previous.setAttribute('aria-expanded','false');}
   });
   chart.hidden=!opening;
   title.setAttribute('aria-expanded',String(opening));
   if(opening&&!chart.firstChild)renderChart(chart,p.contract);
 });
 return card;
}
function renderChart(mount,contract){
 // A live TradingView public market chart is not the user's private LuxAlgo indicator.
 if(!CARD_RE.test(contract)){
   mount.appendChild(node('p','light','TradingView symbol cannot be verified.'));
   return;
 }
 const pair=contract.replace('_','');
 const market='GATEIO:'+pair+'.P';
 const section=node('section','v45-indicator');
 const header=node('div','v45-indicator-head');
 header.appendChild(node('strong','',pair+' · Gate.io Perpetual · TradingView 5m'));
 const link=node('a','production-tv-external','Open in TradingView ↗');
 link.href='https://www.tradingview.com/chart/?symbol='+encodeURIComponent(market);
 link.rel='noopener noreferrer';link.target='_blank';header.appendChild(link);
 section.appendChild(header);
 const widgetWrap=node('div','production-tv-widget');
 const widget=document.createElement('tv-advanced-chart');
 widget.setAttribute('symbol',market);
 widget.setAttribute('interval','5');
 widget.setAttribute('theme','dark');
 widget.setAttribute('timezone','Asia/Kolkata');
 widget.setAttribute('chart-type','candles');
 widgetWrap.appendChild(widget);section.appendChild(widgetWrap);
 section.appendChild(node('p','v45-indicator-foot',
  'TradingView public exchange chart · Paid LuxAlgo overlays require opening your own TradingView workspace.'));
 mount.appendChild(section);
}
function updateCard(card,p,reconciled){
 const protectedByExchange=reconciled&&p.protection_status==='EXCHANGE_VERIFIED';
 const values={pnl:p.unrealized_pnl,contracts:p.size_contracts,entry:p.entry_price,
   mark:p.mark_price,sl:protectedByExchange?p.stop_loss:'NOT VERIFIED',
   tp:protectedByExchange?p.take_profit:'NOT VERIFIED'};
 for(const item of card.querySelectorAll('[data-production-field]')){
   item.textContent=safe(values[item.dataset.productionField]);
 }
 const state=card.querySelector('.v45-refresh-state');
 state.textContent='Execution: '+safe(p.execution_status)+' · SL/TP: '+safe(p.protection_status)+
   ' · '+safe(p.protection_reason)+' · Live order controls locked; exchange management link available';
}
let busy=false;
async function refresh(){
 if(busy)return;busy=true;btn.disabled=true;
 try{
  let positions=[],reconciled=false,mode='READBACK UNVERIFIED';
  try{
   const res=await fetch('/terminal/live-execution/snapshot',{cache:'no-store',credentials:'same-origin'});
   const data=await res.json();
   if(!res.ok||!Array.isArray(data.positions))throw Error('Execution protection readback unavailable');
   positions=data.positions;reconciled=true;mode=String(data.status||'READ_ONLY');
  }catch(reconcileError){
   const res=await fetch('/terminal/live-preparation/status',{cache:'no-store',credentials:'same-origin'});
   const data=await res.json();
   if(!res.ok||!data.positions_verified||!Array.isArray(data.production_positions))
      throw Error('Authenticated position inventory unavailable');
   positions=data.production_positions.map(p=>({...p,protection_status:'NOT_VERIFIED',
       execution_status:'UNKNOWN',protection_reason:'TRIGGER_READBACK_UNAVAILABLE'}));
   mode='POSITION GET ONLY · PROTECTION UNKNOWN';
  }
  // Remove the initial loading/empty-state placeholder only AFTER a verified
  // exchange response. Otherwise CSS grid treats it as a live position slot
  // and pushes the first real card to the second column.
  for(const placeholder of cards.querySelectorAll('.v4-prompt'))placeholder.remove();
  const old=new Map([...cards.querySelectorAll('article[data-production-key]')]
     .map(card=>[card.dataset.productionKey,card]));
  const seen=new Set();
  for(const p of positions){
    if(!CARD_RE.test(String(p.contract||''))||!['LONG','SHORT'].includes(p.side))continue;
    const key=p.contract+'|'+p.side;
    if(seen.has(key))continue; // Never create two UI cards for the same exchange exposure.
    seen.add(key);
    let card=old.get(key);
    if(!card){card=makeCard(p,key);cards.appendChild(card);}
    updateCard(card,p,reconciled);
  }
  for(const [key,card] of old){if(!seen.has(key))card.remove();}
  count.textContent='Gate.io Production Positions ('+seen.size+')';
  cards.classList.toggle('one-position',seen.size===1);
  if(!seen.size){
    cards.replaceChildren(node('div','v4-prompt',positions.length?
      'Some exchange positions have invalid contract/side metadata; details cannot be shown.':
      'No open production positions reported in current exchange readback.'));
  }
  // Existing card nodes and web components survive 30-second refreshes.
  status.textContent=mode+' · '+new Date().toLocaleTimeString('en-IN',{timeZone:'Asia/Kolkata'})+
    ' IST · Trading is not enabled by chart display';
 }catch(e){
   status.textContent='PRODUCTION READBACK UNAVAILABLE — exposure UNKNOWN, protection UNKNOWN; check Gate.io';
   count.textContent='Exchange positions not verified';
 }finally{busy=false;btn.disabled=false;}
}
btn.addEventListener('click',refresh);refresh();
setInterval(()=>{if(!document.hidden)refresh();},30000);
})();</script>'''
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
let busy=false,actualOn=false;
const url='/terminal/production-bot-control';
function display(state){
 const enabled=state&&state.bot_on===true;
 actualOn=enabled;
 label.textContent=enabled?'PRODUCTION BOT · ON':'PRODUCTION BOT · OFF';
 indicator.textContent=enabled?'● BOT ON':'● BOT OFF';
 indicator.className='bot-state-indicator '+(enabled?'on':'off');
 on.setAttribute('aria-pressed',String(enabled));
 off.setAttribute('aria-pressed',String(!enabled));
 on.disabled=enabled;off.disabled=!enabled;
 detail.textContent=enabled?'Automatic entries permitted · Gate.io live Futures':
  'New entries paused · '+String(state?.reason||state?.error||'Unverified safety state').replaceAll('_',' ');
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
body.production-view .production-live-orders-panel{margin-top:12px;padding-bottom:12px}
body.production-view .production-live-orders-panel .v4-head{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
body.production-view .production-orders-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;padding:13px 18px}
body.production-view .production-orders-grid>div{padding:12px;border:1px solid #355a7b;border-radius:10px;background:rgba(13,33,56,.55);min-width:0}
body.production-view .production-orders-grid strong{display:block;font-size:13px;overflow-wrap:anywhere;margin-top:6px}
body.production-view .production-orders-key{display:block;font-size:11px;opacity:.8}
body.production-view #productionOrdersStatus{padding:3px 18px}
@media(max-width:650px){body.production-view .production-orders-grid{grid-template-columns:1fr}}
body.production-view .production-position-cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;padding:12px 16px 17px}
body.production-view .production-position-cards.one-position{grid-template-columns:minmax(0,1fr)}
body.production-view .production-position-cards.one-position .production-position-card{max-width:980px}
body.production-view .production-position-card{border:1px solid #355a7b;background:linear-gradient(145deg,#142d49,#0a1d32);border-radius:12px;padding:14px;min-width:0}
body.production-view .production-position-card .v4-trade-top{display:flex;gap:10px;align-items:center}
body.production-view .production-position-card .v4-numbers{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-top:16px}
body.production-view .production-position-card .v4-controls{display:flex;flex-wrap:wrap;gap:7px;margin-top:12px}
body.production-view .production-position-card .v4-controls button{flex:1}
body.production-view .production-position-card .production-token-toggle{
  border:0;background:transparent;color:#b8edff;font-weight:900;
  padding:4px 1px;cursor:pointer;text-decoration:underline;text-underline-offset:3px
}
body.production-view .production-position-card .production-token-toggle:focus-visible{outline:2px solid #66e6ff;outline-offset:3px}
body.production-view .production-position-card .production-token-toggle[aria-expanded="true"]{color:#78edc7}
body.production-view .production-position-card .production-chart[hidden]{display:none!important}
body.production-view .production-position-card .production-chart{width:100%;margin-top:14px}
body.production-view .production-position-card .production-tv-widget{height:350px;min-height:300px;width:100%}
body.production-view .production-position-card tv-advanced-chart{display:block;width:100%;height:100%}
body.production-view .production-position-card .production-tv-external{color:#bdefff;text-decoration:underline}
body.production-view .production-position-card .production-exchange-manage{display:inline-flex;align-items:center;
  justify-content:center;flex:1;min-height:35px;color:#e5fcff;background:#164e76;
  border:1px solid #4bb8f2;border-radius:7px;text-decoration:none}

@media(max-width:1050px){body.production-view .production-position-cards{grid-template-columns:1fr}}
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
    out=out.replace('<section class="panel" id="v4Workspace"',
                    PRODUCTION_CARDS+'\n<section class="panel" id="v4Workspace"',1)
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

