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
   for(const id of ['fake-one','fake-two']){
     if(!v.includes('data-v45-card="'+id+'"'))continue;
     const fields={pnl:{textContent:'—'},mark:{textContent:'—'},roi:{textContent:'—'}};
     rows.push({dataset:{v45Card:id},querySelector(selector){
       const m=selector.match(/data-v45-field="([^"]+)"/);
       return m?fields[m[1]]:null;
     },fields});
   }
 }});
 const demo={onclick:null,textContent:''};
 const elems={
  paperBody:{closest:()=>({id:'old-paper'}),querySelectorAll:()=>[]},
  paperRefresh:{click:()=>{}},
  v4Workspace:{querySelector:()=>demo,addEventListener:()=>{}},
  v4PaperCards:holder,v4TabCount:{textContent:''},
  v4DemoWarning:{hidden:true},v4PaperCardsStatus:status
 };
 const document={hidden:false,getElementById:id=>elems[id]??null,addEventListener:()=>{}};
 const fetch=async url=>{
   requests.push(url);
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
 assert.equal(rows.length,2,'Expected two individual paper cards');
 assert.match(cardHTML,/ETHUSDT/);assert.match(cardHTML,/SOLUSDT/);
 assert.match(cardHTML,/STOP LOSS/);assert.match(cardHTML,/TAKE PROFIT \(TP1\)/);
 assert.match(cardHTML,/disabled title="No exchange position exists/);
 assert.equal(elems.v4TabCount.textContent,'Paper Positions (2)');
 assert.equal(rows[0].fields.mark.textContent,'2412');
 assert.equal(rows[0].fields.pnl.textContent,'+0.12');
 assert.equal(rows[1].fields.pnl.textContent,'+0.22');
 assert(requests.includes('/terminal/paper-open-direct'));
 assert(requests.includes('/terminal/paper-live-pnl'));
 console.log('PASS: restored individual cards visible, two simulated positions, SL/TP, public mark and paper PnL');
})().catch(err=>{console.error(err);process.exitCode=1;});
