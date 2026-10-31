"""Offline-only Binance XRP Futures execution choreography.

There is intentionally no HTTP client and no connection to the live Flask/FastAPI
webhook app. A fake exchange must be supplied by the test harness. This verifies
the order/protection/failure sequence before a separately reviewed live sender.
"""
from decimal import Decimal
from order_plan import ExecutionBlocked

class EmergencyHalt(ExecutionBlocked):
    pass

def require_order_filled(row, plan):
    if not isinstance(row, dict):
        raise EmergencyHalt("Entry readback missing")
    if row.get("symbol") != plan.symbol:
        raise EmergencyHalt("Entry symbol mismatch")
    if row.get("side") != plan.direction:
        raise EmergencyHalt("Entry side mismatch")
    if row.get("status") != "FILLED":
        raise EmergencyHalt("Entry not fully filled")
    if row.get("clientOrderId") != plan.entry["newClientOrderId"]:
        raise EmergencyHalt("Entry id mismatch")
    if Decimal(str(row.get("executedQty", "0"))) != Decimal(plan.quantity):
        raise EmergencyHalt("Executed quantity mismatch")

def require_algo(row, spec):
    if not isinstance(row, dict):
        raise EmergencyHalt("Missing conditional order")
    for req, actual_key in (
        ("symbol", "symbol"),
        ("side", "side"),
        ("type", "orderType"),
        ("positionSide", "positionSide"),
        ("triggerPrice", "triggerPrice"),
        ("clientAlgoId", "clientAlgoId"),
    ):
        if str(row.get(actual_key)) != str(spec.get(req)):
            raise EmergencyHalt("Conditional order mismatch: " + req)
    if row.get("algoStatus") not in ("NEW", "OPEN"):
        raise EmergencyHalt("Conditional order not open")
    if str(row.get("closePosition")).lower() != "true":
        raise EmergencyHalt("Conditional order not close-position")

def simulate_protected_execution(plan, exchange):
    """Single isolated fixture cycle. NEVER invoked by the deployed webhook app.

    Exchange fixture interface:
      snapshot() -> {qty, open_order_ids, protection_ids, position_side}
      enter(entry_spec), read_entry(client_id)
      create_algo(spec), read_algo(client_algo_id)
      emergency_reduce_only_close(quantity, side)
      cancel_algo(client_algo_id)
    """
    if plan.symbol != "XRPUSDT" or plan.direction not in ("BUY", "SELL"):
        raise EmergencyHalt("Forbidden signal")
    pre = exchange.snapshot()
    if Decimal(str(pre.get("qty", "invalid"))) != 0:
        raise EmergencyHalt("Existing position; cannot safely enter")
    if pre.get("position_side") != "BOTH":
        raise EmergencyHalt("One-way position mode not confirmed")
    if pre.get("open_order_ids"):
        raise EmergencyHalt("Existing orders; cannot safely enter")
    if pre.get("protection_ids"):
        raise EmergencyHalt("Pre-existing protective orders; cannot safely enter")
    created = []
    entry_attempted = False
    try:
        entry_attempted = True
        exchange.enter(plan.entry)
        read = exchange.read_entry(plan.entry["newClientOrderId"])
        require_order_filled(read, plan)
        for spec in (plan.stop, plan.take_profit):
            # Reserve id before dispatch; even a timeout may have placed the algo.
            created.append(spec["clientAlgoId"])
            exchange.create_algo(spec)
            require_algo(exchange.read_algo(spec["clientAlgoId"]), spec)
        current = exchange.snapshot()
        expected_signed = Decimal(plan.quantity) * (1 if plan.direction == "BUY" else -1)
        if Decimal(str(current.get("qty", "0"))) != expected_signed:
            raise EmergencyHalt("Position size readback mismatch")
        if set(current.get("protection_ids", [])) != set(created):
            raise EmergencyHalt("Protection count or IDs mismatch")
        return {"state": "SIMULATION_ACCEPTED", "orders_sent_to_real_exchange": 0,
                "entry_verified": True, "native_protection_verified": True,
                "position_verified": True}
    except Exception as exc:
        # Cannot assume a failed/timeout entry was not filled: always reconcile.
        if entry_attempted:
            try:
                qty = Decimal(str(exchange.snapshot().get("qty", "0")))
                if qty != 0:
                    exchange.emergency_reduce_only_close(
                        str(abs(qty)), "SELL" if qty > 0 else "BUY"
                    )
            except Exception as close_error:
                raise EmergencyHalt("Emergency recovery uncertain") from close_error
        for client_id in created:
            try:
                exchange.cancel_algo(client_id)
            except Exception as cancel_error:
                raise EmergencyHalt("Orphan protection cannot be canceled") from cancel_error
        if Decimal(str(exchange.snapshot().get("qty", "0"))) != 0:
            raise EmergencyHalt("Position not flat after recovery") from exc
        if exchange.snapshot().get("protection_ids"):
            raise EmergencyHalt("Orphan protection after recovery") from exc
        raise EmergencyHalt("Execution rejected; verified flat") from exc
