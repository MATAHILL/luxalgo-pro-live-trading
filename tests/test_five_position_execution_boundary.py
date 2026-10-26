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

if __name__=='__main__':
    unittest.main()
