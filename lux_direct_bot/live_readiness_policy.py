"""LUX DIRECT BOT Binance production preflight, read-only and always fail-closed.

This evaluates *evidence* against explicit operator caps and mandatory real
exchange controls. It never generates signals, chooses directions or prices,
places orders, switches trading on, or treats a 400 USDT deposit as consent.

Real-money activation requires an independent acceptance-test system that does
not exist in this dry-run service; all reports deliberately keep can_trade=False.
"""
from decimal import Decimal, InvalidOperation
from pathlib import Path

MANDATORY_BARRIERS = (
    "PERSISTENT_TRANSACTIONAL_SIGNAL_AND_ORDER_LEDGER_REQUIRED",
    "GENUINE_TRADINGVIEW_ALERT_ORIGIN_AND_LEVELS_NOT_INDEPENDENTLY_CERTIFIED",
    "PRODUCTION_ENTRY_SENDER_NOT_CERTIFIED",
    "EXCHANGE_NATIVE_SL_AND_TP_FILL_READBACK_NOT_CERTIFIED",
    "ONE_SHOT_EMERGENCY_CLOSE_AND_RESTART_RECOVERY_NOT_CERTIFIED",
    "BOUNDED_BINANCE_LIVE_ACCEPTANCE_TRADE_NOT_AUTHORIZED_OR_PASSED",
    "FINAL_AUTOMATIC_TRADING_CERTIFICATION_MISSING",
)

