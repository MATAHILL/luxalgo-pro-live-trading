"""Pure, non-executing ONE-ORDER owner-authorized BTC acceptance scope check.

Never imports an exchange client, never mutates DB, never sends an order.
A PASS is only a dry preflight, not release of source locks/HALT nor evidence
of an exchange fill, native SL/TP protection or unrestricted certification.
"""
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from live_signal_identity import validated_bar_id

START=datetime(2026,10,29,4,30,tzinfo=timezone.utc)  # 10:00 IST
END=datetime(2026,10,29,4,45,tzinfo=timezone.utc)    # 10:15 IST, exclusive
CAP_MARGIN=Decimal("1.70")
CAP_NOTIONAL=Decimal("8.30")
CAP_MODELED_LOSS=Decimal("0.10")
CAP_LEVERAGE=Decimal("5")
CAP_SLIPPAGE=Decimal("0.001")
ONE_CONTRACT=Decimal("1")

class AcceptanceNotEligible(ValueError):
    pass

def _positive(data,key):
    value=data.get(key)
    if isinstance(value,bool):
        raise AcceptanceNotEligible(key+" is boolean, not a number")
    try:
        x=Decimal(str(value))
    except (InvalidOperation,ValueError,TypeError):
        raise AcceptanceNotEligible(key+" missing or not numerical")
    if not x.is_finite() or x<=0:
        raise AcceptanceNotEligible(key+" must be finite and positive")
    return x

def _count(data,key):
    x=_positive_or_zero(data,key)
    if x!=x.to_integral_value():
        raise AcceptanceNotEligible(key+" must be an integer")
    return x

def _positive_or_zero(data,key):
    value=data.get(key)
    if isinstance(value,bool):
        raise AcceptanceNotEligible(key+" is not numerical")
    try:
        x=Decimal(str(value))
    except (InvalidOperation,ValueError,TypeError):
        raise AcceptanceNotEligible(key+" missing or invalid")
    if not x.is_finite() or x<0:
        raise AcceptanceNotEligible(key+" must be finite and non-negative")
    return x

def _time(data,key):
    v=data.get(key)
    if not isinstance(v,datetime) or v.tzinfo is None:
        raise AcceptanceNotEligible(key+" must be timezone-aware datetime")
    return v.astimezone(timezone.utc)

