"""Offline journal compare-and-set contract tests; no exchange operations."""
import unittest
from contextlib import nullcontext
from unittest.mock import MagicMock, Mock
from live_execution_orchestrator import persist_state, require_full_entry_readback, require_linked_trigger_acknowledgments
from live_futures_engine import LiveSafetyError, AckUnknown


class JournalTransitions(unittest.TestCase):
    def connection(self, affected=1):
        con=MagicMock()
        cur=con.cursor.return_value.__enter__.return_value
        cur.rowcount=affected
        return lambda:nullcontext(con),cur

    def test_every_milestone_requires_exact_predecessor(self):
        for state, previous, kwargs in (
            ('SENDING','RESERVED',{}),
            ('ACK_UNKNOWN','SENDING',{}),
            ('ENTRY_CONFIRMED','SENDING',{'entry_order_id':'123'}),
            ('PROTECTION_PENDING','ENTRY_CONFIRMED',{}),
            ('PROTECTED','PROTECTION_PENDING',{'stop_order_id':'124','take_order_id':'125'}),
        ):
            with self.subTest(state=state):
                connect,cur=self.connection()
                self.assertEqual(persist_state(connect,'event123',state,**kwargs),state)
                sql,params=cur.execute.call_args.args
                self.assertIn('WHERE event_id=%s AND state=%s',sql)
                self.assertEqual(params[6],previous)

    def test_stale_worker_rejected_instead_of_overwriting_halt(self):
        connect,cur=self.connection(affected=0)
        with self.assertRaisesRegex(LiveSafetyError,'stale state'):
            persist_state(connect,'event123','PROTECTED',stop_order_id='124',take_order_id='125')
        self.assertEqual(cur.execute.call_count,1)

    def test_closed_and_halted_cannot_be_claimed_by_generic_writer(self):
        connect=Mock()
        for state in ('CLOSED_VERIFIED','HALTED','RESERVED','unknown'):
            with self.subTest(state=state),self.assertRaises(LiveSafetyError):
                persist_state(connect,'event123',state)
        connect.assert_not_called()

    def test_protected_requires_distinct_valid_trigger_ids(self):
        connect=Mock()
        for stop,take in ((None,None),('1','1'),('bad','2'),('0','2')):
            with self.subTest(stop=stop,take=take),self.assertRaises(LiveSafetyError):
                persist_state(connect,'event123','PROTECTED',stop_order_id=stop,take_order_id=take)
        connect.assert_not_called()

    def test_entry_requires_numeric_exchange_identity(self):
        connect=Mock()
        with self.assertRaises(LiveSafetyError):
            persist_state(connect,'event123','ENTRY_CONFIRMED')
        connect.assert_not_called()

    def test_existing_order_identity_is_not_silently_replaced(self):
        connect,cur=self.connection()
        persist_state(connect,'event123','ENTRY_CONFIRMED',entry_order_id='123')
        sql,params=cur.execute.call_args.args
        self.assertIn('entry_order_id IS NULL OR %s IS NULL OR entry_order_id=%s',sql)
        self.assertEqual(params[7:9],('123','123'))

    def test_protection_acknowledgments_must_match_get_ids(self):
        checked=dict(verified=True,stop_order_id='101',take_order_id='102')
        require_linked_trigger_acknowledgments({'id':101},{'id_string':'102'},checked)
        for stop,take in (({'id':999},{'id':102}),({'id':101},{'id':101}),({},{})):
            with self.subTest(stop=stop),self.assertRaises(AckUnknown):
                require_linked_trigger_acknowledgments(stop,take,checked)

    def test_excessive_fill_slippage_is_not_certified(self):
        plan=dict(contract='BTC_USDT',side='LONG',contracts=1,client_text='t-audit',
                  entry='80000',sl='79000',tp='82000',max_entry_slippage_ratio='0.001')
        client=Mock()
        client.request.return_value=dict(id=123,text='t-audit',contract='BTC_USDT',
                                         size=1,left=0,status='finished',fill_price='80100')
        with self.assertRaisesRegex(AckUnknown,'slippage'):
            require_full_entry_readback(client,plan,{'id':123})
        client.snapshot.assert_not_called()

    def test_wrong_original_quantity_cannot_pass_as_exact_fill(self):
        plan=dict(contract='BTC_USDT',side='LONG',contracts=1,client_text='t-audit')
        client=Mock()
        client.request.return_value=dict(id=123,text='t-audit',contract='BTC_USDT',
                                         size=2,left=1,status='finished',fill_price='80000')
        with self.assertRaises(AckUnknown):
            require_full_entry_readback(client,plan,{'id':123})
        client.snapshot.assert_not_called()
