"""Runtime regression for production position-card DOM; all requests mocked.

Exercises the *actual embedded dashboard JavaScript* with a small DOM double.
No live exchange API call, no database mutation and no trading order.
"""
import subprocess
import unittest

from production_cutover_view import PRODUCTION_CARDS, PRODUCTION_CSS

NODE_SMOKE = r"""
'use strict';
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(0,'utf8');
const match=html.match(/<script>([\s\S]*?)<\/script>/);
assert(match,'Expected embedded production card script');
class Element {
  constructor(tag='div'){
    this.tagName=tag.toLowerCase();this.className='';this.children=[];
    this.parentElement=null;this.dataset={};this.listeners={};this.attributes={};
    this.hidden=false;this.disabled=false;this._text='';
    this.classList={toggle:(name,force)=>{
       const classes=new Set(this.className.split(/\s+/).filter(Boolean));
       const enable=force===undefined?!classes.has(name):Boolean(force);
       enable?classes.add(name):classes.delete(name);
       this.className=[...classes].join(' ');
       return enable;
    },contains:(name)=>this.className.split(/\s+/).includes(name)};
  }
  set textContent(t){this.replaceChildren();this._text=String(t);}
  get textContent(){return this._text+this.children.map(c=>c.textContent).join('');}
  append(...nodes){for(const n of nodes){assert(n instanceof Element);n.parentElement=this;this.children.push(n);}}
  appendChild(n){this.append(n);return n;}
  replaceChildren(...nodes){for(const n of this.children)n.parentElement=null;this.children=[];this._text='';this.append(...nodes);}
  remove(){if(this.parentElement){this.parentElement.children=this.parentElement.children.filter(n=>n!==this);this.parentElement=null;}}
  get firstChild(){return this.children[0]||null;}
  addEventListener(event,callback){this.listeners[event]=callback;}
  setAttribute(key,value){this.attributes[key]=String(value);}
  getAttribute(key){return this.attributes[key]??null;}
  matches(query){
    if(query==='article[data-production-key]')return this.tagName==='article'&&Boolean(this.dataset.productionKey);
    if(query==='[data-production-field]')return Boolean(this.dataset.productionField);
    if(query.startsWith('.'))return this.className.split(/\s+/).includes(query.slice(1));
    if(query==='tv-advanced-chart')return this.tagName==='tv-advanced-chart';
    return false;
  }
  querySelectorAll(query){let out=[];
     for(const child of this.children){if(child.matches(query))out.push(child);
       out=out.concat(child.querySelectorAll(query));}
     return out;}
  querySelector(query){return this.querySelectorAll(query)[0]||null;}
}
const cards=new Element('div'),count=new Element('strong'),status=new Element('p'),button=new Element('button');
const prompt=new Element('div');prompt.className='v4-prompt';prompt.textContent='Loading authenticated production positions…';
cards.appendChild(prompt);
const nodes={productionPositionCards:cards,productionTabCount:count,productionCardsStatus:status,productionCardsRefresh:button};
const document={hidden:false,createElement:tag=>new Element(tag),getElementById:id=>nodes[id]||null};
const bull={contract:'BULLA_USDT',side:'LONG',size_contracts:'283',
  unrealized_pnl:'-6.27',entry_price:'0.07059',mark_price:'0.06837',
  protection_status:'NOT_VERIFIED',execution_status:'EXTERNAL_OR_UNTRACKED',
  protection_reason:'UNTRACKED_EXCHANGE_POSITION'};
const eth={...bull,contract:'ETH_USDT',side:'SHORT',size_contracts:'1'};
let response={positions:[bull],status:'HALT_REVIEW_REQUIRED',halted:true};
const fetch=async url=>{
  assert.equal(url,'/terminal/live-execution/snapshot','no unexpected network calls');
  return {ok:true,json:async()=>response};
};
const context={document,fetch,console,Date,Map,Set,encodeURIComponent,
  setInterval:()=>{},window:{}};
vm.runInNewContext(match[1],context,{timeout:1500});
(async()=>{
  // Initial async server GET completes and removes the stale loading tile.
  await new Promise(resolve=>setTimeout(resolve,10));
  assert.equal(cards.querySelectorAll('.v4-prompt').length,0,'loading tile must disappear');
  assert.equal(cards.querySelectorAll('article[data-production-key]').length,1);
  assert.equal(cards.children[0].dataset.productionKey,'BULLA_USDT|LONG','card must occupy first grid slot');
  assert(cards.classList.contains('one-position'),'one position should use single-column CSS');
  assert.equal(count.textContent,'Gate.io Production Positions (1)');
  const card=cards.children[0],title=card.querySelector('.production-token-toggle');
  assert.equal(title.textContent,'BULLAUSDT');
  title.listeners.click();
  const chart=card.querySelector('.production-chart');
  assert.equal(chart.hidden,false,'token click must reveal chart');
  const widget=chart.querySelector('tv-advanced-chart');
  assert(widget,'chart must contain TradingView widget');
  assert.equal(widget.getAttribute('symbol'),'GATEIO:BULLAUSDT.P');
  assert.equal(widget.getAttribute('interval'),'5');
  assert.equal(title.getAttribute('aria-expanded'),'true');

  // Periodic refresh must not recreate the card, lose the open chart or resurrect loader.
  await button.listeners.click();
  assert.equal(cards.children[0],card,'refresh must reuse the existing card node');
  assert.equal(card.querySelector('tv-advanced-chart'),widget,'chart must survive refresh');
  assert.equal(cards.querySelectorAll('.v4-prompt').length,0);

  response={positions:[],status:'READ_ONLY_RECONCILED',halted:true};
  await button.listeners.click();
  assert.equal(cards.querySelectorAll('article[data-production-key]').length,0);
  assert.equal(cards.querySelectorAll('.v4-prompt').length,1,'zero state must show one placeholder');
  assert(!cards.classList.contains('one-position'),'empty state should not use card layout');

  response={positions:[bull,bull],status:'HALT_REVIEW_REQUIRED',halted:true};
  await button.listeners.click();
  assert.equal(cards.querySelectorAll('article[data-production-key]').length,1,'duplicate same-position readbacks must not create duplicate cards');
  assert.equal(cards.querySelectorAll('.v4-prompt').length,0,'empty placeholder must disappear when positions return');

  response={positions:[bull,eth],status:'HALT_REVIEW_REQUIRED',halted:true};
  await button.listeners.click();
  assert.equal(cards.querySelectorAll('article[data-production-key]').length,2);
  assert(!cards.classList.contains('one-position'),'two positions should use two-column grid');
  assert.equal(cards.querySelectorAll('.v4-prompt').length,0);
  const locked=cards.children[0].querySelectorAll('.gloss-safe-disabled');
  assert.equal(locked.length,3);
  assert(locked.every(x=>x.disabled===true),'live order controls must remain locked');
  console.log('PRODUCTION_DOM_RUNTIME_PASS one_position, expand_chart, stable_refresh, empty_transition, duplicate_dedup, two_positions, locked_controls');
})().catch(e=>{console.error(e);process.exitCode=1;});
"""

class ProductionLiveCardDOMTests(unittest.TestCase):
    def test_real_embedded_script_runtime_and_layout(self):
        # Node is preinstalled on GitHub ubuntu-latest runners.
        result=subprocess.run(['node','-e',NODE_SMOKE],input=PRODUCTION_CARDS,
                              text=True,capture_output=True,timeout=12)
        self.assertEqual(result.returncode,0,result.stdout+'\n'+result.stderr)
        self.assertIn('PRODUCTION_DOM_RUNTIME_PASS',result.stdout)
        self.assertIn(".production-position-cards.one-position{grid-template-columns:minmax(0,1fr)}",PRODUCTION_CSS)
        self.assertIn('max-width:980px',PRODUCTION_CSS)

if __name__=='__main__':
    unittest.main()
