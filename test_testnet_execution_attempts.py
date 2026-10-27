import unittest
from unittest.mock import patch,MagicMock
from testnet_execution_attempts import record_testnet_attempt

EID="1234567890abcdefabcd"

class ExecutionAuditTests(unittest.TestCase):
    def test_safe_result_writes_one_transaction(self):
        mock=MagicMock()
        with patch("testnet_execution_attempts.psycopg.connect",return_value=mock) as connect:
            self.assertTrue(record_testnet_attempt(EID,{
                "state":"TESTNET_PREFLIGHT_BLOCKED",
                "detail":"LEVERAGE_STILL_EXCEEDS_TESTNET_MAX_5X",
                "submitted":False},database_url="postgresql://unit.invalid"))
            connect.assert_called_once()
            self.assertEqual(mock.__enter__.return_value.cursor.return_value.__enter__.return_value.execute.call_count,2)

    def test_unsafe_detail_never_reaches_postgres(self):
        with patch("testnet_execution_attempts.psycopg.connect") as db:
            with self.assertRaises(ValueError):
                record_testnet_attempt(EID,{
                    "state":"UNKNOWN","detail":"KEY=secret%20test"},
                    database_url="postgresql://unit.invalid")
            db.assert_not_called()

    def test_invalid_event_id_cannot_write(self):
        with patch("testnet_execution_attempts.psycopg.connect") as db:
            with self.assertRaises(ValueError):
                record_testnet_attempt("fake",{"state":"PROTECTED_VERIFIED"},
                                       database_url="postgresql://unit.invalid")
            db.assert_not_called()

if __name__=="__main__":unittest.main()
