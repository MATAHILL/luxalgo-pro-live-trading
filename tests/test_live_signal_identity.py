"""TradingView webhook stable bar identity and safe legacy fallback."""
import unittest
from datetime import datetime,timezone,timedelta
from live_signal_identity import stable_signal_identity,validated_bar_id

class TradingViewIdentityTests(unittest.TestCase):
    def test_replayed_bar_deduplicates_across_receive_times(self):
        t=datetime(2026,10,28,10,20,tzinfo=timezone.utc)
        a,bar=stable_signal_identity("BTCUSDT","LONG","5m",t,"BTCUSDT-5m-20261028T1020")
        b,_=stable_signal_identity("BTCUSDT","LONG","5m",t+timedelta(hours=2),"BTCUSDT-5m-20261028T1020")
        self.assertEqual(a,b)
        self.assertEqual(bar,"BTCUSDT-5m-20261028T1020")
        self.assertEqual(len(a),20)

    def test_symbol_timeframe_and_side_are_distinct(self):
        t=datetime(2026,10,28,10,20,tzinfo=timezone.utc)
        v=[stable_signal_identity(sym,side,tf,t,"tvBar_12345678")[0] for sym,side,tf in (
            ("BTCUSDT","LONG","5m"),("BTCUSDT","SHORT","5m"),
            ("BTCUSDT","LONG","15m"),("ETHUSDT","LONG","5m"))]
        self.assertEqual(len(set(v)),4)

    def test_missing_bar_id_keeps_legacy_receive_bucket(self):
        t=datetime(2026,10,28,10,20,tzinfo=timezone.utc)
        a,bar=stable_signal_identity("BTCUSDT","LONG","5m",t)
        b,bar2=stable_signal_identity("BTCUSDT","LONG","5m",t+timedelta(seconds=17))
        self.assertEqual(a,b)
        self.assertIsNone(bar)
        self.assertIsNone(bar2)
        nextbar,_=stable_signal_identity("BTCUSDT","LONG","5m",t+timedelta(minutes=5))
        self.assertNotEqual(a,nextbar)

    def test_malformed_bar_ids_never_become_executable(self):
        self.assertIsNone(validated_bar_id("not valid!"))
        self.assertIsNone(validated_bar_id(1234567890))
        self.assertIsNone(validated_bar_id("x"*121))

    def test_naive_datetime_rejected(self):
        with self.assertRaises(ValueError):
            stable_signal_identity("BTCUSDT","LONG","5m",datetime(2026,10,28),"BAR_00000012")

if __name__=="__main__":
    unittest.main()
