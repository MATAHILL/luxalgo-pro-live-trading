"""Isolated Binance USD-M XRPUSDT execution candidate — NOT production-connected.

This module is intentionally NOT imported by app.py or included in Dockerfile.
No automatic or real-money activation is possible from UI/env settings.
It provides auditable, one-shot signed REST operations for exchange acceptance
testing after an independent code review and explicit controlled authorization.
"""
from decimal import Decimal, InvalidOperation
import os
import sqlite3
from contextlib import closing

import httpx

from binance_transport import BinanceReadOnly, signed_parameters, BASE_URL
from execution_runner import require_order_filled, require_algo

READ_PATHS = frozenset({
    "/fapi/v3/positionRisk", "/fapi/v1/openOrders",
    "/fapi/v1/openAlgoOrders", "/fapi/v1/positionSide/dual",
    "/fapi/v1/order", "/fapi/v1/algoOrder",
})
WRITE_PATHS = {
    ("POST", "/fapi/v1/order"),
    ("POST", "/fapi/v1/algoOrder"),
    ("DELETE", "/fapi/v1/algoOrder"),
}

class AcceptanceBlocked(RuntimeError):
    """A halt is mandatory, and no blind exchange retry is permitted."""

def decimal_value(value):
    if isinstance(value, bool):
        raise AcceptanceBlocked("Boolean exchange quantity denied")
    try:
        result = Decimal(str(value))
    except (TypeError, ValueError, InvalidOperation) as exc:
        raise AcceptanceBlocked("Unverified exchange quantity") from exc
    if not result.is_finite():
        raise AcceptanceBlocked("Non-finite exchange quantity")
    return result

class OneShotLedger:
    """Durable claim BEFORE each write; never retry after an uncertain response."""
    def __init__(self, db_path):
        if not isinstance(db_path, str) or not db_path.startswith("/var/data/"):
            raise AcceptanceBlocked("Dedicated persistent ledger required")
        if not os.path.ismount("/var/data"):
            raise AcceptanceBlocked("Dedicated Render disk mount unverified")
        self.db_path = db_path

    def _open(self):
        db = sqlite3.connect(self.db_path, timeout=20)
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA busy_timeout=20000")
        db.execute("""CREATE TABLE IF NOT EXISTS candidate_write_claims (
            signal_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            claimed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(signal_id, stage)
        )""")
        db.commit()
        return db

    def claim(self, signal_id, stage):
        if not isinstance(signal_id, str) or len(signal_id) < 12:
            raise AcceptanceBlocked("Unique original signal ID required")
        if stage not in {"ENTRY", "STOP", "TP", "EMERGENCY", "CANCEL_STOP", "CANCEL_TP"}:
            raise AcceptanceBlocked("Unrecognized write stage")
        with closing(self._open()) as db:
            with db:
                try:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute(
                        "INSERT INTO candidate_write_claims(signal_id,stage) VALUES (?,?)",
                        (signal_id, stage),
                    )
                except sqlite3.IntegrityError as exc:
                    raise AcceptanceBlocked("Write previously claimed; never resend blindly") from exc

    def has_claim(self, signal_id, stage):
        with closing(self._open()) as db:
            return db.execute(
                "SELECT 1 FROM candidate_write_claims WHERE signal_id=? AND stage=?",
                (signal_id, stage),
            ).fetchone() is not None

