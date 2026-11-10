"""Manual recovery flow with fake exchange outcomes and isolated journals."""
import copy,unittest
from contextlib import ExitStack,nullcontext
from unittest.mock import MagicMock,Mock,patch
from flask import Flask
import manual_recovery as recovery
import recovery_terminal as terminal
import live_execution_orchestrator as lifecycle
from live_futures_engine import LiveSafetyError,client_text
from test_live_controlled_acceptance import NOW

EVENT='a'*20
ROW=dict(event_id=EVENT,contract='BTC_USDT',side='LONG',position_mode='single',
         state='HALTED',contracts=1,client_text=client_text(EVENT),entry_order_id='101',
         entry_price='80000',stop_loss='79900',take_profit='80200')

class RecoveryExchange:
    def __init__(self):self.posts=[];self.position=1;self.timeout=False;self.triggers=[]
    def snapshot(self):
        return dict(source='GATE_IO_LIVE_FUTURES',fetched_at=NOW,account={'position_mode':'single'},
            positions=[{'contract':'BTC_USDT','size':self.position}] if self.position else [],
            orders=[],triggers=self.triggers)
    def request(self,method,path,*,payload=None):
        if method=='POST':
            self.posts.append(copy.deepcopy(payload))
            if self.timeout:raise TimeoutError('simulated lost close ack')
            self.position=0
            return {'id':'789'}
        if path.endswith('/101'):
            return dict(id='101',contract='BTC_USDT',text=ROW['client_text'],size=1,left=0,
                        status='finished',fill_price='80000')
        return dict(id='789',contract='BTC_USDT',text=self.posts[0]['text'],size=-1,left=0,
                    status='finished',fill_price='80000',is_reduce_only=True,is_close=False)

class ManualRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(recovery.time,'time',return_value=NOW))
        self.con=MagicMock();self.cur=self.con.cursor.return_value.__enter__.return_value
        self.cur.fetchone.return_value=copy.deepcopy(ROW);self.cur.rowcount=1
        self.db=lambda:nullcontext(self.con)
        self.stack.enter_context(patch.object(recovery,'control',return_value={'halted':True}))
        self.c=RecoveryExchange()
    def prepared(self):return recovery.prepare(self.db,EVENT,self.c)
    def enable_mock_submission(self):
        self.stack.enter_context(patch.object(recovery,'require_release'))
        claims=set()
        def claim(con,**kwargs):
            if kwargs['event_id'] in claims:raise LiveSafetyError('claim consumed')
            claims.add(kwargs['event_id'])
            return dict(claimed=True,state='CLAIMED',event_id=kwargs['event_id'],contract=kwargs['contract'],
                        approval_reference=kwargs['approval_reference'],close_client_text=kwargs['close_text'])
        self.stack.enter_context(patch.object(recovery,'claim_one_shot_recovery',side_effect=claim))
        self.consume=self.stack.enter_context(patch('live_emergency_one_shot_claim.consume_one_shot_recovery_claim',return_value=True))
        self.finalize=self.stack.enter_context(patch('live_emergency_one_shot_claim.finalize_one_shot_recovery_claim',return_value=True))
    def test_source_lock_precedes_all_recovery_io(self):
        db=Mock();client=Mock()
        with self.assertRaisesRegex(LiveSafetyError,'SOURCE_LOCKED'):
            recovery.submit_once(db,{},'digest',client)
        db.assert_not_called();client.request.assert_not_called()
    def test_prepare_only_reads_and_preserves_exact_count(self):
        p=self.prepared()
        self.assertEqual(p['review']['close_order']['size'],-1)
        self.assertTrue(p['review']['close_order']['reduce_only'])
        self.assertNotIn('close',p['review']['close_order']);self.assertFalse(p['can_execute'])
        self.assertEqual(self.c.posts,[])
    def test_unknown_entry_id_cannot_be_treated_as_bot_exposure(self):
        self.cur.fetchone.return_value=dict(ROW,entry_order_id=None)
        with self.assertRaisesRegex(LiveSafetyError,'Unknown bot entry ID'):self.prepared()
        self.assertEqual(self.c.posts,[])
    def test_open_protective_trigger_requires_exchange_review(self):
        self.c.triggers=[{'id':123}]
        with self.assertRaisesRegex(LiveSafetyError,'TRIGGERS'):self.prepared()
    def test_exact_close_larger_reviewed_count_and_hedge_mode(self):
        for side,expected in [('LONG',-2),('SHORT',2)]:
            p=dict(ROW,contracts=2,position_mode='dual',side=side)
            order=lifecycle.exact_reduce_only_close_order(p)
            self.assertEqual(order['size'],expected);self.assertTrue(order['reduce_only'])
            self.assertNotIn('auto_size',order)
    def test_success_requires_close_readback_and_persisted_ledger(self):
        p=self.prepared();self.enable_mock_submission()
        result=recovery.submit_once(self.db,p,p['review_digest'],self.c)
        self.assertTrue(result['closed_verified']);self.assertTrue(result['ledger_closed_verified'])
        self.assertTrue(result['halt_active']);self.assertFalse(result['certified'])
        self.assertEqual(len(self.c.posts),1);self.assertEqual(self.c.posts[0]['size'],-1)
        self.consume.assert_called_once();self.finalize.assert_called_once()
    def test_lost_ack_does_not_retry_or_claim_flat(self):
        p=self.prepared();self.enable_mock_submission();self.c.timeout=True
        result=recovery.submit_once(self.db,p,p['review_digest'],self.c)
        self.assertFalse(result['closed_verified']);self.assertEqual(len(self.c.posts),1)
        self.assertTrue(result['requires_manual_exchange_review'])
        with self.assertRaisesRegex(LiveSafetyError,'consumed'):
            recovery.submit_once(self.db,p,p['review_digest'],self.c)
        self.assertEqual(len(self.c.posts),1)
    def test_tampered_and_expired_reviews_cannot_close(self):
        p=self.prepared();self.enable_mock_submission()
        with self.assertRaises(LiveSafetyError):recovery.submit_once(self.db,p,'0'*64,self.c)
        with patch.object(recovery.time,'time',return_value=NOW+30):
            with self.assertRaisesRegex(LiveSafetyError,'expired'):
                recovery.submit_once(self.db,p,p['review_digest'],self.c)
        self.assertEqual(self.c.posts,[])
    def test_position_changes_before_claim_blocks_close(self):
        p=self.prepared();self.enable_mock_submission();self.c.position=2
        with self.assertRaisesRegex(LiveSafetyError,'SIZE'):
            recovery.submit_once(self.db,p,p['review_digest'],self.c)
        self.assertEqual(self.c.posts,[])
    def test_verified_close_but_ledger_write_failure_is_explicit(self):
        p=self.prepared();self.enable_mock_submission();self.cur.rowcount=0
        result=recovery.submit_once(self.db,p,p['review_digest'],self.c)
        self.assertTrue(result['closed_verified']);self.assertFalse(result['ledger_closed_verified'])
        self.assertTrue(result['requires_manual_exchange_review'])
    def test_partial_close_fill_or_residual_position_never_verifies(self):
        p=self.prepared();self.enable_mock_submission()
        original=self.c.request
        def partial(method,path,**kwargs):
            response=original(method,path,**kwargs)
            if method=='GET' and path.endswith('/789'):response['left']=1
            return response
        self.c.request=partial
        result=recovery.submit_once(self.db,p,p['review_digest'],self.c)
        self.assertFalse(result['closed_verified']);self.finalize.assert_not_called()
        self.assertEqual(len(self.c.posts),1)
    def test_review_cannot_be_mutated_to_close_extra_contracts(self):
        p=self.prepared();self.enable_mock_submission()
        p['review']['close_order']['size']=-99
        p['review_digest']=recovery.canonical_digest(p['review'])
        result=recovery.submit_once(self.db,p,p['review_digest'],self.c)
        self.assertFalse(result['closed_verified']);self.assertEqual(self.c.posts,[])
    def test_ui_auth_csrf_and_locked_close(self):
        app=Flask(__name__);app.secret_key='offline-test'
        permitted=Mock(return_value=False);csrf=Mock(return_value=True)
        terminal.register(app,permitted=permitted,guard=lambda:('auth',401),csrf_valid=csrf,connect=self.db)
        c=app.test_client()
        self.assertEqual(c.get('/terminal/recovery').status_code,303)
        self.assertEqual(c.post('/terminal/recovery/submit').status_code,401)
        permitted.return_value=True;csrf.return_value=False
        self.assertEqual(c.post('/terminal/recovery/prepare').status_code,403)
        csrf.return_value=True
        with patch.object(recovery,'submit_once') as submit:
            response=c.post('/terminal/recovery/submit')
        self.assertEqual(response.status_code,423);submit.assert_not_called()

if __name__=='__main__':unittest.main()
