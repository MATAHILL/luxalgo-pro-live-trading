import unittest
from live_execution_safety import ProposedRisk, validate_risk

GOOD=ProposedRisk(event_id="0123456789abcdefabcd",contract="BTC_USDT",
                  side="LONG",risk_usdt="3",equity_usdt="1000",
                  available_usdt="800",leverage="5",notional_usdt="100")

class LiveExecutionSafetyTests(unittest.TestCase):
    def update(self,**changes):
        return ProposedRisk(**{**GOOD.__dict__,**changes})

    def test_valid_plan_is_reservation_only(self):
        result=validate_risk(GOOD)
        self.assertTrue(result["client_id"].startswith("t-lux-"))
        self.assertEqual(result["leverage"],5)

    def test_risk_limit_fail_closed(self):
        for change in ({"risk_usdt":"5.01"},
                       {"leverage":"5.01"},
                       {"notional_usdt":"4001"},
                       {"equity_usdt":"0"},
                       {"contract":"BTCUSDT"},
                       {"event_id":"old-signal"},
                       {"side":"HEDGE"},
                       {"available_usdt":"1200"},
                       {"risk_usdt":"NaN"}):
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    validate_risk(self.update(**change))

    def test_deterministic_unique_client_id(self):
        x=validate_risk(GOOD)["client_id"]
        self.assertEqual(x,validate_risk(GOOD)["client_id"])
        self.assertNotEqual(x,validate_risk(self.update(event_id="bbbbbbbbbbbbbbbbbbbb"))["client_id"])

if __name__=="__main__":
    unittest.main()