def assess_one_order_scope(data, now):
    """Return a dry-run assessment; never an order/execution authorization.

    All exchange-derived numbers and inventories must be independently
    authenticated immediately before use; accepts no missing metadata.
    """
    try:
        if not isinstance(data,dict):
            raise AcceptanceNotEligible("Incomplete preflight data")
        if not isinstance(now,datetime) or now.tzinfo is None:
            raise AcceptanceNotEligible("Current time cannot be verified")
        now=now.astimezone(timezone.utc)
        if not START<=now<END:
            raise AcceptanceNotEligible("OUTSIDE_OWNER_APPROVED_TEST_WINDOW")
        if data.get("exchange")!="GATE_IO_LIVE_USDT_FUTURES":
            raise AcceptanceNotEligible("Exchange is not Gate.io LIVE USDT-M")
        if data.get("contract")!="BTC_USDT" or data.get("side")!="LONG":
            raise AcceptanceNotEligible("ONE_CONTRACT_BTC_LONG_ONLY")
        if _count(data,"contracts")!=ONE_CONTRACT or _count(data,"exchange_min_contracts")!=ONE_CONTRACT:
            raise AcceptanceNotEligible("ONE_CONTRACT_MINIMUM_ONLY")
        if data.get("position_mode")!="dual":
            raise AcceptanceNotEligible("DUAL_MODE_MUST_BE_EXCHANGE_VERIFIED")
        if (data.get("account_readback_verified") is not True or
            data.get("metadata_readback_verified") is not True or
            data.get("protective_trigger_capability_verified") is not True):
            raise AcceptanceNotEligible("INDEPENDENT_EXCHANGE_READBACK_MISSING")
        if _time(data,"account_snapshot_utc")>now or now-_time(data,"account_snapshot_utc")>timedelta(seconds=30):
            raise AcceptanceNotEligible("STALE_OR_FUTURE_EXCHANGE_READBACK")
        if data.get("contract_delisting") is not False:
            raise AcceptanceNotEligible("CONTRACT_DELISTING_UNKNOWN")
        if any(_count(data,key)!=0 for key in
               ("existing_positions","existing_orders","existing_triggers")):
            raise AcceptanceNotEligible("EXISTING_EXCHANGE_EXPOSURE_OR_ORDERS")
        if data.get("unknown_exchange_ack") is not False:
            raise AcceptanceNotEligible("PENDING_UNKNOWN_EXCHANGE_ACK")
        if (data.get("signal_level_status")!="VALID" or data.get("order_type")!="MARKET"
            or data.get("signal_side")!="LONG" or data.get("signal_contract")!="BTC_USDT"):
            raise AcceptanceNotEligible("QUALIFYING_BTC_LUXALGO_LONG_REQUIRED")
        if data.get("timeframe") not in ("5m","15m") or not validated_bar_id(data.get("tv_bar_id")):
            raise AcceptanceNotEligible("STABLE_COMPLETED_LUXALGO_BAR_REQUIRED")
        received=_time(data,"signal_received_utc")
        if received>now or now-received>timedelta(seconds=120):
            raise AcceptanceNotEligible("LUXALGO_SIGNAL_NOT_CURRENT")
        leverage=_positive(data,"leverage")
        if leverage>CAP_LEVERAGE:
            raise AcceptanceNotEligible("LEVERAGE_ABOVE_5X")
        slip=_positive_or_zero(data,"max_entry_slippage_ratio")
        if slip>CAP_SLIPPAGE:
            raise AcceptanceNotEligible("SLIPPAGE_EXCEEDS_0.10_PERCENT")
        margin=_positive(data,"exchange_required_margin_usdt")
        if margin>CAP_MARGIN:
            raise AcceptanceNotEligible("MARGIN_EXCEEDS_1.70_USDT")
        equity=_positive(data,"equity_usdt")
        available=_positive(data,"available_usdt")
        if margin>available:
            raise AcceptanceNotEligible("INSUFFICIENT_AVAILABLE_MARGIN")
        mult=_positive(data,"quanto_multiplier")
        mark=_positive(data,"mark_price")
        entry=_positive(data,"entry")
        stop=_positive(data,"stop_loss")
        target=_positive(data,"take_profit")
        fee=_positive_or_zero(data,"taker_fee_rate")
        if not stop<entry<target:
            raise AcceptanceNotEligible("LUXALGO_LONG_SL_ENTRY_TP_GEOMETRY_INVALID")
        notional=max(entry,mark)*mult
        if notional>CAP_NOTIONAL:
            raise AcceptanceNotEligible("NOTIONAL_EXCEEDS_8.30_USDT")
        # Model a 0.1% slip EACH way and two taker fees.
        # Stop-market gaps, liquidation, slippage and margin rules can
        # cause actual loss to exceed this *estimate*.
        estimated_loss=(entry-stop)*mult+notional*(2*fee+2*CAP_SLIPPAGE)
        if estimated_loss>CAP_MODELED_LOSS or estimated_loss>equity*Decimal("0.005"):
            raise AcceptanceNotEligible("MODELED_LOSS_EXCEEDS_OWNER_OR_0.5_PERCENT_CAP")
        return dict(status="DRY_PREFLIGHT_PASS_NOT_ORDER_AUTHORITY",
                    notional_usdt=str(notional),modeled_loss_usdt=str(estimated_loss),
                    required_margin_usdt=str(margin),exchange_orders_sent=0,
                    unrestricted_bot_release=False)
    except AcceptanceNotEligible as exc:
        return dict(status="REJECT_NO_ORDER",reason=str(exc),exchange_orders_sent=0,
                    unrestricted_bot_release=False)
