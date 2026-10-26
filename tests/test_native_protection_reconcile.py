"""Runtime fixtures for native protection reconciliation; no exchange access."""
import ast, pathlib, re, unittest
p=pathlib.Path(__file__).resolve().parents[1]/"app.py"
tree=ast.parse(p.read_text(encoding="utf8"))
f=next(x for x in tree.body if isinstance(x,ast.FunctionDef) and x.name=="_five_reconcile_native_protection")
class GateOrderTransportError(Exception): pass
class DummyPsycopg:
    Error=Exception
def run(linked,checked):
    env={"re":re,"GateOrderTransportError":GateOrderTransportError,
         "psycopg":DummyPsycopg,
         "_five_persist_verified_protection_ids":lambda _:linked,
         "_five_protection_coverage_check":lambda _:checked}
    exec(compile(ast.Module(body=[f],type_ignores=[]),str(p),"exec"),env)
    return env["_five_reconcile_native_protection"]("signal-001")
class Tests(unittest.TestCase):
    def test_unlinked_never_claims_protection(self):
        x=run({"ids_persisted":False,"reason":"MISSING_LINKAGE"},{})
        self.assertFalse(x["coverage_verified"])
        self.assertEqual(x["orders_placed"],0)
    def test_linked_but_trigger_missing_still_fails(self):
        x=run({"ids_persisted":True},{"state":"NATIVE_SL_TP_UNVERIFIED","coverage_verified":False})
        self.assertFalse(x["coverage_verified"])
    def test_verified_observation_does_not_arm_orders(self):
        x=run({"ids_persisted":True},{"state":"EXCHANGE_PROTECTION_OBSERVED","coverage_verified":True})
        self.assertTrue(x["coverage_verified"])
        self.assertFalse(x["execution_authorized"])
        self.assertEqual(x["orders_placed"],0)
if __name__=="__main__":unittest.main()
