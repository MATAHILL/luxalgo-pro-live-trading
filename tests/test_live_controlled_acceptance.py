"""Offline acceptance lifecycle/fault tests; all exchange writes are fake."""
import copy
import os
import unittest
from contextlib import ExitStack, nullcontext
from unittest.mock import MagicMock, Mock, patch

import controlled_acceptance as acceptance
import live_futures_engine as engine
import live_execution_orchestrator as lifecycle

NOW=1800000000
SCOPE=dict(scope_id='offline_scope_123456',contract='BTC_USDT',side='LONG',
           start_epoch=NOW-10,end_epoch=NOW+600,contracts=1,leverage='5',
           max_margin='1.70',max_notional='8.30',max_modeled_loss='0.10')
SIGNAL=dict(event_id='a'*20,contract='BTC_USDT',side='LONG',timeframe='5m',
            levels_status='VALID',entry='80000',sl='79900',tp='80200',
            received_epoch=NOW-1,source_bar_id='TVBAR_5m_00005981030',order_type='MARKET')
META=dict(name='BTC_USDT',quanto_multiplier='0.0001',order_size_min='1',
          order_size_max='100000',market_order_size_max='100000',taker_fee_rate='0.00075',
          status='trading',in_delisting=False,order_price_round='0.1',
          leverage_min='1',leverage_max='100',mark_price='80000')


class Exchange:
    def __init__(self):
        self.entries=0; self.protections=[]; self.plan=None; self.calls=[]
        self.meta=copy.deepcopy(META); self.partial=False; self.unknown=False
    def snapshot(self):
        return dict(source='GATE_IO_LIVE_FUTURES',fetched_at=NOW,
            account=dict(currency='USDT',total='100',available='100',unrealised_pnl='0',position_mode='single'),
            positions=[dict(contract='BTC_USDT',size='1')] if self.entries else [],orders=[],
            triggers=copy.deepcopy(self.protections),
            position_settings=[dict(contract='BTC_USDT',mode='single',leverage='5')])
    def request(self, method, path):
        self.calls.append((method,path))
        if method != 'GET': raise AssertionError('Unexpected direct write')
        if '/contracts/' in path: return self.meta
        return dict(id='101',text=self.plan['client_text'],contract='BTC_USDT',size=1,
                    left=1 if self.partial else 0,status='finished',fill_price='80000')
    def submit_entry(self, plan):
        self.entries+=1;self.plan=plan
        if self.unknown: raise engine.AckUnknown('simulated lost ack')
        return {'id':'101'}
    def submit_trigger(self, plan, kind):
        identity=str(201+len(self.protections))
        self.protections.append(dict(engine.build_protective_trigger(plan,kind),id=identity,status='open'))
        return {'id':identity}


