"""LuxAlgo paid Signals & Overlays {time}/{default} webhook qualification.

Actual Flask POST endpoint exercised using fake credentials and mocked database.
Never submits exchange orders or changes production user data.
"""
import json
import unittest
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
from unittest.mock import patch
import app as backend
from live_signal_identity import luxalgo_bar_identity,normalize_luxalgo_side

TOKEN="test-only-token-40-char-not-real-2e8df3d0db"
@contextmanager
def dummy_db():
    class DummyCon:
        def execute(self,*args,**kwargs):return None
    yield DummyCon()

class LuxAlgoSourceQualification(unittest.TestCase):
    def test_valid_fresh_bar_time_stable_within_same_candle(self):
        dt=datetime(2026,10,28,13,40,15,tzinfo=timezone.utc)
        tm=str(int(datetime(2026,10,28,13,40,tzinfo=timezone.utc).timestamp()*1000))
        one=luxalgo_bar_identity("5m",dt,tm)
        two=luxalgo_bar_identity("5m",dt+timedelta(seconds=4),int(tm))
        self.assertEqual(one,two)
        self.assertTrue(one.startswith("TVBAR_5m_"))

    def test_stale_future_malformed_bar_time_rejected(self):
        dt=datetime(2026,10,28,13,40,tzinfo=timezone.utc)
        good=int(dt.timestamp()*1000)
        for value in (good-3600_000,good+3600_000,"{time}","na",None,True,0):
            with self.subTest(value=value):
                self.assertIsNone(luxalgo_bar_identity("5m",dt,value))

    def test_only_directional_confirmations_are_trades(self):
        for alert in ("Bullish Confirmation Signal","BUY","Bullish Confirmation"):
            self.assertEqual(normalize_luxalgo_side(alert),"LONG")
        for alert in ("Bearish Confirmation Signal","SELL"):
            self.assertEqual(normalize_luxalgo_side(alert),"SHORT")
        for alert in ("Bullish Exit","Bearish Exit","Take Profit","Stop Loss","na"):
            self.assertIsNone(normalize_luxalgo_side(alert))

    def test_actual_flask_webhook_luxalgo_template_with_duplicate(self):
        client=backend.app.test_client()
        now=datetime.now(timezone.utc)
        bar_ms=int(now.timestamp()//300*300*1000)
        message={"token":TOKEN,"ticker":"BTCUSDT",
                 "alert":"Bullish Confirmation Signal","tf":"5",
                 "bartime":bar_ms,
                 "entry":"80000","sl":"79000","tp1":"82000",
                 "order_type":"MARKET"}
        with (patch.object(backend,"expected_webhook_token",return_value=TOKEN),
              patch.object(backend,"_db_record",side_effect=[False,True]),
              patch.object(backend,"_db_connect",side_effect=dummy_db),
              patch.object(backend,"_ensure_schema")):
            first=client.post("/webhook",data=json.dumps(message),content_type="text/plain")
            second=client.post("/webhook",data=json.dumps(message),content_type="text/plain")
        self.assertEqual(first.status_code,200)
        self.assertEqual(second.status_code,200)
        self.assertEqual(first.json["side"],"LONG")
        self.assertEqual(first.json["level_status"],"VALID")
        self.assertTrue(first.json["stable_bar_identity"])
        self.assertTrue(first.json["order_type_qualified"])
        self.assertEqual(first.json["dry_run_intent_id"],second.json["dry_run_intent_id"])
        self.assertTrue(second.json["duplicate_in_candle"])
        self.assertEqual(first.json["orders_placed"],0)

    def test_exit_signal_rejected_even_with_valid_prices_and_token(self):
        client=backend.app.test_client()
        body=dict(token=TOKEN,ticker="BTCUSDT",tf="5",
                  alert="Bullish Exit",entry="80000",sl="79000",tp1="82000",
                  order_type="MARKET",bartime=int(datetime.now(timezone.utc).timestamp()*1000))
        with patch.object(backend,"expected_webhook_token",return_value=TOKEN):
            res=client.post("/webhook",json=body)
        self.assertEqual(res.status_code,422)

if __name__=="__main__":unittest.main()
