"""Real disposable PostgreSQL claims; no exchange credentials or order calls.

Set TEST_ACCEPTANCE_DATABASE_URL to a local disposable test database.
Every test gets a new schema. Remote hosts are refused.
"""
import os
import unittest
import uuid
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg import sql
import controlled_acceptance as acceptance
from live_execution_coordinator import install
from live_futures_engine import LiveSafetyError
from test_live_controlled_acceptance import NOW, SCOPE, SIGNAL, Exchange

DSN=os.getenv('TEST_ACCEPTANCE_DATABASE_URL','')


@unittest.skipUnless(DSN,'Disposable local PostgreSQL URL not configured')
class AcceptancePostgresTests(unittest.TestCase):
    def setUp(self):
        host=conninfo_to_dict(DSN).get('host','')
        if host not in ('localhost','127.0.0.1','::1') and not host.startswith('/tmp/'):
            raise RuntimeError('Only disposable local PostgreSQL is permitted')
        self.schema='acceptance_test_'+uuid.uuid4().hex
        with psycopg.connect(DSN) as con:
            con.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.addCleanup(self.drop_schema)
        with self.connection() as con:
            install(con)
            con.execute(acceptance.SCHEMA)
            # Test schema only; never the live production control.
            con.execute('UPDATE luxalgo_prod_execution_control SET halted=FALSE WHERE id=1')
        self.preview=acceptance.prepare(SCOPE,SIGNAL,Exchange(),now=NOW)
        self.review=self.preview['review'];self.digest=self.preview['review_digest']
        self.release=patch.object(acceptance,'require_release');self.release.start()
        self.addCleanup(self.release.stop)
        self.clock=patch.object(acceptance.time,'time',return_value=NOW);self.clock.start()
        self.addCleanup(self.clock.stop)

    def connection(self):
        return psycopg.connect(DSN,row_factory=dict_row,
            options='-c search_path='+self.schema+' -c statement_timeout=5000 -c lock_timeout=4000')

    def drop_schema(self):
        with psycopg.connect(DSN) as con:
            con.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))

    def counts(self):
        with self.connection() as con:
            claim=con.execute('SELECT COUNT(*) AS n FROM luxalgo_single_acceptance').fetchone()['n']
            ledger=con.execute('SELECT COUNT(*) AS n FROM luxalgo_prod_execution_ledger').fetchone()['n']
        return claim,ledger

    def test_real_schema_claim_and_production_reservation_commit_together(self):
        acceptance.claim_and_reserve(self.connection,self.review,self.digest)
        self.assertEqual(self.counts(),(1,1))
        with self.connection() as con:
            row=con.execute('SELECT * FROM luxalgo_single_acceptance').fetchone()
            ledger=con.execute('SELECT * FROM luxalgo_prod_execution_ledger').fetchone()
        self.assertEqual(row['review_digest'],self.digest)
        self.assertEqual(row['review']['scope']['scope_id'],SCOPE['scope_id'])
        self.assertEqual(row['state'],'SENDING');self.assertEqual(ledger['state'],'SENDING')
        self.assertEqual(ledger['contracts'],1)

    def test_competing_connections_only_one_claim_commits(self):
        def claim(_):
            try:
                acceptance.claim_and_reserve(self.connection,self.review,self.digest)
                return 'committed'
            except LiveSafetyError:
                return 'rejected'
        with ThreadPoolExecutor(max_workers=2) as pool:
            result=list(pool.map(claim,range(2)))
        self.assertCountEqual(result,['committed','rejected'])
        self.assertEqual(self.counts(),(1,1))

    def test_transaction_failure_rolls_back_both_records_before_any_send(self):
        @contextmanager
        def failing_commit():
            with self.connection() as con:
                yield con
                raise RuntimeError('Injected failure before commit')
        with self.assertRaises(RuntimeError):
            acceptance.claim_and_reserve(failing_commit,self.review,self.digest)
        self.assertEqual(self.counts(),(0,0))
        acceptance.claim_and_reserve(self.connection,self.review,self.digest)
        self.assertEqual(self.counts(),(1,1))

    def test_reconnect_does_not_reset_consumed_scope_or_event(self):
        acceptance.claim_and_reserve(self.connection,self.review,self.digest)
        # Simulate separately verified closure, so the persistent one-use
        # constraint, rather than an open ledger row, must block the replay.
        with self.connection() as con:
            con.execute("UPDATE luxalgo_prod_execution_ledger SET state='CLOSED_VERIFIED'")
        with self.assertRaisesRegex(LiveSafetyError,'consumed'):
            acceptance.claim_and_reserve(self.connection,self.review,self.digest)
        self.assertEqual(self.counts(),(1,1))

    def test_active_halt_prevents_all_claim_and_ledger_inserts(self):
        with self.connection() as con:
            con.execute('UPDATE luxalgo_prod_execution_control SET halted=TRUE')
        with self.assertRaisesRegex(LiveSafetyError,'HALT'):
            acceptance.claim_and_reserve(self.connection,self.review,self.digest)
        self.assertEqual(self.counts(),(0,0))

    def recovery_execution(self):
        acceptance.claim_and_reserve(self.connection,self.review,self.digest)
        with self.connection() as con:
            con.execute("UPDATE luxalgo_prod_execution_ledger SET state='HALTED',entry_order_id='101'")
            con.execute('UPDATE luxalgo_prod_execution_control SET halted=TRUE')

    def test_recovery_claim_consumed_and_verified_close_journal_real_sql(self):
        import manual_recovery as recovery
        from test_live_manual_recovery import RecoveryExchange,EVENT
        self.recovery_execution()
        client=RecoveryExchange()
        prepared=recovery.prepare(self.connection,EVENT,client)
        with patch.object(recovery,'require_release'):
            result=recovery.submit_once(self.connection,prepared,prepared['review_digest'],client)
        self.assertTrue(result['closed_verified']);self.assertTrue(result['ledger_closed_verified'])
        self.assertEqual(len(client.posts),1)
        with self.connection() as con:
            self.assertEqual(con.execute('SELECT state FROM luxalgo_prod_execution_ledger').fetchone()['state'],'CLOSED_VERIFIED')
            self.assertEqual(con.execute('SELECT state FROM luxalgo_prod_emergency_close_claims').fetchone()['state'],'FLAT_READBACK_VERIFIED')
            self.assertTrue(con.execute('SELECT halted FROM luxalgo_prod_execution_control').fetchone()['halted'])

    def test_competing_recovery_claim_consumers_only_one_can_dispatch(self):
        from live_emergency_one_shot_claim import claim_one_shot_recovery,consume_one_shot_recovery_claim
        from live_execution_orchestrator import exact_reduce_only_close_order
        self.recovery_execution()
        plan=self.review['plan'];close=exact_reduce_only_close_order(plan)
        with self.connection() as con:
            claim=claim_one_shot_recovery(con,event_id=plan['event_id'],contract=plan['contract'],
                approval_reference='offline-owner-scope-test',close_text=close['text'])
        def consume(_):
            try:
                consume_one_shot_recovery_claim(self.connection,claim)
                return 'consumed'
            except LiveSafetyError:return 'rejected'
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes=list(pool.map(consume,range(2)))
        self.assertCountEqual(outcomes,['consumed','rejected'])


if __name__=='__main__': unittest.main()