class ControlledAcceptanceTests(unittest.TestCase):
    def prepared(self, client=None):
        return acceptance.prepare(SCOPE,SIGNAL,client or Exchange(),now=NOW)

    def fixture(self, client=None):
        stack=ExitStack();self.addCleanup(stack.close)
        stack.enter_context(patch.object(acceptance,'ACCEPTANCE_RELEASE',True))
        stack.enter_context(patch.object(engine,'EXCHANGE_WRITE_RELEASE',True))
        stack.enter_context(patch.object(acceptance.time,'time',return_value=NOW))
        stack.enter_context(patch.dict(os.environ,dict(LIVE_TRADING_ENABLED='true',ORDERS_ENABLED='true',
            LIVE_ORDERS_SEPARATELY_AUTHORIZED='true',LUXALGO_LIVE_RISK_PERCENT='0.5')))
        consumed=set()
        def claim(connection, review, digest):
            event=review['plan']['event_id']
            if event in consumed: raise engine.LiveSafetyError('already consumed')
            consumed.add(event)
        stack.enter_context(patch.object(acceptance,'claim_and_reserve',side_effect=claim))
        self.persist=stack.enter_context(patch.object(lifecycle,'persist_state'))
        self.emergency=stack.enter_context(patch.object(lifecycle,'emergency_persist_halt'))
        self.record=stack.enter_context(patch.object(acceptance,'record_outcome'))
        self.halt=stack.enter_context(patch.object(acceptance,'halt'))
        stack.enter_context(patch.object(acceptance,'control',return_value={'halted':False}))
        con=MagicMock()
        return client or Exchange(),lambda:nullcontext(con)

    def test_production_source_lock_blocks_before_network_or_database(self):
        client=Mock();db=Mock()
        with self.assertRaisesRegex(engine.LiveSafetyError,'SOURCE_LOCKED'):
            acceptance.submit_once(db,{},'anything',SIGNAL,client)
        db.assert_not_called();client.snapshot.assert_not_called()

    def test_preview_is_one_contract_get_only_not_permission(self):
        c=Exchange();p=self.prepared(c)
        self.assertEqual(p['review']['plan']['contracts'],1)
        self.assertFalse(p['can_execute']);self.assertEqual(c.entries,0)
        self.assertTrue(all(m=='GET' for m,_ in c.calls))
        self.assertEqual(p['review_digest'],acceptance.canonical_digest(p['review']))

    def test_expired_scope_stale_signal_and_missing_bar_reject(self):
        for scope,signal in [(dict(SCOPE,end_epoch=NOW),SIGNAL),
                             (SCOPE,dict(SIGNAL,received_epoch=NOW-61)),
                             (SCOPE,dict(SIGNAL,source_bar_id=None))]:
            with self.subTest(signal=signal):
                with self.assertRaises(engine.LiveSafetyError):
                    acceptance.prepare(scope,signal,Exchange(),now=NOW)

    def test_broadened_scope_and_minimum_lot_reject(self):
        for changes in ({'contracts':2},{'contracts':True},{'max_modeled_loss':'NaN'},
                        {'max_notional':'9'},{'leverage':'6'},{'end_epoch':NOW+1000}):
            with self.assertRaises(engine.LiveSafetyError):
                acceptance.prepare(dict(SCOPE,**changes),SIGNAL,Exchange(),now=NOW)
        c=Exchange();c.meta['order_size_min']='2'
        with self.assertRaises(engine.LiveSafetyError): self.prepared(c)

    def test_scoped_risk_cap_and_off_tick_protection_reject(self):
        with self.assertRaises(engine.LiveSafetyError):
            acceptance.prepare(dict(SCOPE,max_modeled_loss='0.001'),SIGNAL,Exchange(),now=NOW)
        with self.assertRaises(engine.LiveSafetyError):
            acceptance.prepare(SCOPE,dict(SIGNAL,sl='79900.05'),Exchange(),now=NOW)

    def test_verified_entry_and_linked_sl_tp_halt_for_review(self):
        c,db=self.fixture();p=self.prepared(c)
        result=acceptance.submit_once(db,p,p['review_digest'],SIGNAL,c)
        self.assertEqual(result['state'],'PROTECTED_REVIEW_REQUIRED')
        self.assertEqual(c.entries,1);self.assertEqual(len(c.protections),2)
        self.assertFalse(result['certified']);self.assertFalse(result['continuous_trading_enabled'])
        self.halt.assert_called_once()
        with self.assertRaisesRegex(engine.LiveSafetyError,'inventory|Inventory'):
            acceptance.submit_once(db,p,p['review_digest'],SIGNAL,c)
        self.assertEqual(c.entries,1)

    def test_lost_ack_consumes_claim_and_never_retries(self):
        c,db=self.fixture();p=self.prepared(c);c.unknown=True
        result=acceptance.submit_once(db,p,p['review_digest'],SIGNAL,c)
        self.assertEqual(result['state'],'UNKNOWN_REVIEW_REQUIRED')
        self.assertTrue(result['halt_persisted']);self.assertEqual(c.entries,1)
        self.assertEqual(c.protections,[])
        # Even if exchange later appears flat, the durable claim stays spent.
        c.entries=0
        with self.assertRaisesRegex(engine.LiveSafetyError,'consumed'):
            acceptance.submit_once(db,p,p['review_digest'],SIGNAL,c)

    def test_partial_fill_never_places_protection_or_replays(self):
        c,db=self.fixture();p=self.prepared(c);c.partial=True
        result=acceptance.submit_once(db,p,p['review_digest'],SIGNAL,c)
        self.assertEqual(result['state'],'UNKNOWN_REVIEW_REQUIRED')
        self.assertEqual(len(c.protections),0);self.assertEqual(c.entries,1)

    def test_wrong_digest_or_changed_metadata_no_entry(self):
        c,db=self.fixture();p=self.prepared(c)
        with self.assertRaises(engine.LiveSafetyError):
            acceptance.submit_once(db,p,'0'*64,SIGNAL,c)
        c.meta['mark_price']='80001'
        with self.assertRaisesRegex(engine.LiveSafetyError,'changed'):
            acceptance.submit_once(db,p,p['review_digest'],SIGNAL,c)
        self.assertEqual(c.entries,0)

    def test_db_commit_failure_before_dispatch_no_entry(self):
        c,db=self.fixture();p=self.prepared(c)
        with patch.object(acceptance,'claim_and_reserve',side_effect=RuntimeError('DB commit')):
            with self.assertRaises(RuntimeError):
                acceptance.submit_once(db,p,p['review_digest'],SIGNAL,c)
        self.assertEqual(c.entries,0)

    def test_post_fill_journal_failure_halts_without_retry(self):
        c,db=self.fixture();p=self.prepared(c);self.persist.side_effect=RuntimeError('DB failure')
        result=acceptance.submit_once(db,p,p['review_digest'],SIGNAL,c)
        self.assertTrue(result['requires_manual_exchange_review']);self.assertEqual(c.entries,1)
        self.assertFalse(result['automatic_retry'])

    def test_halt_persistence_failure_is_reported(self):
        c,db=self.fixture();p=self.prepared(c);c.unknown=True
        self.emergency.side_effect=RuntimeError('DB failure')
        result=acceptance.submit_once(db,p,p['review_digest'],SIGNAL,c)
        self.assertFalse(result['halt_persisted']);self.assertTrue(result['requires_manual_exchange_review'])

    def test_continuous_worker_or_missing_environment_blocks(self):
        c,db=self.fixture();p=self.prepared(c)
        with patch.object(lifecycle,'AUTOMATIC_WORKER_RELEASE',True):
            with self.assertRaises(engine.LiveSafetyError): acceptance.submit_once(db,p,p['review_digest'],SIGNAL,c)
        with patch.dict(os.environ,{'ORDERS_ENABLED':'false'}):
            with self.assertRaises(engine.LiveSafetyError): acceptance.submit_once(db,p,p['review_digest'],SIGNAL,c)
        self.assertEqual(c.entries,0)

    def test_halt_at_final_dispatch_blocks_entry(self):
        c,db=self.fixture();p=self.prepared(c)
        with patch.object(acceptance,'control',return_value={'halted':True}):
            result=acceptance.submit_once(db,p,p['review_digest'],SIGNAL,c)
        self.assertEqual(c.entries,0);self.assertTrue(result['requires_manual_exchange_review'])

    def test_sql_claim_checks_halt_and_rejects_duplicate(self):
        con=MagicMock();cur=con.cursor.return_value.__enter__.return_value
        with (patch.object(acceptance,'require_release'),
              patch.object(acceptance,'control',return_value={'halted':False}),
              patch.object(acceptance.time,'time',return_value=NOW),
              patch.object(acceptance,'risk_reserve') as reserve):
            cur.fetchone.side_effect=[None,None]
            with self.assertRaisesRegex(engine.LiveSafetyError,'consumed'):
                acceptance.claim_and_reserve(lambda:nullcontext(con),self.prepared()['review'],'digest')
            reserve.assert_not_called()
            sql=' '.join(call.args[0] for call in cur.execute.call_args_list)
            self.assertIn('pg_advisory_xact_lock',sql);self.assertIn('ON CONFLICT DO NOTHING',sql)

    def test_failed_second_protective_order_never_retries_entry(self):
        c,db=self.fixture();p=self.prepared(c)
        original=c.submit_trigger
        def trigger(plan,kind):
            if kind=='TP': raise TimeoutError('uncertain target acknowledgment')
            return original(plan,kind)
        c.submit_trigger=trigger
        result=acceptance.submit_once(db,p,p['review_digest'],SIGNAL,c)
        self.assertEqual(result['state'],'UNKNOWN_REVIEW_REQUIRED')
        self.assertEqual(c.entries,1);self.assertEqual(len(c.protections),1)
        self.emergency.assert_called_once()

    def test_protection_deadline_and_wrong_ids_fail_closed(self):
        c,db=self.fixture();p=self.prepared(c)
        with patch.object(acceptance.time,'monotonic',side_effect=[100,106]):
            result=acceptance.submit_once(db,p,p['review_digest'],SIGNAL,c)
        self.assertEqual(result['state'],'UNKNOWN_REVIEW_REQUIRED')
        self.assertEqual(len(c.protections),2)
        self.assertNotIn('PROTECTED',[call.args[2] for call in self.persist.call_args_list])

    def test_exposure_or_mode_change_during_fill_rejects(self):
        c=Exchange();plan=self.prepared(c)['review']['plan'];c.entries=1
        base=c.snapshot()
        for changes in ({'orders':[{'id':1}]},{'triggers':[{'id':1}]},
                        {'triggers':None},
                        {'positions':[]},{'account':{'position_mode':'dual'}},
                        {'source':'UNKNOWN'}):
            with self.assertRaises(engine.LiveSafetyError):
                acceptance.require_isolated_position(plan,dict(base,**changes),protected=False)

    def test_two_competing_submissions_only_one_claim_wins(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Lock
        c,db=self.fixture();p=self.prepared(c);claimed=set();lock=Lock()
        def atomic_claim(connection,review,digest):
            with lock:
                if review['scope']['scope_id'] in claimed:
                    raise engine.LiveSafetyError('consumed')
                claimed.add(review['scope']['scope_id'])
        def run(exchange):
            try:
                return acceptance.submit_once(db,p,p['review_digest'],SIGNAL,exchange)['state']
            except engine.LiveSafetyError:
                return 'REJECTED'
        clients=[Exchange(),Exchange()]
        with patch.object(acceptance,'claim_and_reserve',side_effect=atomic_claim):
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes=list(pool.map(run,clients))
        self.assertEqual(sum(client.entries for client in clients),1)
        self.assertCountEqual(outcomes,['PROTECTED_REVIEW_REQUIRED','REJECTED'])

    def test_real_claim_requires_inactive_halt_before_reservation(self):
        con=MagicMock();cur=con.cursor.return_value.__enter__.return_value
        with (patch.object(acceptance,'require_release'),
              patch.object(acceptance.time,'time',return_value=NOW),
              patch.object(acceptance,'control',return_value={'halted':True}),
              patch.object(acceptance,'risk_reserve') as reserve):
            with self.assertRaisesRegex(engine.LiveSafetyError,'HALT'):
                acceptance.claim_and_reserve(lambda:nullcontext(con),self.prepared()['review'],'digest')
            reserve.assert_not_called()
            self.assertFalse(any('INSERT INTO' in call.args[0] for call in cur.execute.call_args_list))

    def test_fresh_readback_does_not_replace_approved_review_or_digest(self):
        c,db=self.fixture();p=self.prepared(c)
        p['review']['prepared_epoch']=NOW-20
        p['review_digest']=acceptance.canonical_digest(p['review'])
        with patch.object(acceptance,'claim_and_reserve') as claim:
            acceptance.submit_once(db,p,p['review_digest'],SIGNAL,c)
        stored=claim.call_args.args[1]
        self.assertEqual(stored['prepared_epoch'],NOW-20)
        self.assertEqual(acceptance.canonical_digest(stored),claim.call_args.args[2])

    def test_expiry_during_exchange_refresh_blocks_before_entry(self):
        real_claim=acceptance.claim_and_reserve
        real_prepare=acceptance.prepare
        c,db=self.fixture();p=self.prepared(c)
        p['review']['prepared_epoch']=NOW-29
        p['review_digest']=acceptance.canonical_digest(p['review'])
        with patch.object(acceptance.time,'time',return_value=NOW) as clock:
            def delayed_refresh(*args,**kwargs):
                value=real_prepare(*args,**kwargs)
                clock.return_value=NOW+2
                return value
            with (patch.object(acceptance,'prepare',side_effect=delayed_refresh),
                  patch.object(acceptance,'claim_and_reserve',side_effect=real_claim)):
                with self.assertRaisesRegex(engine.LiveSafetyError,'expired'):
                    acceptance.submit_once(db,p,p['review_digest'],SIGNAL,c)
        self.assertEqual(c.entries,0)


if __name__=='__main__': unittest.main()