class CandidateBinanceUSDM:
    """Explicitly gated signed REST. No production entry point invokes this.

    The acceptance harness must pass allow_writes=True *in code*, not via
    an environment variable; that alone is NOT an exchange certification.
    """
    def __init__(self, api_key, api_secret, ledger, *,
                 client=None, allow_writes=False):
        if not api_key or not api_secret:
            raise AcceptanceBlocked("Missing Binance Futures credentials")
        self.api_key = api_key
        self.api_secret = api_secret
        self.ledger = ledger
        self.client = client
        self.allow_writes = (allow_writes is True)

    def _request(self, method, path, params):
        if method == "GET":
            if path not in READ_PATHS:
                raise AcceptanceBlocked("Read endpoint denied")
        elif (method, path) not in WRITE_PATHS or not self.allow_writes:
            raise AcceptanceBlocked("Live order sender is disconnected and locked")
        if params.get("symbol", "XRPUSDT") != "XRPUSDT":
            raise AcceptanceBlocked("Only XRPUSDT supported in acceptance candidate")
        signed = signed_parameters(params, self.api_secret)
        headers = {"X-MBX-APIKEY": self.api_key}
        def perform(client):
            # Signing of POST/DELETE uses exactly the parameters transmitted.
            url = BASE_URL + path + "?" + signed
            if method == "GET":
                response = client.get(url, headers=headers)
            elif method == "POST":
                response = client.post(url, headers=headers)
            else:
                response = client.delete(url, headers=headers)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, (dict, list)):
                raise AcceptanceBlocked("Exchange returned malformed response")
            return data
        try:
            if self.client is None:
                with httpx.Client(timeout=5, follow_redirects=False) as c:
                    return perform(c)
            return perform(self.client)
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            # Caller must reconcile by signed GET and NEVER replay a POST.
            raise AcceptanceBlocked("Exchange acknowledgment uncertain; read back, do not retry") from exc

    def get(self, path, params=None):
        return self._request("GET", path, params or {})

    def snapshot(self):
        positions = self.get("/fapi/v3/positionRisk")
        ordinary = self.get("/fapi/v1/openOrders")
        algos = self.get("/fapi/v1/openAlgoOrders")
        mode = self.get("/fapi/v1/positionSide/dual")
        if not all(isinstance(x, list) and len(x) < 1000 for x in (positions, ordinary, algos)):
            raise AcceptanceBlocked("Incomplete exchange inventory")
        if not isinstance(mode, dict) or mode.get("dualSidePosition") is not False:
            raise AcceptanceBlocked("Only verified One-way exchange mode supported")
        if not all(isinstance(row, dict) and isinstance(row.get("symbol"), str)
                   for group in (positions, ordinary, algos) for row in group):
            raise AcceptanceBlocked("Invalid exchange inventory")
        active = []
        for row in positions:
            quantity = decimal_value(row.get("positionAmt"))
            if quantity:
                active.append((row["symbol"], quantity))
        return {"active_positions": active, "open_orders": ordinary, "algos": algos}

    def require_flat_before_entry(self):
        state = self.snapshot()
        if state["active_positions"] or state["open_orders"] or state["algos"]:
            raise AcceptanceBlocked("Existing Binance positions/orders: do not touch manual trades")
        return state

    def read_entry(self, plan):
        row = self.get("/fapi/v1/order", {
            "symbol": "XRPUSDT", "origClientOrderId": plan.entry["newClientOrderId"],
        })
        require_order_filled(row, plan)
        return row

    def read_protection(self, spec):
        row = self.get("/fapi/v1/algoOrder", {
            "clientAlgoId": spec["clientAlgoId"],
        })
        require_algo(row, spec)
        return row

    def _write_once(self, signal_id, stage, method, path, data):
        if not self.allow_writes:
            raise AcceptanceBlocked("Live-order candidate remains locked")
        if data.get("symbol", "XRPUSDT") != "XRPUSDT":
            raise AcceptanceBlocked("Only XRPUSDT allowed")
        # A durable, atomic one-shot claim MUST be committed before HTTP.
        correct = {
            "ENTRY": ("POST", "/fapi/v1/order"),
            "EMERGENCY": ("POST", "/fapi/v1/order"),
            "STOP": ("POST", "/fapi/v1/algoOrder"),
            "TP": ("POST", "/fapi/v1/algoOrder"),
            "CANCEL_STOP": ("DELETE", "/fapi/v1/algoOrder"),
            "CANCEL_TP": ("DELETE", "/fapi/v1/algoOrder"),
        }
        if (method, path) != correct.get(stage):
            raise AcceptanceBlocked("Write stage and Binance endpoint mismatch")
        self.ledger.claim(signal_id, stage)
        return self._request(method, path, data)

    def enter_once(self, signal_id, plan):
        if plan.symbol != "XRPUSDT" or plan.entry.get("positionSide") != "BOTH":
            raise AcceptanceBlocked("One-way XRP acceptance only")
        self.require_flat_before_entry()
        if not self.allow_writes:
            raise AcceptanceBlocked("Acceptance authorization not configured")
        self._write_once(signal_id, "ENTRY", "POST", "/fapi/v1/order", plan.entry)
        return self.read_entry(plan)

    def place_protection_once(self, signal_id, plan):
        self.read_entry(plan)
        if any(spec.get("positionSide") != "BOTH" for spec in (plan.stop, plan.take_profit)):
            raise AcceptanceBlocked("Wrong position side")
        # Never submit TP without an independently verified exchange-native SL.
        self._write_once(signal_id, "STOP", "POST", "/fapi/v1/algoOrder", plan.stop)
        self.read_protection(plan.stop)
        self._write_once(signal_id, "TP", "POST", "/fapi/v1/algoOrder", plan.take_profit)
        self.read_protection(plan.take_profit)
        after = self.snapshot()
        signed_qty = decimal_value(plan.quantity) * (1 if plan.direction == "BUY" else -1)
        if after["active_positions"] != [("XRPUSDT", signed_qty)]:
            raise AcceptanceBlocked("Position mismatch after protection placement")
        expected = {plan.stop["clientAlgoId"], plan.take_profit["clientAlgoId"]}
        actual = {x.get("clientAlgoId") for x in after["algos"]}
        if actual != expected:
            raise AcceptanceBlocked("Unverified or extra conditional orders")
        if after["open_orders"]:
            raise AcceptanceBlocked("Unexpected outstanding ordinary orders")
        return {"state": "PROTECTED_EXCHANGE_READBACK", "entry": True,
                "stop": True, "take_profit": True}

    def emergency_one_shot(self, signal_id, plan):
        """Only a uniquely owned, exact-size position may be closed automatically.

        Any ambiguous or competing manual exposure requires operator intervention.
        A pre-POST durable claim prevents a blind retry after timeout or restart.
        """
        self.read_entry(plan)
        before = self.snapshot()
        signed_qty = decimal_value(plan.quantity) * (1 if plan.direction == "BUY" else -1)
        if before["active_positions"] != [("XRPUSDT", signed_qty)]:
            raise AcceptanceBlocked("Emergency: position ownership/size not verified")
        if before["open_orders"]:
            raise AcceptanceBlocked("Emergency: other open orders present")
        owned_ids = {plan.stop["clientAlgoId"], plan.take_profit["clientAlgoId"]}
        if not all(x.get("clientAlgoId") in owned_ids for x in before["algos"]):
            raise AcceptanceBlocked("Emergency: foreign conditional order present")
        side = "SELL" if signed_qty > 0 else "BUY"
        close = {"symbol": "XRPUSDT", "side": side, "type": "MARKET",
                 "quantity": str(abs(signed_qty)), "positionSide": "BOTH",
                 "reduceOnly": "true",
                 "newClientOrderId": "luxclose-" + plan.entry["newClientOrderId"][7:]}
        self._write_once(signal_id, "EMERGENCY", "POST", "/fapi/v1/order", close)
        # No second POST if acknowledgement is uncertain.
        record = self.get("/fapi/v1/order", {
            "symbol": "XRPUSDT", "origClientOrderId": close["newClientOrderId"],
        })
        if (record.get("status") != "FILLED"
                or record.get("clientOrderId") != close["newClientOrderId"]):
            raise AcceptanceBlocked("Emergency close fill not proven")
        after = self.snapshot()
        if after["active_positions"]:
            raise AcceptanceBlocked("Emergency close not flat; halt and notify operator")
        # Do not cancel foreign orders. Only cancel candidate-owned orphan algos.
        for stage, spec in (("CANCEL_STOP", plan.stop), ("CANCEL_TP", plan.take_profit)):
            if any(x.get("clientAlgoId") == spec["clientAlgoId"] for x in after["algos"]):
                self._write_once(signal_id, stage, "DELETE", "/fapi/v1/algoOrder",
                                 {"clientAlgoId": spec["clientAlgoId"]})
        end = self.snapshot()
        if end["active_positions"] or end["open_orders"] or end["algos"]:
            raise AcceptanceBlocked("Emergency recovery incomplete; do not resume")
        return {"state": "FLAT_AND_NO_ORDERS_VERIFIED", "automatic_reentry": False}

    def reconcile_after_native_exit(self, signal_id, plan):
        """Check native exit and cancel only this signal's orphan algos.

        Never assume SL or TP has fired simply because a webhook says so.
        This method is not scheduled or connected to production in this build.
        """
        self.read_entry(plan)
        before = self.snapshot()
        owned_ids = {plan.stop["clientAlgoId"], plan.take_profit["clientAlgoId"]}
        if before["active_positions"]:
            signed_qty = decimal_value(plan.quantity) * (1 if plan.direction == "BUY" else -1)
            if before["active_positions"] != [("XRPUSDT", signed_qty)]:
                raise AcceptanceBlocked("Position mismatch; operator recovery required")
            for spec in (plan.stop, plan.take_profit):
                self.read_protection(spec)
            return {"state": "STILL_PROTECTED", "automatic_reentry": False}
        if before["open_orders"] or any(
            row.get("clientAlgoId") not in owned_ids for row in before["algos"]
        ):
            raise AcceptanceBlocked("Foreign or uncertain exchange orders; halt")
        for stage, spec in (("CANCEL_STOP", plan.stop), ("CANCEL_TP", plan.take_profit)):
            if any(row.get("clientAlgoId") == spec["clientAlgoId"] for row in before["algos"]):
                self._write_once(signal_id, stage, "DELETE", "/fapi/v1/algoOrder",
                                 {"clientAlgoId": spec["clientAlgoId"]})
        after = self.snapshot()
        if after["active_positions"] or after["open_orders"] or after["algos"]:
            raise AcceptanceBlocked("Orphan order cleanup not confirmed")
        return {"state": "EXCHANGE_FLAT_OWNED_ALGOS_REMOVED",
                "automatic_reentry": False}

    def execute_once_with_emergency(self, signal_id, plan):
        """Single controlled acceptance sequence, NOT used by production.

        Only attempt recovery when the durable ENTRY claim exists. A failed
        or ambiguous Binance write is never resent automatically.
        """
        try:
            self.enter_once(signal_id, plan)
            return self.place_protection_once(signal_id, plan)
        except Exception as failure:
            if not self.ledger.has_claim(signal_id, "ENTRY"):
                raise AcceptanceBlocked("Pre-entry blocked; no orders attempted") from failure
            try:
                self.emergency_one_shot(signal_id, plan)
            except Exception as recovery_failure:
                raise AcceptanceBlocked(
                    "EMERGENCY HALT: exchange state uncertain; manual intervention required"
                ) from recovery_failure
            raise AcceptanceBlocked("Protected entry failed; verified flat after emergency") from failure
