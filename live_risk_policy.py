"""Shared live risk limits. Configuration never grants exchange-write permission.

Two mutually exclusive order-sizing modes are supported:

  risk_percent   (default) size from a per-trade risk budget, as before.
  fixed_notional size every accepted signal to a fixed USDT notional.

The mode only changes HOW the contract count is derived. Every safety gate
(notional ceiling, initial-margin ceiling, leverage ceiling, position count,
aggregate open risk, signal age) is still enforced in live_futures_engine.
"""
import os
from decimal import Decimal, InvalidOperation

SIZING_MODE_RISK = 'risk_percent'
SIZING_MODE_FIXED = 'fixed_notional'

# Hard ceiling for the fixed-notional target. It may never exceed the
# independently enforced per-position notional cap.
MAX_FIXED_NOTIONAL_USDT = Decimal('100')


def sizing_mode():
    """Return the active sizing mode, failing closed on an unknown value."""
    raw = os.getenv('LUXALGO_LIVE_SIZING_MODE', SIZING_MODE_RISK)
    if not isinstance(raw, str):
        raise ValueError('Invalid live sizing mode')
    value = raw.strip().lower()
    if value in ('', SIZING_MODE_RISK, 'risk', 'percent', 'risk_percent'):
        return SIZING_MODE_RISK
    if value in (SIZING_MODE_FIXED, 'fixed', 'notional', 'fixed_usdt'):
        return SIZING_MODE_FIXED
    raise ValueError('Live sizing mode must be risk_percent or fixed_notional')


def fixed_notional_usdt():
    """Fixed USDT notional per trade. Only meaningful in fixed_notional mode."""
    raw = os.getenv('LUXALGO_LIVE_FIXED_NOTIONAL_USDT', '100')
    try:
        amount = Decimal(str(raw).strip())
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError('Invalid fixed notional amount') from None
    if not amount.is_finite() or amount <= 0:
        raise ValueError('Fixed notional must be positive')
    if amount > MAX_FIXED_NOTIONAL_USDT:
        raise ValueError('Fixed notional must not exceed the 100 USDT position cap')
    return amount


def per_entry_risk():
    raw = os.getenv('LUXALGO_LIVE_RISK_PERCENT', '0.5')
    try:
        percent = Decimal(raw)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError('Invalid live risk percentage') from None
    if not percent.is_finite() or not Decimal('0') < percent <= Decimal('3'):
        raise ValueError('Live risk percentage must be positive and at most 3')
    return percent / Decimal('100')


def policy_status():
    mode = sizing_mode()
    status = dict(sizing_mode=mode,
                  aggregate_risk_percent='2.5', max_leverage=5, max_positions=5,
                  equity_basis='Gate USDT-M total + unrealised_pnl',
                  aggregate_cap_preserved=True)
    if mode == SIZING_MODE_FIXED:
        status['fixed_notional_usdt'] = str(fixed_notional_usdt())
        status['per_trade_risk_percent'] = 'not_applied'
    else:
        status['per_trade_risk_percent'] = str(per_entry_risk() * 100)
    return status
