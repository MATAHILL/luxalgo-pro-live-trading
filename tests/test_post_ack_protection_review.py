"""Offline post-ack coverage lifecycle fixtures."""
import ast,pathlib,re,unittest
p=pathlib.Path(__file__).resolve().parents[1]/"app.py"
n=next(x for x in ast.parse(p.read_text()).body if isinstance(x,ast.FunctionDef) and x.name=="_five_post_ack_protection_review")
class GateOrderTransportError(Exception):pass
class Psy: Error=Exception
def run(fill,coverage):
 env={"re":re,"GateOrderTransportError":GateOrderTransportError,"psycopg":Psy,
      "_five_journal_read_fill_evidence":lambda _:fill,
      "_five_reconcile_native_protection":lambda _:coverage}
 exec(compile(ast.Module(body=[n],type_ignores=[]),str(p),"exec"),env)
 return env["_five_post_ack_protection_review"]("event-01")
class Test(unittest.TestCase):
 def test_unconfirmed_fill_blocks(self):
  x=run({"fill_verified":False},{"coverage_verified":True})
  self.assertFalse(x["protection_verified"]);self.assertTrue(x["requires_attention"])
 def test_unprotected_fill_blocks(self):
  x=run({"fill_verified":True},{"coverage_verified":False})
  self.assertFalse(x["protection_verified"]);self.assertEqual(x["orders_placed"],0)
 def test_verified_never_authorizes_trading(self):
  x=run({"fill_verified":True},{"coverage_verified":True,"state":"EXCHANGE_PROTECTION_OBSERVED"})
  self.assertTrue(x["protection_verified"]);self.assertFalse(x["execution_authorized"])
if __name__=="__main__":unittest.main()
