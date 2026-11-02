"""Offline tests. No real clients, credentials, database or orders."""
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, Mock, patch

import acceptance_authorization as auth
import live_execution_orchestrator as orch
from live_futures_engine import LiveSafetyError

NOW = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
PLAN = dict(event_id='tv-XRP-5m-example', contract='XRP_USDT', side='LONG',
            timeframe='5m', received_epoch=int(NOW.timestamp()), contracts=1,
            entry='1.3', sl='1.29', tp='1.32', leverage='5', position_mode='dual',
            estimated_risk_usdt='0.02', max_entry_slippage_ratio='0.001')


def connection(claimed=False, revoked=False):
    con = MagicMock()
    con.__enter__.return_value = con
    cur = con.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = dict(
        approval_id='operator-example', event_id=PLAN['event_id'],
        plan_digest=auth.fingerprint(PLAN), starts_at=NOW-timedelta(seconds=5),
        expires_at=NOW+timedelta(seconds=55),
        claimed_at=NOW if claimed else None, revoked=revoked)
    cur.rowcount = 1
    return con


class AuthorizationTests(unittest.TestCase):
    def test_date_is_supplied_not_historical_constant(self):
        for year in (2026, 2027):
            now = NOW.replace(year=year)
            self.assertTrue(auth.validate_scope(PLAN, now, now+timedelta(minutes=1), now))

    def test_expired_future_long_or_naive_window_rejected(self):
        for start,end,now in (
            (NOW-timedelta(minutes=1), NOW, NOW),
            (NOW+timedelta(seconds=1), NOW+timedelta(minutes=1), NOW),
            (NOW, NOW+timedelta(minutes=16), NOW),
            (NOW, NOW+timedelta(minutes=1), NOW.replace(tzinfo=None))):
            with self.assertRaises(LiveSafetyError):
                auth.validate_scope(PLAN,start,end,now)

    def test_size_leverage_risk_and_scope_are_strict(self):
        for key,value in [('contracts',2),('contracts',0.1),('contracts',True),
                          ('estimated_risk_usdt','NaN'),('leverage',6),
                          ('max_entry_slippage_ratio','0.002'),('side','OTHER'),
                          ('contract','EVAA_USDT')]:
            with self.subTest(key=key,value=value), self.assertRaises(LiveSafetyError):
                auth.validate_scope(dict(PLAN,**{key:value}), NOW,NOW+timedelta(minutes=1),NOW)

    def test_exact_signal_and_levels_bound(self):
        for key,value in [('event_id','other-event'),('entry','1.31'),('tp','1.4')]:
            with self.assertRaises(LiveSafetyError):
                auth.require_approval(connection(),'operator-example',dict(PLAN,**{key:value}),claim=True,now=NOW)

    def test_claimed_or_revoked_approval_rejects_new_attempt(self):
        for con in (connection(claimed=True),connection(revoked=True)):
            with self.assertRaises(LiveSafetyError):
                auth.require_approval(con,'operator-example',PLAN,claim=True,now=NOW)

    def test_unclaimed_approval_cannot_dispatch(self):
        with self.assertRaises(LiveSafetyError):
            auth.require_approval(connection(),'operator-example',PLAN,now=NOW)

    def test_claim_uses_row_lock_and_conditional_update(self):
        con=connection()
        auth.require_approval(con,'operator-example',PLAN,claim=True,now=NOW)
        sql=' '.join(str(c) for c in con.cursor.return_value.__enter__.return_value.execute.call_args_list)
        self.assertIn('FOR UPDATE',sql)
        self.assertIn('claimed_at IS NULL',sql)

    def test_source_lock_remains_enforced(self):
        client=Mock()
        with self.assertRaisesRegex(LiveSafetyError,'SOURCE_LOCKED'):
            orch.execute_confirmed_signal(Mock(),PLAN,client,acceptance_approval_id='operator-example')
        client.submit_entry.assert_not_called()

    def test_acceptance_avoids_certificate_but_keeps_shared_execution(self):
        con=connection()
        snap={'positions':[], 'orders':[], 'triggers':[], 'account':{}}
        client=Mock()
        client.snapshot.return_value=snap
        client.submit_entry.return_value={'id':'100'}
        client.submit_trigger.side_effect=[{'id':'200'},{'id':'300'}]
        with ExitStack() as stack:
            for name,value in [('EXCHANGE_WRITE_RELEASE',True)]:
                stack.enter_context(patch.object(orch,name,value))
            for name,value in [('control',{'halted':False}),('read_rows',[]),
                               ('verified_account_equity',(100,100)),
                               ('require_full_entry_readback',('100',snap)),
                               ('evaluate_protection',{'verified':True,'stop_order_id':'200','take_order_id':'300'})]:
                stack.enter_context(patch.object(orch,name,return_value=value))
            for name in ('install','verify_execution_preflight','assert_existing_protected_exposure',
                         'risk_reserve','persist_state','require_linked_trigger_acknowledgments'):
                stack.enter_context(patch.object(orch,name))
            approval=stack.enter_context(patch.object(auth,'require_approval'))
            operator=stack.enter_context(patch('production_bot_control.require_operator_on'))
            result=orch.execute_confirmed_signal(lambda:con,PLAN,client,acceptance_approval_id='operator-example')
            self.assertEqual(result['state'],'PROTECTED')
            self.assertEqual(approval.call_count,2)
            self.assertTrue(approval.call_args_list[0].kwargs['claim'])
            operator.assert_not_called()
            client.submit_entry.assert_called_once_with(PLAN)
            self.assertEqual(client.submit_trigger.call_count,2)


if __name__=='__main__':
    unittest.main()
