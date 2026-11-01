"""Fail-closed, offline-only sizing for a proposed 300-USDT, 5x Cross trade.

Does NOT call Binance, configure leverage/margin, create orders, or certify a
connected account. Inputs describing account state must come from independent
signed readback before they may be relied upon for eventual acceptance.
"""
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from live_readiness_policy import validate_conservative_budget

class OfflineSizingBlocked(ValueError):
    pass

def d(value, name):
    if value is None or isinstance(value, bool):
        raise OfflineSizingBlocked(name + "_MISSING")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise OfflineSizingBlocked(name + "_INVALID")
    if not number.is_finite() or number <= 0:
        raise OfflineSizingBlocked(name + "_INVALID")
    return number

def plan_offline_300_cross(
    *, side, symbol, signal_entry, signal_sl, signal_tp,
    futures_equity_usdt, available_usdt, qty_step, price_tick,
    min_exchange_notional, taker_fee_rate, max_slippage_ratio,
    verified_account, verified_symbol, operator_absolute_loss_cap=None,
):
    """Never adjust signal prices; only round DOWN the proposed quantity.

    Sizing is computed using worst permissible entry slippage. A missing or
    conflicting readback yields a rejection. No order payload is returned.
    """
    if side not in ("BUY", "SELL") or not isinstance(symbol, str):
        raise OfflineSizingBlocked("INVALID_ORIGINAL_LUXALGO_DIRECTION_OR_SYMBOL")
    if not isinstance(verified_account, dict) or (
        verified_account.get("status") != "CONNECTED_READ_ONLY"
        or verified_account.get("account_snapshot_complete") is not True
        or verified_account.get("position_mode") != "ONE_WAY"
        or verified_account.get("exchange_flat") is not True
        or verified_account.get("open_positions") != 0
        or verified_account.get("open_orders") != 0
        or verified_account.get("open_conditional_orders") != 0
    ):
        raise OfflineSizingBlocked("SIGNED_COMPLETE_FLAT_ONE_WAY_SNAPSHOT_REQUIRED")
    if not isinstance(verified_symbol, dict) or (
        verified_symbol.get("symbol") != symbol
        or verified_symbol.get("margin_type") != "cross"
        or str(verified_symbol.get("leverage")) != "5"
        or verified_symbol.get("metadata_verified") is not True
    ):
        raise OfflineSizingBlocked("SIGNED_CROSS_5X_AND_SYMBOL_METADATA_REQUIRED")
    equity = d(futures_equity_usdt, "FUTURES_EQUITY")
    available = d(available_usdt, "AVAILABLE_BALANCE")
    if (d(verified_account.get("futures_equity_usdt"), "SIGNED_EQUITY") != equity
            or d(verified_account.get("available_usdt"), "SIGNED_AVAILABLE") != available):
        raise OfflineSizingBlocked("BALANCE_INPUT_DOES_NOT_MATCH_SIGNED_SNAPSHOT")
    entry = d(signal_entry, "ORIGINAL_ENTRY")
    stop = d(signal_sl, "ORIGINAL_STOP")
    tp = d(signal_tp, "ORIGINAL_TP")
    step = d(qty_step, "QUANTITY_STEP")
    tick = d(price_tick, "PRICE_TICK")
    minimum = d(min_exchange_notional, "MINIMUM_NOTIONAL")
    try:
        fee = Decimal(str(taker_fee_rate))
        slip = Decimal(str(max_slippage_ratio))
    except (InvalidOperation, TypeError, ValueError):
        raise OfflineSizingBlocked("FEES_OR_SLIPPAGE_UNVERIFIED")
    if not (fee.is_finite() and Decimal(0) <= fee <= Decimal("0.01")
            and slip.is_finite() and Decimal(0) <= slip <= Decimal("0.001")):
        raise OfflineSizingBlocked("FEES_OR_SLIPPAGE_UNVERIFIED")
    if stop % tick or tp % tick:
        raise OfflineSizingBlocked("ORIGINAL_LUXALGO_TRIGGERS_NOT_EXCHANGE_ALIGNED")
    # Do not claim exact 300-USDT fill: slippage, quantity steps, and fees matter.
    notional_limit = Decimal("300")
    planned_margin = Decimal("60")
    leverage = Decimal("5")
    nominal_quantity_ceiling = min(notional_limit, planned_margin * leverage) / (entry * (1 + slip))
    quantity = (nominal_quantity_ceiling / step).to_integral_value(rounding=ROUND_DOWN) * step
    if quantity <= 0:
        raise OfflineSizingBlocked("ROUNDING_PRODUCES_ZERO_QUANTITY")
    cap = equity * Decimal("0.03")
    if operator_absolute_loss_cap is not None:
        cap = min(cap, d(operator_absolute_loss_cap, "ABSOLUTE_LOSS_CAP"))
    assessed = validate_conservative_budget(
        side=side, quantity=quantity, signal_entry=entry,
        original_stop=stop, original_tp=tp, usdt_equity=equity,
        available_margin=available, planned_margin=planned_margin,
        max_leverage=leverage, exchange_min_notional=minimum,
        operator_max_notional=notional_limit, operator_max_loss=cap,
        taker_fee_rate=fee, max_slippage_ratio=slip,
    )
    if assessed["status"] != "OFFLINE_BUDGET_CHECK_PASS_NOT_ORDER_AUTHORITY":
        raise OfflineSizingBlocked(assessed["reason"])
    # Fees also consume available collateral, not just initial margin.
    fee_reserve = quantity * entry * (1 + slip) * fee * 2
    if available < planned_margin + fee_reserve:
        raise OfflineSizingBlocked("AVAILABLE_BALANCE_BELOW_MARGIN_PLUS_FEES")
    return {
        "status": "OFFLINE_SIZING_CHECK_PASS_NO_ORDER_AUTHORITY",
        "symbol": symbol, "original_side": side,
        "original_entry": str(entry), "original_sl": str(stop), "original_tp": str(tp),
        "quantity": str(quantity),
        "nominal_notional_usdt": str(quantity * entry),
        "worst_notional_usdt": assessed["worst_notional_usdt"],
        "planned_margin_usdt": str(planned_margin),
        "leverage": "5", "margin_type": "cross",
        "max_modeled_loss_usdt": str(cap),
        "modeled_loss_usdt": assessed["modeled_loss_usdt"],
        "balance_basis": "authenticated Binance USD-M Futures equity input, not wallet deposit",
        "orders_enabled": False, "orders_sent": 0, "can_trade": False,
    }
