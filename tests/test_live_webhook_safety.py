"""Offline regression checks for stable signal identity and fail-closed execution.

Never imports app, reaches an exchange, or uses credentials.
"""
import ast
import pathlib
import unittest

ROOT=pathlib.Path(__file__).resolve().parents[1]
TREE=ast.parse((ROOT/'app.py').read_text(encoding='utf-8'))
FUNCS={f.name:f for f in TREE.body if isinstance(f,ast.FunctionDef)}

class LiveWebhookSafetyTests(unittest.TestCase):
    def test_stable_tv_event_identity_required(self):
        webhook=FUNCS['webhook']
        source=ast.get_source_segment((ROOT/'app.py').read_text(encoding='utf-8'), webhook)
        self.assertIn("data.get('event_id')", source)
        self.assertIn('valid_tv_id',source)
        self.assertIn('BLOCKED_MISSING_STABLE_EVENT_ID',source)
        self.assertIn('execution_id',source)

    def test_execution_coordinator_cannot_place_orders(self):
        coordinator=FUNCS['_five_position_execution_coordinator']
        calls={n.func.id for n in ast.walk(coordinator)
               if isinstance(n,ast.Call) and isinstance(n.func,ast.Name)}
        forbidden={'_gate_post_transport_implementation',
                   '_gate_send_protected_entry',
                   '_gate_close_existing_oneway_position'}
        self.assertFalse(calls & forbidden)

    def test_dispatch_claim_is_durable_and_single_shot(self):
        text=ast.get_source_segment((ROOT/'app.py').read_text(encoding='utf-8'),
                                    FUNCS['_five_journal_mark_dispatch_uncertain'])
        self.assertIn('pg_advisory_xact_lock',text)
        self.assertIn('DISPATCH_UNCERTAIN',text)
        self.assertIn("row['state']!='PREPARED'",text)

    def test_production_not_part_of_this_offline_suite(self):
        self.assertTrue((ROOT/'.github/workflows/luxalgo-safe-tests.yml').exists())

    def test_schema_ddl_serialized_before_create(self):
        src=ast.get_source_segment((ROOT/'app.py').read_text(encoding='utf-8'),
                                   FUNCS['_ensure_schema'])
        self.assertIn('pg_advisory_xact_lock',src)
        self.assertLess(src.index('pg_advisory_xact_lock'),src.index('connection.execute(DB_SCHEMA)'))

if __name__=='__main__':
    unittest.main()
