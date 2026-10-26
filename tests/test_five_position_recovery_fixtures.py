"""Executable offline emergency-close recovery fixtures.

Imports only pure function AST from app.py; never imports Flask or authenticates
to an exchange. Guards all three position modes and common unsafe conditions.
"""
import ast
import pathlib
import re
import unittest
from decimal import Decimal, InvalidOperation

ROOT = pathlib.Path(__file__).resolve().parents[1]
TREE = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
NAMES = ("_five_recovery_close_shape", "_five_recovery_close_review",
         "_five_recovery_decision")
FUNCTIONS = [n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name in NAMES]
assert len(FUNCTIONS) == len(NAMES)

class GateOrderTransportError(Exception):
    pass

ENV = {"re":re, "Decimal":Decimal, "InvalidOperation":InvalidOperation,
       "GateOrderTransportError": GateOrderTransportError}
exec(compile(ast.Module(body=FUNCTIONS, type_ignores=[]), str(ROOT / "app.py"), "exec"), ENV)

class RecoveryFixtures(unittest.TestCase):
    def position(self, side="LONG", mode="single", qty=3, contract="BTC_USDT"):
        return {"contract":contract,"side":side,"position_mode":mode,
                "size_contracts":qty}

    def test_one_way_close_is_explicit(self):
        p=ENV["_five_recovery_close_review"](self.position(),"BTC_USDT","LONG")
        self.assertTrue(p["payload_preview"]["close"])
        self.assertFalse(p["exchange_write_enabled"])

    def test_hedge_direction_is_unambiguous(self):
        for side,mode,auto in (("LONG","dual_long","close_long"),
                               ("SHORT","dual_short","close_short")):
            with self.subTest(side=side):
                p=ENV["_five_recovery_close_review"](
                    self.position(side=side,mode=mode),"BTC_USDT",side)
                self.assertEqual(p["payload_preview"]["auto_size"],auto)
                self.assertEqual(p["close_orders_submitted"],0)

    def test_no_close_with_wrong_mode_or_identity(self):
        for p,contract,side in (
            (self.position(mode="dual_short"),"BTC_USDT","LONG"),
            (self.position(side="SHORT"),"BTC_USDT","LONG"),
            (self.position(),"ETH_USDT","LONG"),
            (self.position(mode="mystery"),"BTC_USDT","LONG"),
        ):
            with self.subTest(p=p):
                with self.assertRaises(GateOrderTransportError):
                    ENV["_five_recovery_close_review"](p,contract,side)

    def test_no_close_on_bad_quantity(self):
        for qty in (0,-1,0.5,float("nan"),None,"invalid"):
            with self.subTest(qty=qty):
                with self.assertRaises(GateOrderTransportError):
                    ENV["_five_recovery_close_review"](
                        self.position(qty=qty),"BTC_USDT","LONG")

    def test_dispatch_unknown_is_never_retry(self):
        f=ENV["_five_recovery_decision"]
        self.assertEqual(f("DISPATCH_UNCERTAIN",False,False)["recovery_action"],
                         "MANUAL_EXCHANGE_RECONCILIATION")
        self.assertEqual(f("ACKNOWLEDGED",True,False)["recovery_action"],
                         "URGENT_OPERATOR_REVIEW")
        self.assertEqual(f("ACKNOWLEDGED",True,True)["recovery_action"],
                         "NONE_OBSERVED_PROTECTED")

if __name__ == "__main__":
    unittest.main()
