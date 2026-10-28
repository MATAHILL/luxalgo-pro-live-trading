"""Production monitor overlay for the original LuxAlgo PRO terminal.

Transforms only the authenticated presentation. Retains existing page design
and signal/radar tooling; hides archived Testnet controls on main view.
Never creates a real-money order sender or changes exchange state.
"""
import re

PRODUCTION_CARDS=r'''<section class="panel" id="productionCardsPanel" aria-label="Gate.io production futures position cards read-only">
<div class="v4-head"><div><strong>LUXALGO PRO · LIVE FUTURES POSITION CARDS</strong>
<p class="light">Production Gate.io positions only · Verified exchange GET requests · SL/TP coverage NOT YET VERIFIED · Trading OFF</p></div></div>
<div class="v4-tabs"><strong id="productionTabCount">Checking production exchange positions…</strong><span>REAL MONEY EXECUTION DISABLED</span></div>
<div style="display:flex;align-items:center;justify-content:space-between;gap:8px;padding:12px 18px 0;flex-wrap:wrap">
<strong>GATE.IO PRODUCTION · READ-ONLY POSITION CARDS</strong>
<button class="btn gloss-action" type="button" id="productionCardsRefresh">↻ Refresh Live Positions</button></div>
<p class="light" id="productionCardsStatus" role="status" style="padding:6px 18px">Production position readback pending.</p>
<div id="productionPositionCards" class="production-position-cards" role="status" aria-live="polite">
<div class="v4-prompt">Loading authenticated production positions…</div></div>
</section>
<script>(function(){'use strict';
const section=document.getElementById('productionCardsPanel'),cards=document.getElementById('productionPositionCards'),
  status=document.getElementById('productionCardsStatus'),count=document.getElementById('productionTabCount'),
  btn=document.getElementById('productionCardsRefresh');
if(!section||!cards||!status||!count||!btn)return;
function node(tag,cls,value){const n=document.createElement(tag);if(cls)n.className=cls;if(value!==undefined)n.textContent=value;return n;}
function safe(v){return v===null||v===undefined||v===''?'—':String(v);}
function field(container,label,value){const wrap=node('div','v4-number'),title=node('div','v4-number-label',label);
 const val=node('div','v4-number-value',safe(value));wrap.append(title,val);container.appendChild(wrap);}
async function refresh(){
 if(btn.disabled)return;btn.disabled=true;
 try{
  let positions=[],reconciled=false,halted=true,mode='READBACK UNVERIFIED';
  try{
   const res=await fetch('/terminal/live-execution/snapshot',{cache:'no-store',credentials:'same-origin'});
   const data=await res.json();
   if(!res.ok||!Array.isArray(data.positions))throw Error('Execution protection readback unavailable');
   positions=data.positions;reconciled=true;halted=data.halted===true;mode=String(data.status||'READ_ONLY');
  }catch(reconcileError){
   // Keep account positions visible even if trigger-order readback fails.
   // Never claim protective coverage from this fallback.
   const res=await fetch('/terminal/live-preparation/status',{cache:'no-store',credentials:'same-origin'});
   const data=await res.json();
   if(!res.ok||!data.positions_verified||!Array.isArray(data.production_positions))
     throw Error('Authenticated production position inventory unavailable');
   positions=data.production_positions.map(p=>({...p,protection_status:'NOT_VERIFIED',
    execution_status:'UNKNOWN','protection_reason':'TRIGGER_READBACK_UNAVAILABLE'}));
   mode='POSITION GET ONLY · PROTECTION UNKNOWN';
  }
  cards.replaceChildren();count.textContent='Gate.io Production Positions ('+positions.length+')';
  if(!positions.length)cards.appendChild(node('div','v4-prompt','No open production positions reported in current exchange readback.'));
  for(const p of positions){
   const card=node('article','v4-trade production-position-card');
   const top=node('div','v4-trade-top');
   const title=node('strong','v4-token',safe(p.contract).replace('_USDT','USDT'));
   const tag=node('span','v4-tag',safe(p.side));top.append(title,tag);card.appendChild(top);
   const grid=node('div','v4-numbers');
   field(grid,'EXCHANGE UNREALIZED P&L (USDT)',p.unrealized_pnl);
   field(grid,'SIZE (GATE CONTRACTS)',p.size_contracts);
   field(grid,'ENTRY PRICE',p.entry_price);field(grid,'GATE MARK PRICE',p.mark_price);
   const protectedByExchange=reconciled && p.protection_status==='EXCHANGE_VERIFIED';
   field(grid,'STOP LOSS',protectedByExchange?p.stop_loss:'NOT VERIFIED');
   field(grid,'TAKE PROFIT',protectedByExchange?p.take_profit:'NOT VERIFIED');
   card.appendChild(grid);
   card.appendChild(node('p','v45-refresh-state',
     'Execution: '+safe(p.execution_status)+' · SL/TP: '+safe(p.protection_status)+
     ' · '+safe(p.protection_reason)+' · Order controls locked'));
   const controls=node('div','v4-controls');
   for(const action of ['Market Close — LOCKED','Close — LOCKED','Reverse — LOCKED']){
      const b=node('button','btn gloss-safe-disabled',action);b.disabled=true;controls.appendChild(b);
   }card.appendChild(controls);cards.appendChild(card);
  }
  status.textContent=mode+' · '+new Date().toLocaleTimeString('en-IN',{timeZone:'Asia/Kolkata'})+
   ' IST · '+(halted?'HALT / ORDERS OFF':'ORDERS OFF')+
   ' · Market scanners run independently';
 }catch(e){
  status.textContent='PRODUCTION READBACK UNAVAILABLE — exposure UNKNOWN, protection UNKNOWN, real-money orders OFF';
  count.textContent='Exchange positions not verified';
 }finally{btn.disabled=false;}
}
btn.addEventListener('click',refresh);refresh();
setInterval(()=>{if(!document.hidden)refresh();},30000);
})();</script>'''

