"""Binance 400-USDT capital-readiness fixtures: NO HTTP, NO ORDERS."""
import pytest
from live_readiness_policy import evaluate_readonly_binance_futures, validate_conservative_budget

ACCOUNT={
    "status":"CONNECTED_READ_ONLY","mode":"DRY_RUN",
    "account_snapshot_complete":True,
    "position_mode":"ONE_WAY","exchange_flat":True,
    "wallet_usdt":"400","available_usdt":"400",
    "open_positions":0,"open_orders":0,"open_conditional_orders":0,
    "can_trade":False,
}
LIMITS={
    "FUTURES_ORDER_QTY":"10",
    "FUTURES_MAX_NOTIONAL_USDT":"30",
    "FUTURES_MAX_LOSS_USDT":"2",
    "FUTURES_MAX_LEVERAGE":"5",
}
PLAN=dict(
    side="BUY",quantity="10",signal_entry="2",original_stop="1.95",
    original_tp="2.1",usdt_equity="400",available_margin="400",
    planned_margin="5",max_leverage="5",exchange_min_notional="5",
    operator_max_notional="30",operator_max_loss="2",
    taker_fee_rate="0.0005",max_slippage_ratio="0.001",
)

def test_400_usdt_signed_balance_is_not_live_trading_permission():
    x=evaluate_readonly_binance_futures(ACCOUNT,db_path="/tmp/lux-direct-bot.sqlite3",settings=LIMITS)
    assert x["authenticated_available_usdt"]=="400"
    assert x["status"]=="BLOCKED_NON_EXECUTING_DIAGNOSTIC"
    assert x["can_trade"] is False and x["orders_enabled"] is False
    assert x["funding_unblocks_live_execution"] is False
    assert "DURABLE_DATABASE_NOT_CONFIGURED" in x["blocked_reasons"]
    assert "PRODUCTION_ENTRY_SENDER_NOT_CERTIFIED" in x["blocked_reasons"]
    assert "EXCHANGE_NATIVE_SL_AND_TP_FILL_READBACK_NOT_CERTIFIED" in x["blocked_reasons"]
    assert x["requires_new_specific_binance_trade_authorization"] is True

def test_even_full_readback_with_syntactic_limits_still_fails_closed():
    x=evaluate_readonly_binance_futures(ACCOUNT,db_path="/var/data/live.sqlite3",durable_verified=True,settings=LIMITS)
    assert x["status"]=="BLOCKED_NON_EXECUTING_DIAGNOSTIC"
    assert "DURABLE_DATABASE_NOT_CONFIGURED" not in x["blocked_reasons"]
    assert x["can_trade"] is False

@pytest.mark.parametrize("changes,reason",[
    ({"status":"UNVERIFIED"},"AUTHENTICATED_BINANCE_FUTURES_INVENTORY_UNVERIFIED"),
    ({"account_snapshot_complete":False},"FULL_EXCHANGE_ORDER_AND_POSITION_READBACK_REQUIRED"),
    ({"available_usdt":"0"},"POSITIVE_SIGNED_FUTURES_AVAILABLE_BALANCE_UNVERIFIED"),
    ({"available_usdt":"NaN"},"POSITIVE_SIGNED_FUTURES_AVAILABLE_BALANCE_UNVERIFIED"),
    ({"position_mode":"HEDGE"},"ONE_WAY_POSITION_MODE_NOT_CONFIRMED"),
    ({"exchange_flat":False},"FLAT_EXCHANGE_INVENTORY_NOT_VERIFIED"),
])
def test_funding_or_unknown_exchange_state_never_arms_bot(changes,reason):
    x=evaluate_readonly_binance_futures(ACCOUNT|changes,db_path="/tmp/app.sqlite",settings=LIMITS)
    assert reason in x["blocked_reasons"]
    assert x["can_trade"] is False

def test_budget_includes_fee_and_slippage_but_never_sends_order():
    x=validate_conservative_budget(**PLAN)
    assert x["status"]=="OFFLINE_BUDGET_CHECK_PASS_NOT_ORDER_AUTHORITY"
    assert x["orders_sent"]==0
    assert x["can_trade"] is False
    assert float(x["modeled_loss_usdt"])>0.5
    assert float(x["loss_cap_usdt"])==2

