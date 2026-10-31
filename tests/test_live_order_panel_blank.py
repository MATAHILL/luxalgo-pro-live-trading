"""Live order-inventory UI: no archived BTC test and no false empty readback."""
import subprocess
import unittest

from production_cutover_view import PRODUCTION_ORDERS, production_monitor_page
from app import PAGE

NODE_SMOKE = r"""
'use strict';
const vm=require('node:vm'),assert=require('node:assert/strict'),fs=require('node:fs');
const html=fs.readFileSync(0,'utf8');
const script=html.match(/<script>([\s\S]*?)<\/script>/);
assert(script,'Order status script missing');
class Element {
  constructor(){this.textContent='';this.disabled=false;this.listeners={};}
  addEventListener(name,callback){this.listeners[name]=callback;}
}
const elements={};
for(const id of ['productionOpenOrders','productionOpenTriggers',
  'productionOpenPositions','productionOrdersStatus','productionOrdersRefresh']){
  elements[id]=new Element();
}
const document={hidden:false,getElementById:id=>elements[id]||null};
const state={data:{status:'READ_ONLY_RECONCILED',positions:[],open_positions:0,
  exchange_open_orders:0,exchange_open_triggers:0,fetched_at:1793425000},fail:false};
const fetch=async (url,options)=>{
  assert.equal(url,'/terminal/live-execution/snapshot');
  assert.equal(options.credentials,'same-origin');
  assert.equal(options.cache,'no-store');
  assert.equal(options.signal instanceof AbortSignal,true);
  return {ok:!state.fail,json:async()=>state.data};
};
vm.runInNewContext(script[1],{document,fetch,AbortController,Date,Number,
  clearTimeout,setTimeout,setInterval:()=>{},console},{timeout:1500});
const refresh=elements.productionOrdersRefresh;
(async()=>{
 await new Promise(resolve=>setTimeout(resolve,10));
 assert.equal(elements.productionOpenOrders.textContent,'0 · EMPTY');
 assert.equal(elements.productionOpenTriggers.textContent,'0 · EMPTY');
 assert.equal(elements.productionOpenPositions.textContent,'0 · EMPTY');
 assert.match(elements.productionOrdersStatus.textContent,/Exchange confirms no open orders/);
 state.data={...state.data,exchange_open_orders:1,exchange_open_triggers:2};
 await refresh.listeners.click();
 assert.equal(elements.productionOpenOrders.textContent,'1 · OPEN');
 assert.equal(elements.productionOpenTriggers.textContent,'2 · OPEN');
 assert.match(elements.productionOrdersStatus.textContent,/Active exchange exposure detected/);
 state.fail=true;
 await refresh.listeners.click();
 for(const id of ['productionOpenOrders','productionOpenTriggers','productionOpenPositions'])
  assert.equal(elements[id].textContent,'UNVERIFIED','readback error must not show stale zeros');
 state.fail=false;
 state.data={...state.data,exchange_open_orders:null};
 await refresh.listeners.click();
 assert.equal(elements.productionOpenOrders.textContent,'UNVERIFIED');
 console.log('PRODUCTION_ORDERS_EMPTY_AND_FAILCLOSED_PASS');
})().catch(e=>{console.error(e);process.exitCode=1;});
"""

class LiveOrdersPanelCleanupTests(unittest.TestCase):
    def test_main_terminal_has_no_old_btc_acceptance_order_panel(self):
        page=production_monitor_page(PAGE)
        self.assertIn('id="productionOrdersPanel"',page)
        self.assertIn('LIVE FUTURES ORDERS',page)
        self.assertIn('No historical test-order details',page)
        self.assertNotIn('id="btcAcceptancePanel"',page)
        self.assertNotIn('BTC_USDT · ONE-CONTRACT LIVE ACCEPTANCE TEST',page)
        self.assertNotIn("'/terminal/acceptance-test/status'",page)
        self.assertIn('id="productionPositionCards"',page)
        self.assertIn('id="productionBotOff"',page)
        self.assertIn('INCOMING LUXALGO SIGNALS · NOT EXCHANGE ORDERS',page)
        self.assertNotIn('INCOMING LUXALGO ORDERS · WEBHOOK SIGNALS',page)

    def test_live_inventory_updates_empty_nonempty_and_unknown(self):
        r=subprocess.run(['node','-e',NODE_SMOKE],input=PRODUCTION_ORDERS,
            text=True,capture_output=True,timeout=15)
        self.assertEqual(r.returncode,0,r.stdout+'\n'+r.stderr)
        self.assertIn('PRODUCTION_ORDERS_EMPTY_AND_FAILCLOSED_PASS',r.stdout)

if __name__=='__main__':
    unittest.main()
