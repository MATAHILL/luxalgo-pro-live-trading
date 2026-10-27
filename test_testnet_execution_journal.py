import unittest
from unittest.mock import patch
from testnet_execution_journal import PostgresTestnetJournal, ALLOWED_TRANSITIONS, OPEN_STATES

PAYLOAD={"contract":"BTC_USDT","size":1,"text":"t-journal001","tif":"ioc",
         "price":"0","tpsl_sl_trigger_price":"90","tpsl_tp_trigger_price":"110"}

class FakeCursor:
    def __init__(self, rows):
        self.rows=list(rows)
        self.commands=[]
    def execute(self, statement, params=None):
        self.commands.append((statement,params))
    def fetchone(self):
        return self.rows.pop(0) if self.rows else None
    def fetchall(self):
        return self.rows.pop(0) if self.rows else []
    def __enter__(self): return self
    def __exit__(self,*exc): pass

class FakeConnection:
    def __init__(self, rows):
        self.cur=FakeCursor(rows)
    def cursor(self): return self.cur
    def __enter__(self): return self
    def __exit__(self,*exc): pass

class JournalTests(unittest.TestCase):
    def make(self,rows):
        journal=PostgresTestnetJournal("postgresql://unused")
        fake=FakeConnection(rows)
        journal._connect=lambda:fake
        return journal,fake

    def test_unique_durable_reservation_and_cap(self):
        j,conn=self.make([None,{"n":0},None])
        self.assertTrue(j.reserve("event-12345",PAYLOAD))
        self.assertTrue(any("pg_advisory_xact_lock" in q for q,_ in conn.cur.commands))
        self.assertTrue(any("INSERT INTO luxalgo_testnet_protected_journal" in q for q,_ in conn.cur.commands))

    def test_dedup_blocks_before_insert(self):
        j,conn=self.make([{"state":"HALTED_UNKNOWN"}])
        self.assertFalse(j.reserve("event-12345",PAYLOAD))
        self.assertFalse(any("INSERT INTO luxalgo_testnet_protected_journal" in q for q,_ in conn.cur.commands))

    def test_five_simultaneous_positions_limit(self):
        j,conn=self.make([None,{"n":5}])
        self.assertFalse(j.reserve("event-12345",PAYLOAD))

    def test_one_position_per_contract_reservation(self):
        j,conn=self.make([None,{"n":1},{"contract":"BTC_USDT"}])
        self.assertFalse(j.reserve("event-12345",PAYLOAD))

    def test_invalid_transition_never_updates(self):
        j,conn=self.make([{"state":"RESERVED"}])
        with self.assertRaises(RuntimeError):
            j.transition("event-12345","PROTECTED_VERIFIED",{})
        self.assertFalse(any(q.startswith("UPDATE") for q,_ in conn.cur.commands))

    def test_entry_unknown_saved_before_network_and_cannot_revert(self):
        j,conn=self.make([{"state":"RESERVED"}])
        j.transition("event-12345","SUBMISSION_UNKNOWN",{"client_id":"t-journal001"})
        self.assertTrue(any(q.startswith("UPDATE") for q,_ in conn.cur.commands))
        j2,conn2=self.make([{"state":"PROTECTED_VERIFIED"}])
        with self.assertRaises(RuntimeError):
            j2.transition("event-12345","SUBMISSION_UNKNOWN",{})

    def test_recovery_candidates_do_not_replay(self):
        j,conn=self.make([[{"signal_id":"x","state":"HALTED_UNKNOWN"}]])
        rows=j.recovery_candidates()
        self.assertEqual(rows[0]["state"],"HALTED_UNKNOWN")
        self.assertFalse(any(q.startswith("UPDATE") or q.startswith("INSERT") for q,_ in conn.cur.commands))

    def test_rejects_invalid_payload(self):
        j,conn=self.make([])
        with self.assertRaises(ValueError):
            j.reserve("event-12345",{**PAYLOAD,"size":0})
        self.assertFalse(conn.cur.commands)

if __name__=="__main__":
    unittest.main()
