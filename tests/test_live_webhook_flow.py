"""End-to-end HTTP webhook qualification with fake secret and mocked Postgres.

Tests actual Flask /webhook parser + authentication + durable SQL handoff.
No real TradingView account, Gate.io order, or persistent DB writes are used.
"""
import json
import os
import unittest
from contextlib import contextmanager
from unittest.mock import Mock,patch

import app as backend

FAKE_TOKEN="offline-test-key-7f9e018ee37b41d4"
BAR="BTCUSDT-5m-20261028T1200Z"

@contextmanager
def fake_db():
    con=Mock()
    con.cursor.return_value.__enter__=Mock(return_value=Mock())
    con.cursor.return_value.__exit__=Mock(return_value=False)
    yield con

class WebhookSafeIntegration(unittest.TestCase):
    def setUp(self):
        self.client=backend.app.test_client()
        self.valid=dict(token=FAKE_TOKEN,symbol="BTCUSDT",side="BUY",
                        tf="5m",entry="80000",sl="79000",tp1="82000",
                        tv_bar_id=BAR,order_type="MARKET")

    def test_live_style_bar_id_is_stable_across_http_retries(self):
        calls=[]
        @contextmanager
        def db():
            class Con:
                def execute(self,sql,params=None):calls.append((sql,params))
            yield Con()
        with (patch.object(backend,"expected_webhook_token",return_value=FAKE_TOKEN),
              patch.object(backend,"_db_record",side_effect=[False,True]) as record,
              patch.object(backend,"_db_connect",side_effect=db),
              patch.object(backend,"_ensure_schema"),
              patch.dict(os.environ,{"LIVE_TRADING_ENABLED":"true",
                            "ORDERS_ENABLED":"true",
                            "LIVE_ORDERS_SEPARATELY_AUTHORIZED":"true"})):
            first=self.client.post("/webhook",data=json.dumps(self.valid),content_type="text/plain")
            replay=self.client.post("/webhook",data=json.dumps(self.valid),content_type="text/plain")
        self.assertEqual(first.status_code,200)
        self.assertEqual(replay.status_code,200)
        self.assertEqual(first.json["dry_run_intent_id"],replay.json["dry_run_intent_id"])
        self.assertFalse(first.json["duplicate_in_candle"])
        self.assertTrue(replay.json["duplicate_in_candle"])
        self.assertEqual(first.json["orders_placed"],0)
        handoff=[params for sql,params in calls if "INSERT INTO luxalgo_live_signal_handoff" in sql]
        self.assertEqual(len(handoff),2)
        self.assertEqual(handoff[0][-1],BAR)
        self.assertEqual(handoff[0][0],handoff[1][0])
        self.assertEqual(record.call_count,2)

    def test_legacy_missing_bar_keeps_signal_only(self):
        old=dict(self.valid);old.pop("tv_bar_id")
        with (patch.object(backend,"expected_webhook_token",return_value=FAKE_TOKEN),
              patch.object(backend,"_db_record",return_value=False),
              patch.object(backend,"_db_connect",side_effect=fake_db),
              patch.object(backend,"_ensure_schema")):
            response=self.client.post("/webhook",json=old)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json["mode"],"SIGNAL_ONLY")
        self.assertEqual(response.json["orders_placed"],0)

    def test_forged_and_missing_token_rejected_before_db(self):
        for val in ("invalid-key",""):
            d=dict(self.valid,token=val)
            with (patch.object(backend,"expected_webhook_token",return_value=FAKE_TOKEN),
                  patch.object(backend,"_db_record") as recorder):
                res=self.client.post("/webhook",json=d)
            self.assertEqual(res.status_code,401)
            recorder.assert_not_called()

    def test_invalid_payload_and_levels_do_not_dispatch(self):
        with (patch.object(backend,"expected_webhook_token",return_value=FAKE_TOKEN),
              patch.object(backend,"_db_record",return_value=False),
              patch.object(backend,"_db_connect",side_effect=fake_db),
              patch.object(backend,"_ensure_schema")):
            broken=self.client.post("/webhook",data="bad-json",content_type="text/plain")
            d=dict(self.valid,sl="82000")
            resp=self.client.post("/webhook",json=d)
        self.assertEqual(broken.status_code,400)
        self.assertEqual(resp.status_code,200)
        self.assertEqual(resp.json["level_status"],"INVALID")
        self.assertEqual(resp.json["orders_placed"],0)

    def test_sell_signal_normalizes_to_short(self):
        signal=dict(self.valid,side="SELL",entry="80000",sl="80800",tp1="78400")
        with (patch.object(backend,"expected_webhook_token",return_value=FAKE_TOKEN),
              patch.object(backend,"_db_record",return_value=False),
              patch.object(backend,"_db_connect",side_effect=fake_db),
              patch.object(backend,"_ensure_schema")):
            res=self.client.post("/webhook",json=signal)
        self.assertEqual(res.status_code,200)
        self.assertEqual(res.json["side"],"SHORT")
        self.assertEqual(res.json["level_status"],"VALID")
        self.assertEqual(res.json["orders_placed"],0)

    def test_production_monitor_explains_missing_bar_and_order_type_without_claiming_fill(self):
        from datetime import datetime,timezone
        from contextlib import nullcontext
        with patch.dict(os.environ,{
            "PRODUCTION_TERMINAL_VIEW":"true",
            "TERMINAL_PASSWORD":"test-passphrase-allowing-strong-auth",
            "TERMINAL_USER":"admin"}):
            client=backend.app.test_client()
            with client.session_transaction() as sess:
                sess["authenticated"]=True;sess["user"]="admin";sess["csrf_token"]="safe"
            scenarios=[
                (None,None,"NO_LIVE_HANDOFF"),
                (None,"MARKET","MISSING_STABLE_BAR_ID"),
                ("TVBAR_5m_00597712345",None,"MISSING_MARKET_DECLARATION"),
                ("TVBAR_5m_00597712345","LIMIT","MISSING_MARKET_DECLARATION"),
                ("TVBAR_5m_00597712345","MARKET","QUALIFIED_SIGNAL_EXECUTION_LOCKED")]
            for bar,declared,expected in scenarios:
                with self.subTest(qualification=expected,bar=bar,declared=declared):
                    row=dict(event_id="a"*20,received_at=datetime.now(timezone.utc),
                             symbol="BTCUSDT",side="LONG",timeframe="5m",
                             level_status="VALID",entry_price="80000",
                             stop_loss="79000",take_profit="82000",
                             order_type="MARKET",source_bar_id=bar,
                             handoff_order_type=declared,
                             handoff_event_id=None if bar is None and declared is None else "a"*20)
                    # Simulates a stored receipt + optional handoff; no DB changes,
                    # exchange requests or orders.
                    con=Mock();cursor=Mock()
                    cursor.fetchall.return_value=[row]
                    con.cursor.return_value.__enter__=Mock(return_value=cursor)
                    con.cursor.return_value.__exit__=Mock(return_value=False)
                    with (patch.object(backend,"_db_connect",return_value=nullcontext(con)),
                          patch.object(backend,"_ensure_schema"),
                          patch.object(backend,"_incoming_visible_since",
                                       return_value=datetime.now(timezone.utc))):
                        res=client.get("/terminal/incoming-signals?production=1")
                    self.assertEqual(res.status_code,200)
                    self.assertEqual(res.json["signals"][0]["execution_qualification"],expected)
                    # A signal with no ledger row has not executed. The label is
                    # now the truthful PENDING rather than the old hardcoded
                    # "NOT EXECUTED", which was applied even to filled trades.
                    self.assertEqual(res.json["signals"][0]["status_label"],"PENDING")
                    self.assertEqual(res.json["signals"][0]["state"],"PENDING")
                    self.assertFalse(res.json["orders_enabled"])
                    self.assertFalse(res.json["live_trading"])

if __name__=="__main__":
    unittest.main()
