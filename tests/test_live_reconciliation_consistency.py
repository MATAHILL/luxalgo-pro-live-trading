"""Regression checks for exposure identity; no exchange or database writes."""
import unittest
from unittest.mock import Mock, patch
import live_execution_coordinator as coordinator
from live_futures_engine import build_protective_trigger, reconcile_ledger, LiveSafetyError
from live_execution_orchestrator import assert_existing_protected_exposure


class ReconciliationConsistency(unittest.TestCase):
    def setUp(self):
        self.row = dict(event_id='audit-event-123', client_text='t-lxp-audit',
                        contract='BTC_USDT', side='LONG', state='PROTECTED',
                        contracts=2, entry_price='80000', stop_loss='79000',
                        take_profit='82000', position_mode='single',
                        stop_order_id='101', take_order_id='102')
        plan = dict(contract='BTC_USDT', side='LONG', entry='80000', sl='79000',
                    tp='82000', position_mode='single')
        self.book = [dict(build_protective_trigger(plan,k), id_string=str(i),status='open')
                     for k,i in [('SL',101),('TP',102)]]
        self.position = dict(contract='BTC_USDT', size='2')
        self.snapshot = dict(positions=[self.position], triggers=self.book, orders=[],
                             source='FIXTURE',fetched_at=1800000000)

    def reconcile(self, row=None, position=None):
        return reconcile_ledger([row or self.row], [position or self.position], self.book)

    def test_exact_linked_exposure_passes(self):
        self.assertFalse(self.reconcile()['requires_halt'])
        self.assertEqual(assert_existing_protected_exposure([self.row],self.snapshot)
                         ['protected_open_positions'],1)

    def test_changed_quantity_and_direction_halt(self):
        for size in ('1','3','-2'):
            position=dict(self.position,size=size)
            with self.subTest(size=size):
                self.assertTrue(self.reconcile(position=position)['requires_halt'])
                with self.assertRaises(LiveSafetyError):
                    assert_existing_protected_exposure([self.row],dict(self.snapshot,positions=[position]))

    def test_same_prices_with_different_protection_ids_halt(self):
        self.assertTrue(self.reconcile(row=dict(self.row,stop_order_id='999'))['requires_halt'])

    def test_pending_lifecycle_never_displays_verified_protection(self):
        client=Mock();client.snapshot.return_value=self.snapshot
        with patch.object(coordinator,'install'), patch.object(coordinator,'read_rows',
             return_value=[dict(self.row,state='SENDING')]), patch.object(coordinator,'control',
             return_value=dict(halted=False,reason='fixture')):
            result=coordinator.inspect(object(),client)
        self.assertTrue(result['halted'])
        self.assertEqual(result['positions'][0]['protection_status'],'NOT_VERIFIED')

    def test_duplicate_active_contract_halts(self):
        result=reconcile_ledger([self.row,dict(self.row,event_id='other-event')],
                                [self.position],self.book)
        self.assertTrue(result['requires_halt'])

    def test_historical_closed_trade_does_not_block_new_tracked_trade(self):
        history=dict(self.row,event_id='old-event',state='CLOSED_VERIFIED')
        result=reconcile_ledger([history,self.row],[self.position],self.book)
        self.assertFalse(result['requires_halt'])

    def test_missing_protected_position_blocks_new_entry(self):
        with self.assertRaises(LiveSafetyError):
            assert_existing_protected_exposure([self.row],dict(self.snapshot,positions=[]))

    def test_string_only_trigger_ids_are_recognized(self):
        client=Mock();client.snapshot.return_value=self.snapshot
        with patch.object(coordinator,'install'), patch.object(coordinator,'read_rows',
             return_value=[self.row]), patch.object(coordinator,'control',
             return_value=dict(halted=False,reason='fixture')):
            result=coordinator.inspect(object(),client)
        self.assertEqual(result['untracked_open_triggers'],0)
        self.assertEqual(result['positions'][0]['protection_status'],'EXCHANGE_VERIFIED')
