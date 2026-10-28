"""Read-only, fail-closed production futures position-size proposal.

No exchange communication, no order placement, and no permission to trade.
Risk is determined by stop distance and REAL exchange contract multiplier;
never confuse "one contract" with "one whole coin".
"""
from decimal import Decimal, InvalidOperation, ROUND_FLOOR

MAX_LEVERAGE=Decimal("5")
MAX_TRADE_RISK_FRACTION=Decimal("0.005")
MAX_ACCOUNT_RISK_FRACTION=Decimal("0.025")
MAX_POSITIONS=5

class SizingBlocked(ValueError):
    pass

def _positive(value, label):
    if isinstance(value,bool) or value is None:
        raise SizingBlocked(label+"_UNAVAILABLE")
    try:
        x=Decimal(str(value))
    except (InvalidOperation,TypeError,ValueError):
        raise SizingBlocked(label+"_UNAVAILABLE") from None
    if not x.is_finite() or x<=0:
        raise SizingBlocked(label+"_INVALID")
    return x

def propose_live_contract_size(*,side,entry,stop,equity_usdt,available_usdt,
                               multiplier,order_size_min,order_size_max,
                               open_positions,already_reserved_stop_risk_usdt=0,
                               risk_fraction="0.005"):
    """Returns a read-only integer-contract risk proposal, never an authorization.

    No trade is proposed without fresh verified inputs supplied by the caller.
    Uses both stop-distance risk and initial margin at a conservative 5x
    ceiling, with 20% of AVAILABLE margin reserved for any one entry.
    """
    if side not in ("LONG","SHORT"):
        raise SizingBlocked("INVALID_SIDE")
    if type(open_positions) is not int or not 0<=open_positions<MAX_POSITIONS:
        raise SizingBlocked("POSITION_CAPACITY_UNAVAILABLE")
    e=_positive(entry,"ENTRY")
    s=_positive(stop,"STOP")
    if not (s<e if side=="LONG" else e<s):
        raise SizingBlocked("STOP_GEOMETRY_INVALID")
    equity=_positive(equity_usdt,"EQUITY")
    available=_positive(available_usdt,"AVAILABLE_MARGIN")
    mult=_positive(multiplier,"CONTRACT_MULTIPLIER")
    minimum=_positive(order_size_min,"CONTRACT_MIN")
    maximum=_positive(order_size_max,"CONTRACT_MAX")
    if minimum!=minimum.to_integral_value() or maximum!=maximum.to_integral_value():
        raise SizingBlocked("CONTRACT_SIZE_NOT_INTEGER")
    risk=_positive(risk_fraction,"RISK_FRACTION")
    if risk>MAX_TRADE_RISK_FRACTION:
        raise SizingBlocked("RISK_EXCEEDS_LIVE_PREP_MAX")
    if isinstance(already_reserved_stop_risk_usdt,bool):
        raise SizingBlocked("RESERVED_RISK_INVALID")
    try:
        reserved=Decimal(str(already_reserved_stop_risk_usdt))
    except (InvalidOperation,TypeError,ValueError):
        raise SizingBlocked("RESERVED_RISK_INVALID") from None
    if not reserved.is_finite() or reserved<0:
        raise SizingBlocked("RESERVED_RISK_INVALID")
    # For true live execution, reserved must include other live positions,
    # pending orders and durable in-flight reservations atomically.
    if reserved>=equity*MAX_ACCOUNT_RISK_FRACTION:
        raise SizingBlocked("AGGREGATE_RISK_BUDGET_EXHAUSTED")
    stop_risk_contract=abs(e-s)*mult
    notional_contract=e*mult
    if stop_risk_contract<=0 or notional_contract<=0:
        raise SizingBlocked("CONTRACT_EXPOSURE_INVALID")
    trade_budget=min(equity*risk,equity*MAX_ACCOUNT_RISK_FRACTION-reserved)
    # Limit each position to 20% of currently available margin at <=5x.
    max_position_margin=available*Decimal("0.20")
    size_from_risk=(trade_budget/stop_risk_contract).to_integral_value(rounding=ROUND_FLOOR)
    size_from_margin=(max_position_margin*MAX_LEVERAGE/notional_contract).to_integral_value(rounding=ROUND_FLOOR)
    qty=int(min(size_from_risk,size_from_margin,maximum))
    if qty<int(minimum):
        raise SizingBlocked("MINIMUM_CONTRACT_EXCEEDS_MARGIN_OR_RISK_BUDGET")
    notional=notional_contract*qty
    initial_margin=notional/MAX_LEVERAGE
    stop_risk=stop_risk_contract*qty
    return dict(status="READ_ONLY_PROPOSAL",orders_submitted=0,
                size_contracts=qty if side=="LONG" else -qty,
                underlying_quantity=str(mult*qty),notional_usdt=str(notional),
                estimated_initial_margin_usdt=str(initial_margin),
                planned_stop_loss_risk_usdt=str(stop_risk),
                trade_risk_budget_usdt=str(trade_budget),
                max_leverage=str(MAX_LEVERAGE),
                warning="Estimates exclude slippage, fees, funding, and liquidation; live sender disabled")
