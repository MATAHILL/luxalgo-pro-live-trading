"""Exchange inventory must match precisely before any emergency close claim is consumed.

No real exchange requests. All Gate client and database operations are mocked.
"""
import unittest
from unittest.mock import Mock, patch
from live_futures_engine import LiveSafetyError, client_text
from live_execution_orchestrator import (
    require_safe_emergency_close_inventory, emergency_close_once,
)

PLAN = {
    "event_id": "alert-BTC-5m-test-0001",
    "client_text": client_text("alert-BTC-5m-test-0001"),
    "contract": "BTC_USDT",
    "side": "LONG",
    "contracts": 2,
    "position_mode": "dual",
}
CLAIM = {
    "claimed": True, "state": "CLAIMED",
    "event_id": PLAN["event_id"],
    "contract": "BTC_USDT",
    "close_client_text": "t-em-" + PLAN["client_text"][2:25],
    "approval_reference": "EXPLICIT_EXAMPLE_NOT_REAL_PERMISSION",
}


def snapshot():
    return {
        "source": "GATE_IO_LIVE_FUTURES", "fetched_at": 1000,
        "account": {"position_mode": "dual"},
        "positions": [{"contract": "BTC_USDT", "size": "2"}],
        "orders": [], "triggers": [],
    }


class EmergencyCloseInventoryTests(unittest.TestCase):
    def test_verified_exact_position_has_no_order_side_effect(self):
        client=Mock()
        client.snapshot.return_value=snapshot()
        result=require_safe_emergency_close_inventory(client,PLAN,now_epoch=1010)
        self.assertTrue(result["verified"])
        self.assertEqual(result["signed_contracts"],"2")
        self.assertEqual(result["orders_sent"],0)
        client.request.assert_not_called()

    def test_wrong_mode_side_quantity_and_contract_rejected(self):
        for name,change in (
            ("wrong-mode", lambda x: x["account"].update(position_mode="single")),
            ("wrong-size", lambda x: x["positions"][0].update(size="1")),
            ("short", lambda x: x["positions"][0].update(size="-2")),
            ("wrong-contract", lambda x: x["positions"][0].update(contract="ETH_USDT")),
            ("extra-position", lambda x: x["positions"].append({"contract":"ETH_USDT","size":"1"})),
            ("untracked-order", lambda x: x["orders"].append({"id":100})),
            ("orphan-trigger", lambda x: x["triggers"].append({"id":101})),
            ("wrong-source", lambda x: x.update(source="SAMPLE")),
            ("missing-orders", lambda x: x.pop("orders")),
        ):
            with self.subTest(name=name):
                client=Mock()
                data=snapshot()
                change(data)
                client.snapshot.return_value=data
                with self.assertRaises(LiveSafetyError):
                    require_safe_emergency_close_inventory(client,PLAN,now_epoch=1010)
                client.request.assert_not_called()

    def test_old_and_future_readbacks_rejected(self):
        client=Mock()
        client.snapshot.return_value=snapshot()
        for now in (1031,999, None):
            with self.subTest(now=now):
                if now is None:
                    client.snapshot.return_value=dict(snapshot(),fetched_at=None)
                    now=1010
                with self.assertRaises(LiveSafetyError):
                    require_safe_emergency_close_inventory(client,PLAN,now_epoch=now)

    def test_bad_contract_quantity_is_never_rounded(self):
        client=Mock()
        client.snapshot.return_value=snapshot()
        for quantity in ("1.5", "0", None, True):
            with self.subTest(quantity=quantity):
                with self.assertRaises(LiveSafetyError):
                    require_safe_emergency_close_inventory(client,
                        dict(PLAN,contracts=quantity),now_epoch=1010)

    def test_failed_readback_prevents_consuming_claim_and_any_post(self):
        client=Mock()
        client.snapshot.return_value=dict(snapshot(),positions=[])
        with patch("live_emergency_one_shot_claim.consume_one_shot_recovery_claim") as consume:
            result=emergency_close_once(client,PLAN,
                        durable_claim=CLAIM,open_connection=lambda:None)
        self.assertFalse(result["closed_verified"])
        self.assertEqual(result["orders_sent"],0)
        self.assertEqual(result["reason"],"FRESH_EXCHANGE_CLOSE_PREFLIGHT_FAILED_NO_ORDER")
        consume.assert_not_called()
        client.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
