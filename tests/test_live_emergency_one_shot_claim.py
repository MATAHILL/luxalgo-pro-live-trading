"""Recovery preparation tests: no actual DB changes or Gate order submission."""
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock
from live_futures_engine import LiveSafetyError
from live_emergency_one_shot_claim import (
    evaluate_one_shot_close_candidate, claim_one_shot_recovery,
    consume_one_shot_recovery_claim, finalize_one_shot_recovery_claim, CLAIM_SCHEMA,
)

NOW=datetime(2026,11,1,6,0,tzinfo=timezone.utc)
LEDGER=dict(event_id="test-xrp-2026",contract="XRP_USDT",
    side="LONG",contracts="1",position_mode="dual",state="HALTED")
SNAPSHOT=dict(readback_utc=NOW-timedelta(seconds=5),
    source="GATE_IO_LIVE_FUTURES",halt_active=True,position_mode="dual",
    positions=[dict(contract="XRP_USDT",size="1")],orders=[],triggers=[])


def con_for_claim(*fetches):
    con=Mock()
    cursor=Mock()
    cursor.fetchone.side_effect=list(fetches)
    con.cursor.return_value.__enter__=Mock(return_value=cursor)
    con.cursor.return_value.__exit__=Mock(return_value=False)
    return con,cursor


class DurableEmergencyOneShotTests(unittest.TestCase):
    def test_recovery_candidate_requires_exact_fresh_single_position(self):
        result=evaluate_one_shot_close_candidate(LEDGER,SNAPSHOT,NOW)
        self.assertTrue(result["eligible_for_atomic_claim"])
        self.assertEqual(result["orders_sent"],0)

    def test_unknown_exposure_cannot_authorize_close_claim(self):
        cases=[
            dict(SNAPSHOT,positions=[]),
            dict(SNAPSHOT,positions=SNAPSHOT["positions"]*2),
            dict(SNAPSHOT,orders=[{"id":1}]),
            dict(SNAPSHOT,triggers=[{"id":101}]),
            dict(SNAPSHOT,halt_active=False),
            dict(SNAPSHOT,source="UNVERIFIED"),
            dict(SNAPSHOT,position_mode="single"),
            dict(SNAPSHOT,readback_utc=NOW-timedelta(seconds=31)),
            dict(SNAPSHOT,positions=[{"contract":"XRP_USDT","size":"2"}]),
            dict(SNAPSHOT,positions=[{"contract":"XRP_USDT","size":"-1"}]),
            dict(SNAPSHOT,positions=None),
        ]
        for case in cases:
            with self.subTest(case=case):
                result=evaluate_one_shot_close_candidate(LEDGER,case,NOW)
                self.assertFalse(result["eligible_for_atomic_claim"])
                self.assertEqual(result["orders_sent"],0)
        self.assertFalse(evaluate_one_shot_close_candidate(
            dict(LEDGER,state="CLOSED_VERIFIED"),SNAPSHOT,NOW)["eligible_for_atomic_claim"])

    def test_durable_claim_requires_halt_and_unique_exact_journal(self):
        con,cur=con_for_claim(dict(halted=True),
            dict(event_id=LEDGER["event_id"],contract="XRP_USDT",state="HALTED"),
            dict(event_id=LEDGER["event_id"]))
        result=claim_one_shot_recovery(con,event_id=LEDGER["event_id"],
            contract="XRP_USDT",approval_reference="EXAMPLE_LONG_RECOVERY_APPROVAL",
            close_text="t-em-lxp-testhash")
        self.assertTrue(result["claimed"])
        self.assertEqual(result["orders_sent"],0)
        self.assertIn("CREATE TABLE IF NOT EXISTS luxalgo_prod_emergency_close_claims",CLAIM_SCHEMA)
        self.assertTrue(any("ON CONFLICT DO NOTHING" in c.args[0] for c in cur.execute.call_args_list))
        self.assertEqual(cur.execute.call_count,4)

    def test_duplicate_claim_rejected_with_no_exchange_order(self):
        con,_=con_for_claim(dict(halted=True),
            dict(event_id=LEDGER["event_id"],contract="XRP_USDT",state="HALTED"),None)
        with self.assertRaisesRegex(LiveSafetyError,"ONE_SHOT_CLAIM_ALREADY_USED"):
            claim_one_shot_recovery(con,event_id=LEDGER["event_id"],
                contract="XRP_USDT",approval_reference="EXAMPLE_LONG_RECOVERY_APPROVAL",
                close_text="t-em-lxp-testhash")

    def test_unconfirmed_halt_or_mismatched_journal_rejected(self):
        cases=[
            (dict(halted=False),None),
            (dict(halted=True),dict(contract="BTC_USDT",state="HALTED")),
            (dict(halted=True),dict(contract="XRP_USDT",state="PROTECTED")),
        ]
        for rows in cases:
            with self.subTest(rows=rows):
                con,_=con_for_claim(*rows)
                with self.assertRaises(LiveSafetyError):
                    claim_one_shot_recovery(con,event_id=LEDGER["event_id"],
                        contract="XRP_USDT",approval_reference="EXAMPLE_LONG_RECOVERY_APPROVAL",
                        close_text="t-em-lxp-testhash")

    def test_claim_consumption_is_once_only_before_exchange_call(self):
        claim=dict(claimed=True,state="CLAIMED",event_id=LEDGER["event_id"],
            contract="XRP_USDT",approval_reference="OWNER_APPROVED_EXAMPLE",
            close_client_text="t-em-lxp-testhash")
        con,cur=con_for_claim(dict(halted=True),dict(event_id=LEDGER["event_id"]))
        self.assertTrue(consume_one_shot_recovery_claim(lambda:con,claim))
        self.assertTrue(any("state='EXCHANGE_RESULT_UNKNOWN'" in a.args[0]
                            and "state='CLAIMED'" in a.args[0]
                            for a in cur.execute.call_args_list))
        # A repeated invocation cannot get the CAS result again.
        duplicate,cur2=con_for_claim(dict(halted=True),None)
        with self.assertRaisesRegex(LiveSafetyError,"CLOSE_CLAIM_CONSUMED"):
            consume_one_shot_recovery_claim(lambda:duplicate,claim)

    def test_close_outcome_requires_persisted_finalization(self):
        claim=dict(event_id=LEDGER["event_id"],contract="XRP_USDT",
                   close_client_text="t-em-lxp-testhash")
        con,cur=con_for_claim(dict(event_id=LEDGER["event_id"]))
        self.assertTrue(finalize_one_shot_recovery_claim(lambda:con,claim))
        self.assertTrue(any("FLAT_READBACK_VERIFIED" in a.args[0]
                            for a in cur.execute.call_args_list))
        missing,_=con_for_claim(None)
        with self.assertRaisesRegex(LiveSafetyError,"CLOSE_VERIFICATION_NOT_PERSISTED"):
            finalize_one_shot_recovery_claim(lambda:missing,claim)

    def test_invalid_approval_reference_rejected_before_db(self):
        con=Mock()
        with self.assertRaisesRegex(LiveSafetyError,"MISSING_EXACT"):
            claim_one_shot_recovery(con,event_id=LEDGER["event_id"],
                contract="XRP_USDT",approval_reference="",close_text="t-em-lxp-testhash")
        con.execute.assert_not_called()

if __name__=="__main__":unittest.main()
