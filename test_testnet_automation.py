import unittest
from datetime import datetime,timezone,timedelta
from unittest.mock import patch
from testnet_automation import run_fresh_luxalgo_testnet_signal, testnet_automation_armed

NOW=datetime(2026,10,27,12,0,0,tzinfo=timezone.utc)
EVENT="1234567890abcdefabcd"
ROW=dict(event_id=EVENT,received_at=NOW-timedelta(seconds=5),
         symbol="ETHUSDT",side="SHORT",timeframe="5m",level_status="VALID",
         entry_price="100",stop_loss="110",take_profit="90",bot_enabled=True)

class Cursor:
    def __init__(self,row): self.row=row
    def execute(self,*a,**kw): pass
    def fetchone(self):return self.row
    def __enter__(self):return self
    def __exit__(self,*args):pass
class Connection:
    def __init__(self,row):self.row=row
    def cursor(self):return Cursor(self.row)
    def __enter__(self):return self
    def __exit__(self,*args):pass

class TestAutomation(unittest.TestCase):
    def test_both_independent_flags_required(self):
        for a,b,enabled in [("false","false",False),("true","false",False),
                            ("false","true",False),("true","true",True)]:
            with patch.dict("os.environ",{"GATE_TESTNET_PROTECTED_EXECUTION_ENABLED":a,
                "GATE_TESTNET_WEBHOOK_AUTOMATION_ENABLED":b}):
                self.assertEqual(testnet_automation_armed(),enabled)

    def call(self,row,now=NOW):
        with patch.dict("os.environ",{
             "GATE_TESTNET_PROTECTED_EXECUTION_ENABLED":"true",
             "GATE_TESTNET_WEBHOOK_AUTOMATION_ENABLED":"true",
             "DATABASE_URL":"postgresql://unused"}):
            with patch("testnet_automation.psycopg.connect",return_value=Connection(row)):
                with patch("testnet_automation.execute_protected_testnet",
                    return_value={"state":"PROTECTED_VERIFIED"}) as coordinator:
                    response=run_fresh_luxalgo_testnet_signal(EVENT,broker=object(),journal=object(),now=now)
                    return response,coordinator

    def test_fresh_valid_signal_reaches_testnet_coordinator(self):
        result,run=self.call(ROW)
        self.assertEqual(result["state"],"PROTECTED_VERIFIED")
        intent=run.call_args.args[0]
        self.assertEqual((intent.contract,intent.side,intent.stop,intent.target),
                         ("ETH_USDT","SHORT","110","90"))
        self.assertEqual(intent.client_id,"t-lx-"+EVENT)

    def test_old_signal_not_replayed(self):
        result,run=self.call({**ROW,"received_at":NOW-timedelta(seconds=63)})
        self.assertEqual(result["state"],"SIGNAL_EXPIRED")
        run.assert_not_called()

    def test_bot_off_not_processed(self):
        result,run=self.call({**ROW,"bot_enabled":False})
        self.assertEqual(result["state"],"SIGNAL_NOT_ELIGIBLE")
        run.assert_not_called()

    def test_invalid_levels_not_processed(self):
        result,run=self.call({**ROW,"stop_loss":None})
        self.assertEqual(result["state"],"INVALID_LEVELS")
        run.assert_not_called()

    def test_disarmed_never_accesses_db(self):
        with patch.dict("os.environ",{"GATE_TESTNET_PROTECTED_EXECUTION_ENABLED":"false",
                     "GATE_TESTNET_WEBHOOK_AUTOMATION_ENABLED":"true"}):
            with patch("testnet_automation.psycopg.connect") as db:
                self.assertEqual(run_fresh_luxalgo_testnet_signal(EVENT)["state"],"TESTNET_DISABLED")
                db.assert_not_called()

if __name__=="__main__":
    unittest.main()