PRODUCTION_BANNER='''<section class="live-ops-banner" aria-label="Production trading preparation">
<div><strong>GATE.IO FUTURES · PRODUCTION MONITOR</strong><p>Real-money exchange account is read-only. LuxAlgo signals are recorded; automatic live orders are NOT enabled. Native production SL/TP and emergency close have not been verified.</p></div>
</section>'''

PRODUCTION_CONTROL=''  # Replaced by the single Radar-side production BOT switch.


PRODUCTION_BOT_WIDGET=r'''<div class="prod-radar-bot" aria-label="Production operator bot control">
<label for="productionBotSwitch" class="prod-radar-bot-label">BOT CONTROL</label>
<button type="button" id="productionBotSwitch" role="switch" aria-checked="false"
        aria-label="Toggle production bot ON or OFF" disabled>BOT OFF</button>
<span class="prod-radar-bot-detail" id="productionBotDetail" role="status" aria-live="polite">Checking server safety state…</span>
</div>'''

PRODUCTION_BOT_SCRIPT=r'''<script>(function(){'use strict';
const sw=document.getElementById('productionBotSwitch'),
      detail=document.getElementById('productionBotDetail');
if(!sw||!detail)return;
let busy=false,actualOn=false;
const url='/terminal/production-bot-control';
function display(state){
 const on=state&&state.bot_on===true;
 actualOn=on;sw.disabled=false;sw.setAttribute('aria-checked',String(on));
 sw.textContent=on?'BOT ON':'BOT OFF';
 sw.classList.toggle('enabled',on);
 const reason=String(state?.reason||state?.error||'EXECUTION STATUS UNKNOWN');
 detail.textContent=on?'Automatic entries permitted · Gate.io LIVE':
     'New entries blocked · '+reason.replaceAll('_',' ');
}
async function refresh(){
 if(busy)return;
 try{
  const response=await fetch(url,{cache:'no-store',credentials:'same-origin'});
  const state=await response.json();
  if(!response.ok)throw Error(state.error||'Server-side state unavailable');
  display(state);
 }catch(e){
  actualOn=false;sw.disabled=true;sw.setAttribute('aria-checked','false');
  sw.textContent='BOT OFF';detail.textContent='Execution state unverified · BOT OFF';
 }
}
sw.addEventListener('click',async()=>{
 if(busy)return;busy=true;sw.disabled=true;
 const desired=!actualOn;
 detail.textContent=desired?'Checking all mandatory ON gates…':'Persisting OFF / emergency HALT…';
 try{
  const csrf=document.querySelector('input[name="csrf_token"]')?.value;
  if(!csrf)throw Error('Session CSRF unavailable');
  const response=await fetch(url,{method:'POST',credentials:'same-origin',
     cache:'no-store',headers:{'Content-Type':'application/json','X-Terminal-CSRF':csrf},
     body:JSON.stringify({enabled:desired})});
  const state=await response.json();
  display(state);
  if(!response.ok)detail.textContent='BOT OFF · '+String(state.reason||state.error||'Authorization denied').replaceAll('_',' ');
 }catch(e){detail.textContent='BOT OFF · update not verified';}
 finally{busy=false;await refresh();}
});
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
body.production-view .prod-radar-bot{display:flex;align-items:center;gap:7px;flex-wrap:wrap;margin-right:12px;min-width:220px}
body.production-view .prod-radar-bot-label{font-size:10px;font-weight:900;color:#b7d8ef;white-space:nowrap}
body.production-view #productionBotSwitch{border:1px solid #fa9a9a;background:#6a2440;color:#fff;
 border-radius:14px;padding:7px 13px;font:900 12px system-ui,Arial;cursor:pointer;min-width:88px}
body.production-view #productionBotSwitch.enabled{border-color:#73e1a6;background:#126d48}
body.production-view #productionBotSwitch:disabled{cursor:wait;opacity:.65}
body.production-view .prod-radar-bot-detail{font-size:10px;color:#ffe0a4;max-width:245px}
@media(max-width:640px){body.production-view .prod-radar-bot{width:100%;margin:0 0 9px}}
body.production-view .production-position-cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;padding:12px 16px 17px}
body.production-view .production-position-card{border:1px solid #355a7b;background:linear-gradient(145deg,#142d49,#0a1d32);border-radius:12px;padding:14px;min-width:0}
body.production-view .production-position-card .v4-trade-top{display:flex;gap:10px;align-items:center}
body.production-view .production-position-card .v4-numbers{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-top:16px}
body.production-view .production-position-card .v4-controls{display:flex;flex-wrap:wrap;gap:7px;margin-top:12px}
body.production-view .production-position-card .v4-controls button{flex:1}
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
    # Place the only production BOT control immediately left of Futures Radar.
    radar='<section class="panel market-panel"><div class="panel-heading"><h2>GATE.IO FUTURES RADAR'
    if out.count(radar)!=1:raise ValueError("Futures Radar heading missing")
    out=out.replace(radar,
        '<section class="panel market-panel"><div class="panel-heading">'+
        PRODUCTION_BOT_WIDGET+'<h2>GATE.IO FUTURES RADAR',1)
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
    # The same signal table remains, but production must never use stale
    # Testnet fill/SL/TP states as evidence of real-money execution.
    old="fetch('/terminal/incoming-signals',{cache:'no-store'})"
    if out.count(old)!=1:
        raise ValueError("Signal monitor script changed unexpectedly")
    out=out.replace(old,"fetch('/terminal/incoming-signals?production=1',{cache:'no-store'})",1)
    return out
