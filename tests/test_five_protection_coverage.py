"""Functional, fully offline native protection coverage tests."""
import ast
import pathlib
import re
import unittest

source=pathlib.Path(__file__).resolve().parents[1]/"app.py"
tree=ast.parse(source.read_text(encoding="utf-8"))
fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef)
        and n.name=="_five_protection_coverage_check")
class GateOrderTransportError(Exception): pass

class CoverageTests(unittest.TestCase):
    def check(self,fill,trigger):
        env={"re":re,"GateOrderTransportError":GateOrderTransportError,
             "_five_journal_read_fill_evidence":lambda event:fill,
             "_five_verify_native_tpsl":lambda event:trigger}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),str(source),"exec"),env)
        return env["_five_protection_coverage_check"]("event-001")

    def test_missing_fill_fails_closed(self):
        result=self.check({"state":"FILL_NOT_CONFIRMED","fill_verified":False},{})
        self.assertFalse(result["coverage_verified"])
        self.assertEqual(result["state"],"FILL_UNVERIFIED")

    def test_missing_or_invalid_trigger_ids_fail_closed(self):
        for ids in ({},{"sl_order_id":"100","tp_order_id":"100"},
                    {"sl_order_id":"abc","tp_order_id":"102"}):
            with self.subTest(ids=ids):
                result=self.check({"fill_verified":True},
                    {"protection_verified":True,**ids})
                self.assertFalse(result["coverage_verified"])

    def test_inactive_protection_fails_closed(self):
        result=self.check({"fill_verified":True},
            {"protection_verified":False,"reason":"TRIGGER_NOT_ACTIVE_OR_MISMATCHED"})
        self.assertTrue(result["requires_operator_attention"])
        self.assertFalse(result["coverage_verified"])

    def test_distinct_active_triggers_are_observed_but_not_authorized(self):
        result=self.check({"fill_verified":True},
            {"protection_verified":True,"sl_order_id":"101","tp_order_id":"102"})
        self.assertTrue(result["coverage_verified"])
        self.assertFalse(result["execution_authorized"])
        self.assertEqual(result["orders_placed"],0)

if __name__=="__main__":
    unittest.main()
