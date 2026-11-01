import pytest
from order_plan import create_protected_plan, ExecutionBlocked, LIVE_ORDERS_ENABLED, ENTRY_ENDPOINT, PROTECTIVE_ENDPOINT

BASE = dict(
    symbol="XRPUSDT", direction="BUY", signal_id="signal-1234567",
    quantity="10", reference_price="2.00", stop_price="1.95",
    take_profit_price="2.10", quantity_step="0.1", price_tick="0.001",
    minimum_notional="5", maximum_notional="40", maximum_loss="1",
)

def test_only_original_buy_sell_and_native_protection():
    plan = create_protected_plan(**BASE)
    assert plan.entry["side"] == "BUY"
    assert plan.entry["type"] == "MARKET"
    assert plan.stop["side"] == "SELL"
    assert plan.take_profit["side"] == "SELL"
    assert plan.stop["type"] == "STOP_MARKET"
    assert plan.take_profit["type"] == "TAKE_PROFIT_MARKET"
    assert plan.stop["closePosition"] == "true"
    assert plan.max_loss_usdt == "0.50"
    assert ENTRY_ENDPOINT.endswith("/fapi/v1/order")
    assert PROTECTIVE_ENDPOINT.endswith("/fapi/v1/algoOrder")
    assert LIVE_ORDERS_ENABLED is False

def test_short_signal_not_modified():
    data = BASE | dict(direction="SELL", stop_price="2.05", take_profit_price="1.90")
    plan = create_protected_plan(**data)
    assert plan.entry["side"] == "SELL"
    assert plan.stop["side"] == "BUY"
    assert plan.take_profit["side"] == "BUY"

@pytest.mark.parametrize("update", [
    {"symbol": "BTCUSDT"},
    {"direction": "HOLD"},
    {"quantity": "-2"},
    {"quantity": "2.33"},
    {"quantity": "1"},
    {"reference_price": "0"},
    {"reference_price": "100"},
    {"stop_price": "2.05"},
    {"stop_price": "1.9515"},
    {"take_profit_price": "1.9"},
    {"maximum_loss": "0.01"},
    {"signal_id": "short"},
    {"quantity_step": "0"},
])
def test_invalid_or_overbudget_refused(update):
    with pytest.raises(ExecutionBlocked):
        create_protected_plan(**(BASE | update))


@pytest.mark.parametrize("direction,stop,target,side,exit_side", [
    ("BUY","1.95","2.10","LONG","SELL"),
    ("SELL","2.05","1.90","SHORT","BUY"),
])
def test_hedge_entries_and_native_close_all_sl_tp_are_position_side_specific(
        direction,stop,target,side,exit_side):
    plan=create_protected_plan(**(BASE | {
        "direction":direction,"stop_price":stop,"take_profit_price":target,
        "position_mode":"HEDGE",
    }))
    assert plan.entry["positionSide"]==side
    assert plan.entry["side"]==direction
    assert plan.entry["type"]=="MARKET"
    assert plan.stop["positionSide"]==side and plan.take_profit["positionSide"]==side
    assert plan.stop["side"]==exit_side and plan.take_profit["side"]==exit_side
    assert plan.stop["closePosition"]==plan.take_profit["closePosition"]=="true"
    for payload in (plan.entry,plan.stop,plan.take_profit):
        assert "reduceOnly" not in payload
    for payload in (plan.stop,plan.take_profit):
        assert "quantity" not in payload
        assert payload["workingType"]=="MARK_PRICE"
    assert plan.stop["triggerPrice"]==stop
    assert plan.take_profit["triggerPrice"]==target
    assert LIVE_ORDERS_ENABLED is False

@pytest.mark.parametrize("bad_mode", ["HEDGE_SHORT", "BOTH", "", None])
def test_unrecognized_position_modes_block_order_plan(bad_mode):
    with pytest.raises(ExecutionBlocked):
        create_protected_plan(**(BASE | {"position_mode":bad_mode}))
