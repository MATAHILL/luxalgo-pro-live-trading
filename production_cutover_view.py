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
  const res=await fetch('/terminal/live-preparation/status',{cache:'no-store',credentials:'same-origin'});
  const data=await res.json();
  if(!res.ok||data.status!=='PREPARATION_ONLY'||!data.positions_verified||!Array.isArray(data.production_positions))
     throw Error(data.error||'Authenticated production position readback unavailable');
  cards.replaceChildren();
  count.textContent='Gate.io Production Positions ('+data.production_positions.length+')';
  if(!data.production_positions.length)cards.appendChild(node('div','v4-prompt','No open production USDT futures positions reported by Gate.io.'));
  for(const p of data.production_positions){
   const card=node('article','v4-trade production-position-card');
   const top=node('div','v4-trade-top');
   const title=node('strong','v4-token',safe(p.contract).replace('_USDT','USDT'));
   const tag=node('span','v4-tag',safe(p.side));top.append(title,tag);card.appendChild(top);
   const grid=node('div','v4-numbers');
   field(grid,'EXCHANGE UNREALIZED P&L (USDT)',p.unrealized_pnl);
   field(grid,'SIZE (GATE CONTRACTS)',p.size_contracts);
   field(grid,'ENTRY PRICE',p.entry_price);field(grid,'GATE MARK PRICE',p.mark_price);
   field(grid,'STOP LOSS','NOT VERIFIED');field(grid,'TAKE PROFIT','NOT VERIFIED');
   card.appendChild(grid);
   card.appendChild(node('p','v45-refresh-state','Production exchange position · SL/TP not reconciled · Read-only; controls locked'));
   const controls=node('div','v4-controls');
   for(const action of ['Market Close — LOCKED','Close — LOCKED','Reverse — LOCKED']){
      const b=node('button','btn gloss-safe-disabled',action);b.disabled=true;controls.appendChild(b);
   }card.appendChild(controls);cards.appendChild(card);
  }
  status.textContent='Production positions refreshed · '+new Date().toLocaleTimeString('en-IN',{timeZone:'Asia/Kolkata'})+
  ' IST · REAL MONEY ORDER SUBMISSION OFF';
 }catch(e){status.textContent='PRODUCTION READBACK UNAVAILABLE — no assumptions about positions or protection; live orders disabled';count.textContent='Exchange positions unverified';}
 finally{btn.disabled=false;}
}
btn.addEventListener('click',refresh);refresh();
setInterval(()=>{if(!document.hidden)refresh();},30000);
})();</script>'''

PRODUCTION_BANNER='''<section class="live-ops-banner" aria-label="Production trading preparation">
<div><strong>GATE.IO FUTURES · PRODUCTION MONITOR</strong><p>Real-money exchange account is read-only. LuxAlgo signals are recorded; automatic live orders are NOT enabled. Native production SL/TP and emergency close have not been verified.</p></div>
<span class="live-ops-state">LIVE ORDERS OFF</span></section>'''

PRODUCTION_CONTROL='''<section class="bot-control-panel" aria-label="Production bot execution state">
<div><div class="bot-status-line"><strong>PRODUCTION BOT · NOT ARMED</strong><span class="bot-state-indicator off" role="status">● EXECUTION OFF</span></div>
<p>No Testnet entries and no live-money orders. Existing exchange positions are never treated as safely protected without independent readback.</p></div>
<div><a href="/terminal/live-preparation" class="btn gloss-action">Production readiness</a></div></section>'''

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
