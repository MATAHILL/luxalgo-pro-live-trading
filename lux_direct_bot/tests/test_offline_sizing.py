"""Offline sizing fixtures never reach the exchange or modify the deployed bot."""
import pytest
from offline_sizing import plan_offline_300_cross, OfflineSizingBlocked

ACCOUNT = {
    "status": "CONNECTED_READ_ONLY",
    "account_snapshot_complete": True,
    "position_mode": "ONE_WAY",
    "exchange_flat": True,
    "open_positions": 0,
    "open_orders": 0,
    "open_conditional_orders": 0,
    "futures_equity_usdt": "400",
    "available_usdt": "400",
}
SYMBOL = {
    "symbol": "XRPUSDT", "margin_type": "cross",
    "leverage": "5", "metadata_verified": True,
}
PLAN = dict(
    side="BUY", symbol="XRPUSDT", signal_entry="2",
    signal_sl="1.950", signal_tp="2.100",
    futures_equity_usdt="400", available_usdt="400",
    qty_step="0.1", price_tick="0.001",
    min_exchange_notional="5", taker_fee_rate="0.0005",
    max_slippage_ratio="0.001",
    verified_account=ACCOUNT, verified_symbol=SYMBOL,
)

def test_300_usdt_60_margin_5x_cross_is_offline_only():
    p = plan_offline_300_cross(**PLAN)
    assert p["status"] == "OFFLINE_SIZING_CHECK_PASS_NO_ORDER_AUTHORITY"
    assert p["leverage"] == "5" and p["margin_type"] == "cross"
    assert p["planned_margin_usdt"] == "60"
    assert p["original_side"] == "BUY"
    assert p["original_entry"] == "2"
    assert p["original_sl"] == "1.950" and p["original_tp"] == "2.100"
    assert p["max_modeled_loss_usdt"] == "12.00"
    assert float(p["worst_notional_usdt"]) <= 300
    assert float(p["nominal_notional_usdt"]) < 300
    assert p["orders_enabled"] is False and p["orders_sent"] == 0
    assert p["can_trade"] is False

@pytest.mark.parametrize("overrides", [
    {"verified_account": ACCOUNT | {"position_mode": "HEDGE"}},
    {"verified_account": ACCOUNT | {"exchange_flat": False}},
    {"verified_account": ACCOUNT | {"open_orders": 1}},
    {"verified_symbol": SYMBOL | {"margin_type": "isolated"}},
    {"verified_symbol": SYMBOL | {"leverage": "20"}},
    {"verified_symbol": SYMBOL | {"metadata_verified": False}},
    {"available_usdt": "60", "verified_account": ACCOUNT | {"available_usdt": "60"}},
    {"futures_equity_usdt": "50", "verified_account": ACCOUNT | {"futures_equity_usdt": "50"}},
    {"signal_sl": "1.9505"},
    {"signal_tp": "1.900"},
    {"max_slippage_ratio": "0.004"},
    {"taker_fee_rate": "invalid"},
    {"verified_account": ACCOUNT | {"futures_equity_usdt": "300"}},
    {"verified_account": ACCOUNT | {"available_usdt": "200"}},
    {"operator_absolute_loss_cap": "1"},
])
def test_fail_closed_when_snapshot_sizing_or_protection_invalid(overrides):
    with pytest.raises(OfflineSizingBlocked):
        plan_offline_300_cross(**(PLAN | overrides))

def test_original_bearish_confirmation_keeps_side_and_prices():
    p = plan_offline_300_cross(**(
        PLAN | {"side": "SELL", "signal_sl": "2.050", "signal_tp": "1.900"}
    ))
    assert p["original_side"] == "SELL"
    assert p["original_sl"] == "2.050"
    assert p["original_tp"] == "1.900"
    assert p["can_trade"] is False
