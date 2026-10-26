"""Unit tests for the isolated Gate.io futures order fill parser.

Run: python -m unittest discover -s tests -p 'test_gate_fill_parser.py'
These tests do not call Gate.io, PostgreSQL or submit any orders.
"""
import ast
import pathlib
import unittest
from decimal import Decimal, InvalidOperation

class GateOrderTransportError(Exception):
    pass

module = ast.parse((pathlib.Path(__file__).resolve().parents[1] / 'app.py').read_text(encoding='utf-8'))
node = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == '_gate_order_fill_quantity')
namespace = {'Decimal': Decimal, 'InvalidOperation': InvalidOperation, 'GateOrderTransportError': GateOrderTransportError}
exec(compile(ast.Module(body=[node], type_ignores=[]), 'app.py', 'exec'), namespace)
parse = namespace['_gate_order_fill_quantity']

class GateFillParserTests(unittest.TestCase):
    def test_full_long(self):
        self.assertEqual(parse({'size':10,'left':0,'status':'finished'}), (Decimal('10'),Decimal('10')))
    def test_partial_short(self):
        self.assertEqual(parse({'size':-10,'left':-4,'status':'finished'}), (Decimal('-10'),Decimal('6')))
    def test_open_unfilled(self):
        self.assertEqual(parse({'size':10,'left':10,'status':'open'}), (Decimal('10'),Decimal('0')))
    def test_wrong_left_sign(self):
        with self.assertRaises(GateOrderTransportError): parse({'size':10,'left':-2,'status':'finished'})
    def test_left_exceeds_size(self):
        with self.assertRaises(GateOrderTransportError): parse({'size':10,'left':11,'status':'open'})
    def test_missing_left(self):
        with self.assertRaises(GateOrderTransportError): parse({'size':10,'status':'finished'})
    def test_unknown_status(self):
        with self.assertRaises(GateOrderTransportError): parse({'size':10,'left':0,'status':'cancelled'})
    def test_nonfinite_size(self):
        with self.assertRaises(GateOrderTransportError): parse({'size':'nan','left':0,'status':'finished'})
if __name__ == '__main__': unittest.main()
