import ast
import pathlib
import unittest

SOURCE = (pathlib.Path(__file__).resolve().parents[1] / 'app.py').read_text(encoding='utf8')
MODULE = ast.parse(SOURCE)
FUNCTION = next(n for n in MODULE.body if isinstance(n,ast.FunctionDef)
                and n.name == 'terminal_gate_testnet_check')
BODY = ast.get_source_segment(SOURCE,FUNCTION)

class TestGateTestnetReadOnly(unittest.TestCase):
    def test_separate_keys_and_testnet_host(self):
        self.assertIn('GATE_TESTNET_API_KEY',BODY)
        self.assertIn('GATE_TESTNET_API_SECRET',BODY)
        self.assertIn('https://api-testnet.gateapi.io',BODY)
        self.assertNotIn("os.getenv('GATE_API_KEY'",BODY)
        self.assertNotIn("os.getenv('GATE_API_SECRET'",BODY)
    def test_no_exchange_write(self):
        self.assertIn("method='GET'",BODY)
        self.assertNotIn("method='POST'",BODY)
        self.assertIn("'orders_enabled':False",BODY.replace('orders_enabled=False',"'orders_enabled':False"))

if __name__ == '__main__':
    unittest.main()
