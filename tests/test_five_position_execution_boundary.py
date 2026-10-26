"""Offline source-level execution-boundary regression checks.

These tests never import the application, access credentials, or contact Gate.io.
They verify protective entry construction and the five-position coordinator
cannot bypass the missing automated recovery implementation.
"""
import ast
import pathlib
import unittest

ROOT=pathlib.Path(__file__).resolve().parents[1]
SRC=(ROOT/'app.py').read_text(encoding='utf-8')
TREE=ast.parse(SRC)
FUNCTIONS={node.name:node for node in TREE.body if isinstance(node,ast.FunctionDef)}

def source(name):
    return ast.get_source_segment(SRC,FUNCTIONS[name])

class FivePositionOrderBoundary(unittest.TestCase):
    def test_entry_payload_carries_both_native_triggers(self):
        src=source('_gate_protected_entry_payload')
        self.assertIn("'tpsl_tp_trigger_price'",src)
        self.assertIn("'tpsl_sl_trigger_price'",src)
        self.assertIn("'tif':'ioc'",src)
        self.assertIn("'price':'0'",src)

    def test_post_does_not_claim_protection_from_ack(self):
        src=source('_gate_post_transport_implementation')
        self.assertIn("'protection_verified':False",src)
        self.assertIn('ACK_UNKNOWN_DO_NOT_RETRY',src)

    def test_five_position_coordinator_cannot_submit_live_order(self):
        coordinator=FUNCTIONS['_five_position_execution_coordinator']
        invoked={node.func.id for node in ast.walk(coordinator)
                 if isinstance(node,ast.Call) and isinstance(node.func,ast.Name)}
        self.assertNotIn('_gate_post_transport_implementation',invoked)
        self.assertNotIn('_gate_send_protected_entry',invoked)

    def test_missing_exchange_recovery_still_blocks_execution(self):
        src=source('_five_position_execution_coordinator')
        self.assertIn('DISPATCH_NOT_ARMED',src)
        self.assertIn('Automated emergency close is unverified',src)

    def test_order_ack_requires_exchange_id_and_direction(self):
        src=source('_five_journal_verify_exchange_order')
        self.assertIn("order.get('contract')",src)
        self.assertIn("order.get('text')",src)
        self.assertIn("order['size']",src)
        self.assertIn("order.get('reduce_only') is True",src)

    def test_recovery_audit_blocks_unreviewed_sixth_journal(self):
        src=source('_five_position_recovery_audit')
        self.assertIn('outstanding > limit',src)
        self.assertIn('BLOCKED_UNAUDITED_EXPOSURE',src)
        self.assertIn("'close_orders_submitted':0",src)

    def test_unresolved_exchange_dispatch_prevents_next_entry(self):
        src=source('_five_position_entry_preflight')
        self.assertIn('DISPATCH_UNCERTAIN',src)
        self.assertIn('PROTECTION_UNVERIFIED',src)
        self.assertIn('RECOVERY_REQUIRED',src)
        self.assertIn('if unresolved:',src)
        self.assertIn('entry blocked',src)

    def test_protection_requires_explicit_close_position_direction(self):
        src=source('_five_verify_native_tpsl')
        self.assertIn("initial.get('auto_size')",src)
        self.assertIn("'close_long' if row['side']=='LONG' else 'close_short'",src)

    def test_fill_reconciliation_checks_client_identity_and_exact_quantity(self):
        src=source('_five_journal_read_fill_evidence')
        self.assertIn("order.get('text')!=row['client_order_text']",src)
        self.assertIn("order.get('reduce_only') is True",src)
        self.assertIn('observed==filled',src)
        self.assertIn("order.get('status')=='finished'",src)

    def test_coordinator_validates_dispatch_envelope_without_submitting(self):
        src=source('_five_position_execution_coordinator')
        self.assertIn('_five_dispatch_plan(event_id,preview)',src)
        self.assertIn('BLOCKED_DISPATCH_ENVELOPE_INVALID',src)
        self.assertIn("'exchange_write_enabled':False",src)

    def test_dispatch_plan_is_non_executable(self):
        src=source('_five_dispatch_plan')
        self.assertIn('READY_FOR_OFFLINE_REVIEW',src)
        self.assertIn("'orders_placed':0",src)
        self.assertIn("'exchange_write_enabled':False",src)
        invoked={node.func.id for node in ast.walk(FUNCTIONS['_five_dispatch_plan'])
                 if isinstance(node,ast.Call) and isinstance(node.func,ast.Name)}
        self.assertNotIn('_gate_post_transport_implementation',invoked)
        self.assertNotIn('_five_journal_mark_dispatch_uncertain',invoked)

    def test_dispatch_rejects_wrong_direction_native_protection(self):
        src=source('_five_dispatch_plan')
        self.assertIn('Native SL/TP direction mismatch',src)
        self.assertIn('stop<entry<take',src)
        self.assertIn('take<entry<stop',src)
        self.assertIn('Invalid dispatch contract',src)

    def test_atomic_claim_excludes_unresolved_dispatch(self):
        src=source('_five_journal_mark_dispatch_uncertain')
        self.assertIn('pg_advisory_xact_lock',src)
        self.assertIn("'DISPATCH_UNCERTAIN','ACKNOWLEDGED'",src)
        self.assertIn("'PROTECTION_UNVERIFIED','RECOVERY_REQUIRED'",src)
        self.assertLess(src.index("if cur.fetchone()['n']:"),src.index("state='DISPATCH_UNCERTAIN'"))

    def test_recovery_classifier_is_fail_closed(self):
        src=source('_five_recovery_decision')
        self.assertIn('ORDER_ACK_UNKNOWN_DO_NOT_RETRY',src)
        self.assertIn('URGENT_OPERATOR_REVIEW',src)
        self.assertIn('HALT_AND_RECONCILE',src)
        audit=source('_five_position_recovery_audit')
        self.assertIn('_five_recovery_decision(',audit)

    def test_live_sender_is_hard_disabled_until_certified(self):
        src=source('_five_submit_order_once')
        self.assertIn('FIVE_POSITION_EXECUTION_NOT_CERTIFIED',src)
        self.assertLess(src.index('FIVE_POSITION_EXECUTION_NOT_CERTIFIED'),
                        src.index('_five_journal_mark_dispatch_uncertain'))
        self.assertLess(src.index('_five_journal_mark_dispatch_uncertain'),
                        src.index('_gate_post_transport_implementation'))
        self.assertIn('ACK_UNKNOWN_DO_NOT_RETRY',src)
        self.assertIn('_five_journal_verify_exchange_order',src)
        self.assertIn('FIVE_POSITION_EXECUTION_CERTIFIED',SRC)

    def test_recovery_errors_require_operator_attention(self):
        src=source('_five_position_recovery_audit')
        self.assertIn("'recovery_action']='HALT_AND_RECONCILE'",src)
        self.assertIn("'needs_operator_attention':unsafe",src)
        self.assertIn("'safe_to_enable_new_entries':False",src)

    def test_native_protection_observation_not_authorization(self):
        src=source('_five_verify_native_tpsl')
        self.assertIn("'execution_authorized':False",src)

    def test_recovery_close_preview_is_mode_specific_and_never_dispatches(self):
        src=source('_five_recovery_close_shape')
        self.assertIn("ACCOUNT_MODE_UNVERIFIED",src)
        self.assertIn("HEDGE_CLOSE_SIDE_MISMATCH",src)
        self.assertIn("order['close']=True",src)
        self.assertIn("'close_long' if side=='LONG' else 'close_short'",src)
        self.assertIn("'close_orders_submitted':0",src)
        self.assertIn("'exchange_write_enabled':False",src)
        calls={node.func.id for node in ast.walk(FUNCTIONS['_five_recovery_close_shape'])
               if isinstance(node,ast.Call) and isinstance(node.func,ast.Name)}
        self.assertNotIn('_gate_v4_authenticated_request',calls)

    def test_close_review_requires_exact_contract_side_and_size(self):
        src=source('_five_recovery_close_review')
        self.assertIn('RECOVERY_POSITION_IDENTITY_MISMATCH',src)
        self.assertIn('RECOVERY_POSITION_UNAVAILABLE',src)
        self.assertIn('RECOVERY_SIZE_INVALID',src)
        self.assertIn('amount.to_integral_value()',src)
        self.assertIn('_five_recovery_close_shape(',src)

    def test_exchange_ack_uses_id_string_fallback(self):
        src=source('_five_journal_verify_exchange_order')
        self.assertIn("order.get('id_string') or order.get('id')",src)

    def test_native_tpsl_validates_oneway_and_hedge_closing_modes(self):
        src=source('_five_verify_native_tpsl')
        self.assertIn('_gate_read_only_positions()',src)
        self.assertIn("mode=='single' and initial.get('close') is not True",src)
        self.assertIn("mode!='single' and str(initial.get('auto_size')",src)
        self.assertIn('POSITION_MODE_UNVERIFIED',src)
        self.assertIn('POSITION_MODE_SIDE_MISMATCH',src)

if __name__=='__main__':
    unittest.main()
