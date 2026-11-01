"""Independent Binance USD-M signed-GET verification; never sends money or orders.

Only used on a private operator endpoint. This proves what Binance returned
at read time, not TradingView origin, write permissions or trading acceptance.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import os
import httpx

from binance_transport import BinanceReadOnly, BinanceTransportBlocked

TARGET_SYMBOL = "XRPUSDT"  # Matches the ONLY supported offline execution plan.
NON_EXECUTION_BARRIERS = [
    "REAL_TRADINGVIEW_TRIGGER_NOT_INDEPENDENTLY_VERIFIED",
    "LIVE_ENTRY_AND_NATIVE_PROTECTION_NOT_EXCHANGE_CERTIFIED",
    "RESTART_RECOVERY_AND_EMERGENCY_CLOSE_NOT_EXCHANGE_CERTIFIED",
    "LIVE_ORDER_SENDER_HARD_LOCKED",
]

def nonnegative(value, label):
    if value is None or isinstance(value, bool):
        raise ValueError(label + "_MISSING")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(label + "_INVALID")
    if not result.is_finite() or result < 0:
        raise ValueError(label + "_INVALID")
    return result

def signed_exchange_verification(client=None, symbol=TARGET_SYMBOL):
    safe = dict(exchange="BINANCE_USDT_M", mode="DRY_RUN", can_trade=False,
                orders_enabled=False, acceptance_certified=False, orders_sent=0,
                target_symbol=symbol, blocked_reasons=list(NON_EXECUTION_BARRIERS))
    if symbol != TARGET_SYMBOL:
        return dict(safe, status="BLOCKED", reason="EXECUTION_PLAN_SUPPORTS_XRPUSDT_ONLY")
    key = os.environ.get("BINANCE_API_KEY", "")
    secret = os.environ.get("BINANCE_API_SECRET", "")
    if not key or not secret:
        return dict(safe, status="NOT_CONFIGURED", reason="BINANCE_API_KEYS_NOT_AVAILABLE")
    broker = client if client is not None else BinanceReadOnly(key, secret)
    try:
        account = broker.get("/fapi/v3/account")
        account_config = broker.get("/fapi/v1/accountConfig")
        positions = broker.get("/fapi/v3/positionRisk")
        open_orders = broker.get("/fapi/v1/openOrders")
        open_algos = broker.get("/fapi/v1/openAlgoOrders")
        mode = broker.get("/fapi/v1/positionSide/dual")
        symbol_configs = broker.get("/fapi/v1/symbolConfig", {"symbol": symbol})
        if not isinstance(account, dict) or not isinstance(account_config, dict) or not all(
            isinstance(x, list) for x in (positions, open_orders, open_algos, symbol_configs)
        ) or not isinstance(mode, dict):
            raise ValueError("INCOMPLETE_SIGNED_INVENTORY")
        # Binance v3 account intentionally omits configuration fields.
        # Official /fapi/v1/accountConfig owns multiAssetsMargin and canTrade.
        if type(account_config.get("multiAssetsMargin")) is not bool:
            raise ValueError("ACCOUNT_MODE_NOT_VERIFIED")
        if type(account_config.get("canTrade")) is not bool:
            raise ValueError("FUTURES_CAN_TRADE_FLAG_UNVERIFIED")
        if type(mode.get("dualSidePosition")) is not bool or (
            type(account_config.get("dualSidePosition")) is not bool
            or account_config["dualSidePosition"] != mode["dualSidePosition"]
        ):
            raise ValueError("POSITION_MODE_CONFLICT_OR_UNVERIFIED")
        if any(len(x) >= 1000 for x in (positions, open_orders, open_algos)):
            raise ValueError("INVENTORY_TRUNCATION_CANNOT_BE_EXCLUDED")
        if not all(isinstance(row, dict) and isinstance(row.get("symbol"), str)
                   for rows in (positions, open_orders, open_algos) for row in rows):
            raise ValueError("UNVERIFIED_EXCHANGE_ORDER_OR_POSITION_ROW")
        position_count = 0
        for row in positions:
            qty = nonnegative(abs(Decimal(str(row.get("positionAmt", "NaN")))), "POSITION")
            position_count += int(qty != 0)
        matches = [v for v in symbol_configs
                   if isinstance(v, dict) and v.get("symbol") == symbol]
        if len(matches) != 1:
            raise ValueError("TARGET_SYMBOL_SETTINGS_UNVERIFIED")
        config = matches[0]
        if not isinstance(config.get("marginType"), str) or type(config.get("leverage")) is not int:
            raise ValueError("TARGET_MARGIN_AND_LEVERAGE_UNVERIFIED")
        assets = account.get("assets")
        if not isinstance(assets, list):
            raise ValueError("ACCOUNT_ASSET_INVENTORY_UNVERIFIED")
        usdt = [v for v in assets if isinstance(v, dict) and v.get("asset") == "USDT"]
        if len(usdt) != 1:
            raise ValueError("USDT_EQUITY_UNVERIFIED")
        wallet = nonnegative(usdt[0].get("walletBalance"), "USDT_WALLET")
        equity = nonnegative(usdt[0].get("marginBalance"), "USDT_MARGIN_EQUITY")
        available = nonnegative(usdt[0].get("availableBalance"), "USDT_AVAILABLE")
        if available > equity:
            raise ValueError("AVAILABLE_EXCEEDS_EQUITY")
        # Multi-assets portfolios must not be budgeted using a plain USDT cap.
        if not account_config["multiAssetsMargin"]:
            total = nonnegative(account.get("totalMarginBalance"), "TOTAL_MARGIN_BALANCE")
            if abs(total - equity) > Decimal("0.00000001"):
                raise ValueError("ACCOUNT_EQUITY_INCONSISTENT")
        blockers = list(NON_EXECUTION_BARRIERS)
        if account_config["multiAssetsMargin"]:
            blockers.append("MULTI_ASSETS_EQUITY_POLICY_NOT_SUPPORTED")
        if not account_config["canTrade"]:
            blockers.append("BINANCE_REPORTS_TRADING_NOT_ALLOWED")
        if mode["dualSidePosition"]:
            blockers.append("HEDGE_MODE_NOT_SUPPORTED_BY_EXECUTION_PLAN")
        if config["marginType"].upper() not in ("CROSSED", "CROSS"):
            blockers.append("TARGET_SYMBOL_NOT_CROSS_MARGIN")
        if config["leverage"] != 5:
            blockers.append("TARGET_SYMBOL_NOT_SET_TO_5X")
        if position_count or open_orders or open_algos:
            blockers.append("EXCHANGE_NOT_FLAT_EXISTING_POSITIONS_OR_ORDERS")
        if equity == 0:
            blockers.append("ZERO_VERIFIED_FUTURES_EQUITY")
        if available < Decimal("60"):
            blockers.append("AVAILABLE_USDT_BELOW_60_BEFORE_FEES")
        return dict(safe, status="CONNECTED_READ_ONLY", source="BINANCE_SIGNED_GET",
            snapshot_utc=datetime.now(timezone.utc).isoformat(),
            usdt_equity=str(equity), equity_basis="USDT Futures marginBalance (wallet + unrealized PnL)",
            usdt_wallet=str(wallet), available_usdt=str(available),
            modeled_loss_budget_3pct=str(equity * Decimal("0.03")),
            multi_assets_margin=account_config["multiAssetsMargin"],
            position_mode="HEDGE" if mode["dualSidePosition"] else "ONE_WAY",
            target_margin_type=config["marginType"], target_leverage=config["leverage"],
            positions_open=position_count, orders_open=len(open_orders),
            protective_algos_open=len(open_algos), account_snapshot_complete=True,
            blocked_reasons=blockers)
    except httpx.HTTPStatusError as error:
        # Safe diagnostic only: NEVER expose response text, full URL with signature,
        # request headers, API key, params, order IDs, or raw exception repr.
        permitted = {
            "/fapi/v3/account", "/fapi/v1/accountConfig", "/fapi/v3/positionRisk",
            "/fapi/v1/openOrders", "/fapi/v1/openAlgoOrders",
            "/fapi/v1/positionSide/dual", "/fapi/v1/symbolConfig",
        }
        path = error.request.url.path
        endpoint = path if path in permitted else "UNKNOWN_SIGNED_GET"
        code = error.response.status_code
        code = code if isinstance(code, int) and 400 <= code <= 599 else 0
        binance_code = None
        try:
            data = error.response.json()
            value = data.get("code") if isinstance(data, dict) else None
            if type(value) is int and -100000 <= value <= 100000:
                binance_code = value
        except (ValueError, TypeError):
            pass
        return dict(safe, status="READBACK_UNVERIFIED",
                    reason="HTTPSTATUSERROR_FAIL_CLOSED",
                    failed_endpoint=endpoint, http_status=code,
                    binance_code=binance_code)
    except (httpx.HTTPError, BinanceTransportBlocked, ValueError, TypeError,
            KeyError, ArithmeticError, OverflowError) as error:
        # Never return secrets, request signatures, exchange order IDs, or raw errors.
        return dict(safe, status="READBACK_UNVERIFIED",
                    reason=type(error).__name__.upper() + "_FAIL_CLOSED")
