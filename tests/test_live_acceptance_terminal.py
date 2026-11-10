"""Signed-in acceptance interface; no real network, credentials or orders."""
import copy
import unittest
from datetime import datetime,timezone
from unittest.mock import MagicMock,Mock,patch
from contextlib import nullcontext
from flask import Flask
import acceptance_terminal as terminal
import controlled_acceptance as execution
from test_live_controlled_acceptance import NOW,SCOPE,SIGNAL,Exchange


class AcceptanceTerminalTests(unittest.TestCase):
    def setUp(self):
        self.app=Flask(__name__);self.app.secret_key='offline-session-key'
        self.permitted=Mock(return_value=True);self.csrf=Mock(return_value=True)
        self.con=MagicMock();self.cur=self.con.cursor.return_value.__enter__.return_value
        self.cur.fetchall.return_value=[]
        self.connect=Mock(side_effect=lambda:nullcontext(self.con))
        terminal.register(self.app,permitted=self.permitted,csrf_valid=self.csrf,
                          guard=lambda:('authentication required',401),connect=self.connect)
        self.client=self.app.test_client()
        with self.client.session_transaction() as session:
            session['csrf_token']='test-session-csrf'
        self.row=dict(event_id='a'*20,symbol='BTCUSDT',side='LONG',timeframe='5m',
            level_status='VALID',order_type='MARKET',source_bar_id=SIGNAL['source_bar_id'],
            received_at=datetime.fromtimestamp(NOW-1,timezone.utc),entry_price='80000',
            stop_loss='79900',take_profit='80200')
        self.fields=dict(event_id='a'*20,max_margin='1.70',max_notional='8.30',
                         max_modeled_loss='0.10',leverage='5')

    def test_signin_and_csrf_before_exchange_or_execution(self):
        self.permitted.return_value=False
        self.assertEqual(self.client.get('/terminal/acceptance').status_code,303)
        for path in ('prepare','submit'):
            self.assertEqual(self.client.post('/terminal/acceptance/'+path).status_code,401)
        self.connect.assert_not_called()
        self.permitted.return_value=True;self.csrf.return_value=False
        for path in ('prepare','submit'):
            self.assertEqual(self.client.post('/terminal/acceptance/'+path).status_code,403)
        self.connect.assert_not_called()

    def test_locked_page_cannot_submit(self):
        response=self.client.get('/terminal/acceptance')
        self.assertIn(b'Live submission locked',response.data)
        with patch.object(execution,'submit_once') as submit:
            response=self.client.post('/terminal/acceptance/submit',data={'acknowledge':'one-real-money-test'})
        self.assertEqual(response.status_code,423);submit.assert_not_called()
        self.assertEqual(response.headers['Cache-Control'],'private, no-store')

    def test_prepare_uses_stored_signal_and_saves_session_bound_review(self):
        self.cur.fetchone.return_value=self.row
        client=Exchange()
        with (patch.object(terminal,'GateLiveClient',return_value=client),
              patch.object(terminal.time,'time',return_value=NOW)):
            with self.client.session_transaction() as session:
                session['csrf_token']='test-session-csrf'
            response=self.client.post('/terminal/acceptance/prepare',data=dict(self.fields,entry='1',side='SHORT'))
        self.assertEqual(response.status_code,200)
        self.assertIn(b'Submit one reviewed test',response.data);self.assertIn(b'disabled',response.data)
        self.assertEqual(client.entries,0)
        inserts=[call for call in self.con.execute.call_args_list if 'INSERT INTO luxalgo_acceptance_reviews' in call.args[0]]
        self.assertEqual(len(inserts),1)
        import hashlib,json
        params=inserts[0].args[1]
        self.assertEqual(params[1],hashlib.sha256(b'test-session-csrf').hexdigest())
        stored=json.loads(params[2]);self.assertEqual(stored['review']['plan']['entry'],'80000')
        self.assertEqual(stored['review']['plan']['side'],'LONG')

    def test_missing_stored_event_or_stale_signal_no_review(self):
        self.cur.fetchone.return_value=None
        response=self.client.post('/terminal/acceptance/prepare',data=self.fields)
        self.assertEqual(response.status_code,422)
        self.con.execute.assert_not_called()

    def test_expired_or_other_session_review_never_executes(self):
        self.cur.fetchone.return_value=None
        with (patch.object(execution,'require_release'),patch.object(execution,'submit_once') as submit):
            response=self.client.post('/terminal/acceptance/submit',data=dict(
                acknowledge='one-real-money-test',review_id='b'*32,review_digest='c'*64))
        self.assertEqual(response.status_code,409);submit.assert_not_called()
        query=self.cur.execute.call_args_list[0]
        self.assertIn('owner_hash=%s',query.args[0]);self.assertIn('expires_epoch>%s',query.args[0])

    def test_explicit_ack_required_even_if_release_present(self):
        with patch.object(execution,'require_release'),patch.object(execution,'submit_once') as submit:
            response=self.client.post('/terminal/acceptance/submit',data=dict(review_id='b'*32,review_digest='c'*64))
        self.assertEqual(response.status_code,422);submit.assert_not_called()

    def test_server_side_review_loaded_for_manual_submit_boundary(self):
        prepared=execution.prepare(SCOPE,SIGNAL,Exchange(),now=NOW)
        self.cur.fetchone.side_effect=[{'prepared':prepared},self.row]
        with (patch.object(execution,'require_release'),
              patch.object(execution,'submit_once',return_value={'state':'SIMULATED_REVIEW_REQUIRED'}) as submit):
            response=self.client.post('/terminal/acceptance/submit',data=dict(
                acknowledge='one-real-money-test',review_id='b'*32,review_digest=prepared['review_digest']))
        self.assertEqual(response.status_code,200);submit.assert_called_once()
        self.assertEqual(submit.call_args.args[1],prepared)
        self.assertEqual(submit.call_args.args[3]['entry'],'80000')


if __name__=='__main__':unittest.main()
