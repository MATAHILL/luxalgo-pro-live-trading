// Dashboard card regression test: synthetic UI; NO exchange calls.
'use strict';
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
(async()=>{
 const page=fs.readFileSync('app.py','utf8');
 const i=page.indexOf("PAGE = '''"),j=page.indexOf("'''",i+10);
 assert(i>=0&&j>i,'HTML terminal template missing');
 const html=page.slice(i+10,j);
 assert(html.includes('id="v4Workspace"'),'Original individual card workspace missing');
 assert(html.includes('id="v4PaperCards"'),'Individual card container missing');
 assert(!html.includes('#v4Workspace,#v4PaperCards{display:none!important}'),
        'Cards incorrectly hidden by old CSS override');
 assert(!html.includes("document.getElementById('chartShell')"),
        'Card setup still requires removed chartShell');
 assert(html.includes('widgets.tradingview-widget.com/w/en/tv-advanced-chart.js'),
        'TradingView custom-element loader missing');
 const start=html.indexOf('// Stage 16 V4 presentation');
 const end=html.indexOf('</script>',start);
 assert(start>=0&&end>start,'Card render script missing');
 const source=html.slice(start,end);
 let cardHTML='',rows=[],requests=[],status={textContent:''};
 const holder={querySelectorAll:()=>rows};
 Object.defineProperty(holder,'innerHTML',{get(){return cardHTML},set(v){
   cardHTML=v;rows=[];
   for(const id of ['TESTNET:ETH_USDT:LONG']){
     if(!v.includes('data-v45-card="'+id+'"'))continue;
     const fields={pnl:{textContent:'—'},mark:{textContent:'—'},roi:{textContent:'—'}};
     rows.push({dataset:{v45Card:id},querySelector(selector){
       const m=selector.match(/data-v45-field="([^"]+)"/);
       return m?fields[m[1]]:null;
     },fields});
   }
 }});
 const demo={onclick:null,textContent:''};
 const refresh={disabled:false,addEventListener:()=>{}};
 const elems={
  paperBody:{closest:()=>({id:'old-paper'}),querySelectorAll:()=>[]},
  paperRefresh:{click:()=>{}},
  v4Workspace:{querySelector:()=>demo,addEventListener:()=>{}},
  v4PaperCards:holder,v4TabCount:{textContent:''},
  v4DemoWarning:{hidden:true},v4PaperCardsStatus:status,
  v4TestnetRefresh:refresh,v4TestnetCardsStatus:{textContent:''}
 };
 const document={hidden:false,getElementById:id=>elems[id]??null,addEventListener:()=>{}};
 const fetch=async url=>{
   requests.push(url);
   if(url==='/terminal/testnet-position-cards')return {ok:true,json:async()=>({environment:'GATE_FUTURES_TESTNET',fetched_at:1800000000,positions:[
    {contract:'ETH_USDT',symbol:'ETHUSDT',side:'LONG',size_contracts:1,entry_price:2400,mark_price:2410,unrealized_pnl:0.10,leverage_label:'5×',roi_percent:2,margin_usdt:4.8,liquidation_price:1800,protection_state:'EXCHANGE_VERIFIED',origin:'ONE_SHOT_TESTNET_TRIAL',signal_id:'testnet-trial-20261027-a',entry_order_id:'12345',stop_loss:'2380',take_profit:'2450',stop_order_id:'222',take_order_id:'333'}
   ]})};
   if(url==='/terminal/paper-open-direct')return {ok:true,json:async()=>({positions:[
    {event_id:'fake-one',symbol:'ETHUSDT',side:'LONG',entry:'2400',stop_loss:'2380',take_profit:'2450',size_contracts:1},
    {event_id:'fake-two',symbol:'SOLUSDT',side:'SHORT',entry:'160',stop_loss:'164',take_profit:'154',size_contracts:2}
   ]})};
   if(url==='/terminal/paper-live-pnl')return {ok:true,json:async()=>({
    source:'GATE_IO_PUBLIC_LAST_PRICE',marks:{
     'fake-one':{mark_price:'2412',unrealized_pnl_usdt:'0.12'},
     'fake-two':{mark_price:'157',unrealized_pnl_usdt:'0.22'}}
   })};
   throw Error('Unexpected endpoint: '+url);
 };
 class Controller{constructor(){this.signal={}}abort(){}}
 class Observer{observe(){}}
 vm.runInNewContext(source,{document,fetch,AbortController:Controller,
  MutationObserver:Observer,setTimeout:()=>0,clearTimeout:()=>{},setInterval:()=>{}},
  {timeout:3000});
 for(let n=0;n<4;n++)await new Promise(resolve=>setImmediate(resolve));
 assert.equal(rows.length,1,'Expected exactly one exchange Testnet card');
 assert.match(cardHTML,/ETHUSDT/);assert.doesNotMatch(cardHTML,/SOLUSDT/);
 assert.match(cardHTML,/STOP LOSS/);assert.match(cardHTML,/TAKE PROFIT \(TP1\)/);
 assert.match(cardHTML,/data-v45-inspect="1"/);
 assert.match(cardHTML,/data-v45-close="market"/);
 assert.match(cardHTML,/data-v45-close="close"/);
 assert.match(cardHTML,/Reverse · LOCKED/);
 assert(!cardHTML.includes('data-v45-close="market" disabled'));
 assert.equal(elems.v4TabCount.textContent,'Gate.io Testnet Positions (1)');
 assert.match(cardHTML,/EXCHANGE UNREALIZED P&L/);
 assert.match(cardHTML,/2410/);
 assert.match(cardHTML,/>\+0.1</);
 assert.match(cardHTML,/TESTNET · EXCHANGE/);
 assert.match(cardHTML,/Native SL\/TP: EXCHANGE_VERIFIED/);
 assert.match(cardHTML,/SL #222 · TP #333/);
 assert(requests.includes('/terminal/testnet-position-cards'));
 assert(!requests.includes('/terminal/paper-open-direct'));
 assert(!requests.includes('/terminal/paper-live-pnl'));
 console.log('PASS: Testnet-only card, native TP/SL and PnL; view and guarded close controls, Reverse locked');
})().catch(err=>{console.error(err);process.exitCode=1;});