@pytest.mark.parametrize("changes,expected",[
    ({"side":"HOLD"},"MISSING_OR_INVALID_ORIGINAL_SIGNAL_OR_LIMITS"),
    ({"quantity":None},"MISSING_OR_INVALID_ORIGINAL_SIGNAL_OR_LIMITS"),
    ({"quantity":True},"MISSING_OR_INVALID_ORIGINAL_SIGNAL_OR_LIMITS"),
    ({"original_stop":"2.05"},"ORIGINAL_LUXALGO_SL_TP_GEOMETRY_INVALID"),
    ({"original_tp":"1.98"},"ORIGINAL_LUXALGO_SL_TP_GEOMETRY_INVALID"),
    ({"max_leverage":"8"},"LEVERAGE_OR_AVAILABLE_MARGIN_EXCEEDED"),
    ({"planned_margin":"0.5"},"INSUFFICIENT_LEVERAGED_MARGIN"),
    ({"planned_margin":"500"},"LEVERAGE_OR_AVAILABLE_MARGIN_EXCEEDED"),
    ({"operator_max_notional":"10"},"EXCHANGE_MINIMUM_OR_OWNER_MAXIMUM_NOTIONAL"),
    ({"exchange_min_notional":"40"},"EXCHANGE_MINIMUM_OR_OWNER_MAXIMUM_NOTIONAL"),
    ({"operator_max_loss":"0.1"},"MODELED_LOSS_INCLUDING_FEES_EXCEEDS_BUDGET"),
    ({"usdt_equity":"10"},"MODELED_LOSS_INCLUDING_FEES_EXCEEDS_BUDGET"),
    ({"taker_fee_rate":"NaN"},"UNVERIFIED_FEES_OR_SLIPPAGE_LIMIT"),
    ({"max_slippage_ratio":"0.003"},"UNVERIFIED_FEES_OR_SLIPPAGE_LIMIT"),
])
def test_malformed_and_over_budget_plans_rejected_without_orders(changes,expected):
    x=validate_conservative_budget(**(PLAN|changes))
    assert x["status"]=="REJECT_NO_ORDER"
    assert x["reason"]==expected
    assert x["orders_sent"]==0


def test_requested_three_percent_outer_budget_remains_offline_only():
    # Illustrative equity from a fixture, NOT a verified live Binance balance.
    params = PLAN | {
        "usdt_equity": "400",
        "operator_max_loss": "20",
        "original_stop": "1.10",
    }
    x = validate_conservative_budget(**params)
    assert x["status"] == "OFFLINE_BUDGET_CHECK_PASS_NOT_ORDER_AUTHORITY"
    assert x["loss_cap_usdt"] == "12.00"
    assert x["orders_sent"] == 0 and x["can_trade"] is False


def test_requested_three_percent_limit_vetoes_over_budget_signal():
    params = PLAN | {
        "usdt_equity": "400",
        "operator_max_loss": "20",
        "original_stop": "0.50",
    }
    x = validate_conservative_budget(**params)
    assert x["status"] == "REJECT_NO_ORDER"
    assert x["reason"] == "MODELED_LOSS_INCLUDING_FEES_EXCEEDS_BUDGET"
    assert x["orders_sent"] == 0


def test_short_signal_levels_are_not_rewritten():
    params=PLAN|{"side":"SELL","original_stop":"2.05","original_tp":"1.90"}
    x=validate_conservative_budget(**params)
    assert x["status"]=="OFFLINE_BUDGET_CHECK_PASS_NOT_ORDER_AUTHORITY"
    assert x["orders_sent"]==0


def test_private_readiness_endpoint_denies_public_access_and_never_enables_orders(monkeypatch,tmp_path):
    import importlib
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    monkeypatch.setenv("WEBHOOK_SECRET","test-webhook-ready")
    monkeypatch.setenv("ADMIN_TOKEN","test-operator-ready")
    monkeypatch.setenv("DB_PATH",str(tmp_path/"test-readiness.sqlite"))
    for k,v in LIMITS.items():
        monkeypatch.setenv(k,v)
    import app
    bot=importlib.reload(app)
    with TestClient(bot.app) as c:
        assert c.get("/operator/live-readiness").status_code==403
        with patch.object(bot,"snapshot_read_only",return_value=ACCOUNT):
            r=c.get("/operator/live-readiness",headers={"X-Admin-Token":"test-operator-ready"})
        assert r.status_code==200
        assert r.headers["cache-control"]=="private, no-store"
        result=r.json()
        assert result["authenticated_available_usdt"]=="400"
        assert result["status"]=="BLOCKED_NON_EXECUTING_DIAGNOSTIC"
        assert result["orders_enabled"] is False
        assert result["can_trade"] is False
        assert "PRODUCTION_ENTRY_SENDER_NOT_CERTIFIED" in result["blocked_reasons"]
        assert c.get("/health").json()["live_trading"] is False
