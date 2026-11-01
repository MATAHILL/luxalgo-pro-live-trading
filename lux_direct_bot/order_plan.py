"""Binance XRPUSDT order construction, no network requests or credential access.

This module can construct MARKET and exchange-native conditional order payloads,
but it cannot execute them. Activating live execution requires a separate reviewed
sender, actual verified TradingView webhook, and exchange acceptance test.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib

class ExecutionBlocked(ValueError):
    pass

def positive_decimal(value, label):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result <= 0:
            raise ExecutionBlocked(label + " must be positive")
        return result
    except (InvalidOperation, TypeError):
        raise ExecutionBlocked(label + " must be numeric")

def aligned(value, step):
    return value % step == 0

@dataclass(frozen=True)
class ProtectedPlan:
    symbol: str
    direction: str
    quantity: str
    entry: dict
    stop: dict
    take_profit: dict
    exposure_usdt: str
    max_loss_usdt: str

def create_protected_plan(
    *, symbol, direction, signal_id, quantity, reference_price,
    stop_price, take_profit_price, quantity_step, price_tick,
    minimum_notional, maximum_notional, maximum_loss, position_mode="ONE_WAY"
):
    """Fail closed unless all broker restrictions and user-set limits are supplied.

    Prices and sizing are caller-supplied; this code does not infer or alter
    the LuxAlgo BUY/SELL direction. One-way or explicitly verified Hedge mode.
    """
    if symbol != "XRPUSDT" or direction not in ("BUY", "SELL"):
        raise ExecutionBlocked("Only XRPUSDT BUY/SELL permitted")
    if position_mode not in ("ONE_WAY", "HEDGE"):
        raise ExecutionBlocked("Signed Binance position mode must be ONE_WAY or HEDGE")
    if not isinstance(signal_id, str) or len(signal_id) < 12:
        raise ExecutionBlocked("Unique signal id required")
    qty = positive_decimal(quantity, "quantity")
    reference = positive_decimal(reference_price, "reference_price")
    stop = positive_decimal(stop_price, "stop_price")
    tp = positive_decimal(take_profit_price, "take_profit_price")
    qty_step = positive_decimal(quantity_step, "quantity_step")
    tick = positive_decimal(price_tick, "price_tick")
    minimum = positive_decimal(minimum_notional, "minimum_notional")
    ceiling = positive_decimal(maximum_notional, "maximum_notional")
    max_loss = positive_decimal(maximum_loss, "maximum_loss")
    if not aligned(qty, qty_step) or not aligned(stop, tick) or not aligned(tp, tick):
        raise ExecutionBlocked("Quantity and trigger prices must align to exchange steps")
    if direction == "BUY" and not (stop < reference < tp):
        raise ExecutionBlocked("LONG protection geometry invalid")
    if direction == "SELL" and not (tp < reference < stop):
        raise ExecutionBlocked("SHORT protection geometry invalid")
    notional = qty * reference
    modeled_loss = qty * abs(reference - stop)
    if not minimum <= notional <= ceiling:
        raise ExecutionBlocked("Notional outside exchange minimum/user maximum")
    if modeled_loss > max_loss:
        raise ExecutionBlocked("Stop-loss budget exceeded")
    opposite = "SELL" if direction == "BUY" else "BUY"
    # In Hedge mode positionSide is mandatory: BUY enters LONG, SELL enters SHORT.
    # STOP_MARKET / TAKE_PROFIT_MARKET closePosition=true uses the SAME
    # positionSide as the entry, but the OPPOSITE side for closing.
    # Never send reduceOnly or quantity alongside closePosition=true.
    position_side = "BOTH" if position_mode == "ONE_WAY" else (
        "LONG" if direction == "BUY" else "SHORT"
    )
    id_suffix = hashlib.sha256(signal_id.encode("utf-8")).hexdigest()[:20]
    entry = {
        "symbol": symbol, "side": direction, "type": "MARKET",
        "quantity": str(qty), "positionSide": position_side,
        "newClientOrderId": "luxent-" + id_suffix,
    }
    conditional = {
        "algoType": "CONDITIONAL", "symbol": symbol, "side": opposite,
        "positionSide": position_side, "closePosition": "true",
        "workingType": "MARK_PRICE",
    }
    sl = dict(conditional, type="STOP_MARKET", triggerPrice=str(stop),
              clientAlgoId="luxsl-" + id_suffix)
    take = dict(conditional, type="TAKE_PROFIT_MARKET",
                triggerPrice=str(tp), clientAlgoId="luxtp-" + id_suffix)
    return ProtectedPlan(
        symbol, direction, str(qty), entry, sl, take,
        str(notional), str(modeled_loss),
    )

ENTRY_ENDPOINT = "POST /fapi/v1/order"
PROTECTIVE_ENDPOINT = "POST /fapi/v1/algoOrder"
LIVE_ORDERS_ENABLED = False  # Contract-construction code has NO HTTP writes.
