"""Offline matching of Gate.io native SL/TP orders by original exchange order ID."""
import ast
import pathlib
import re
import unittest
from decimal import Decimal, InvalidOperation
source=pathlib.Path(__file__).resolve().parents[1]/"app.py"
tree=ast.parse(source.read_text(encoding="utf8"))
fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=="_five_match_native_protection_ids")
class GateOrderTransportError(Exception): pass
env={"re":re,"Decimal":Decimal,"InvalidOperation":InvalidOperation,"GateOrderTransportError":GateOrderTransportError}
exec(compile(ast.Module(body=[fn],type_ignores=[]),str(source),"exec"),env)
match=env["_five_match_native_protection_ids"]
def order(oid, price, rule, parent="123", status="open"):
    return {"id_string":str(oid),"me_order_id":parent,"status":status,
            "order_type":"close-long-order","initial":{"contract":"BTC_USDT"},
            "trigger":{"price":str(price),"rule":rule}}
class NativeIdentityTests(unittest.TestCase):
    def test_distinct_matching_parent(self):
        result=match("123","BTC_USDT","LONG",900,1100,[order(2,900,2),order(3,1100,1)])
        self.assertEqual((result["sl_order_id"],result["tp_order_id"]),("2","3"))
        self.assertFalse(result["execution_authorized"])
    def test_wrong_parent_or_inactive(self):
        result=match("123","BTC_USDT","LONG",900,1100,
                     [order(2,900,2,parent="other"),order(3,1100,1,status="finished")])
        self.assertFalse(result["coverage_linked"])
    def test_ambiguous_duplicate_rule(self):
        result=match("123","BTC_USDT","LONG",900,1100,
                     [order(2,900,2),order(4,900,2),order(3,1100,1)])
        self.assertFalse(result["coverage_linked"])
    def test_missing_parent_link_is_not_guessed(self):
        result=match("123","BTC_USDT","LONG",900,1100,
                     [order(2,900,2,parent=None),order(3,1100,1,parent=None)])
        self.assertFalse(result["coverage_linked"])
if __name__=="__main__": unittest.main()