def decimal_positive(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        d = Decimal(str(value))
        return d if d.is_finite() and d > 0 else None
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        return None

def evaluate_readonly_binance_futures(account, *, db_path, durable_verified=False, settings=None):
    """Produce authenticated readback findings without granting live authority."""
    settings=settings or {}
    reasons=[]
    if not isinstance(account,dict) or account.get("status")!="CONNECTED_READ_ONLY":
        reasons.append("AUTHENTICATED_BINANCE_FUTURES_INVENTORY_UNVERIFIED")
        account={}
    if account.get("account_snapshot_complete") is not True:
        reasons.append("FULL_EXCHANGE_ORDER_AND_POSITION_READBACK_REQUIRED")
    available=decimal_positive(account.get("available_usdt"))
    wallet=decimal_positive(account.get("wallet_usdt"))
    if available is None or wallet is None or available>wallet:
        reasons.append("POSITIVE_SIGNED_FUTURES_AVAILABLE_BALANCE_UNVERIFIED")
    mode=account.get("position_mode")
    if mode not in ("ONE_WAY","HEDGE"):
        reasons.append("SIGNED_POSITION_MODE_NOT_CONFIRMED")
    elif mode=="HEDGE":
        reasons.append("HEDGE_NATIVE_PROTECTION_AND_RECOVERY_NOT_CERTIFIED")
    if account.get("exchange_flat") is not True:
        reasons.append("FLAT_EXCHANGE_INVENTORY_NOT_VERIFIED")
    if durable_verified is not True or not isinstance(db_path,str) or not db_path.startswith("/var/data/"):
        reasons.append("DURABLE_DATABASE_NOT_CONFIGURED")
    if settings.get("FUTURES_ORDER_QTY") is None or decimal_positive(settings.get("FUTURES_ORDER_QTY")) is None:
        reasons.append("EXPLICIT_ORDER_QUANTITY_NOT_CONFIGURED")
    if decimal_positive(settings.get("FUTURES_MAX_NOTIONAL_USDT")) is None:
        reasons.append("EXPLICIT_NOTIONAL_LIMIT_NOT_CONFIGURED")
    if decimal_positive(settings.get("FUTURES_MAX_LOSS_USDT")) is None:
        reasons.append("EXPLICIT_LOSS_LIMIT_NOT_CONFIGURED")
    lev=decimal_positive(settings.get("FUTURES_MAX_LEVERAGE"))
    if lev is None or lev>5:
        reasons.append("LEVERAGE_CAP_1_TO_5_REQUIRED")
    return {
        "status":"BLOCKED_NON_EXECUTING_DIAGNOSTIC",
        "mode":"DRY_RUN",
        "account_status":account.get("status","UNVERIFIED"),
        "authenticated_available_usdt":str(available) if available is not None else None,
        "open_positions":account.get("open_positions") if account.get("account_snapshot_complete") is True else None,
        "open_orders":account.get("open_orders") if account.get("account_snapshot_complete") is True else None,
        "open_conditional_orders":account.get("open_conditional_orders") if account.get("account_snapshot_complete") is True else None,
        "current_order_plan_scope":"XRPUSDT_ONLY_OFFLINE",
        "funding_unblocks_live_execution":False,
        "blocked_reasons":sorted(set(reasons+list(MANDATORY_BARRIERS))),
        "can_trade":False,
        "orders_enabled":False,
        "requires_new_specific_binance_trade_authorization":True,
    }

def validate_conservative_budget(
    *, side, quantity, signal_entry, original_stop, original_tp,
    usdt_equity, available_margin, planned_margin, max_leverage,
    exchange_min_notional, operator_max_notional, operator_max_loss,
    taker_fee_rate, max_slippage_ratio
):
    """Offline-only veto, no price substitution or adjustment.

    Worst modeled loss includes entry/exit slippage and both taker fees.
    Actual market losses may exceed the model because of gaps or latency.
    """
    vals={
        k: decimal_positive(v) for k,v in {
            "quantity":quantity,"entry":signal_entry,"stop":original_stop,
            "tp":original_tp,"equity":usdt_equity,
            "available":available_margin,"margin":planned_margin,
            "leverage":max_leverage,"min_notional":exchange_min_notional,
            "notional_cap":operator_max_notional,"loss_cap":operator_max_loss,
        }.items()
    }
    if any(v is None for v in vals.values()) or side not in ("BUY","SELL"):
        return {"status":"REJECT_NO_ORDER","reason":"MISSING_OR_INVALID_ORIGINAL_SIGNAL_OR_LIMITS","orders_sent":0}
    try:
        fee=Decimal(str(taker_fee_rate))
        slip=Decimal(str(max_slippage_ratio))
    except (InvalidOperation,TypeError,ValueError):
        fee=slip=Decimal("-1")
    if any(not x.is_finite() or x<0 for x in (fee,slip)) or slip>Decimal("0.001"):
        return {"status":"REJECT_NO_ORDER","reason":"UNVERIFIED_FEES_OR_SLIPPAGE_LIMIT","orders_sent":0}
    p=vals
    if not ((p["stop"]<p["entry"]<p["tp"]) if side=="BUY"
            else (p["tp"]<p["entry"]<p["stop"])):
        return {"status":"REJECT_NO_ORDER","reason":"ORIGINAL_LUXALGO_SL_TP_GEOMETRY_INVALID","orders_sent":0}
    if p["leverage"]>5 or p["margin"]>p["available"]:
        return {"status":"REJECT_NO_ORDER","reason":"LEVERAGE_OR_AVAILABLE_MARGIN_EXCEEDED","orders_sent":0}
    nominal=p["quantity"]*p["entry"]
    worst_notional=nominal*(1+slip)
    risk=p["quantity"]*(abs(p["entry"]-p["stop"])+2*p["entry"]*(slip+fee))
    if p["margin"]*p["leverage"]<worst_notional:
        return {"status":"REJECT_NO_ORDER","reason":"INSUFFICIENT_LEVERAGED_MARGIN","orders_sent":0}
    if worst_notional<p["min_notional"] or worst_notional>p["notional_cap"]:
        return {"status":"REJECT_NO_ORDER","reason":"EXCHANGE_MINIMUM_OR_OWNER_MAXIMUM_NOTIONAL","orders_sent":0}
    # This is a software ceiling, not assumed financial authorization.
    # Operator-requested 3% of verified Futures equity is an OUTER CEILING.
    # Lower absolute operator limits still win; never authorizes live execution.
    loss_cap=min(p["loss_cap"],p["equity"]*Decimal("0.03"))
    if risk>loss_cap:
        return {"status":"REJECT_NO_ORDER","reason":"MODELED_LOSS_INCLUDING_FEES_EXCEEDS_BUDGET","orders_sent":0}
    return {"status":"OFFLINE_BUDGET_CHECK_PASS_NOT_ORDER_AUTHORITY",
            "modeled_loss_usdt":str(risk),"worst_notional_usdt":str(worst_notional),
            "loss_cap_usdt":str(loss_cap),"orders_sent":0,"can_trade":False}
