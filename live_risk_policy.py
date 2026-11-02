"""Shared live risk limits. Configuration never grants exchange-write permission."""
import os
from decimal import Decimal, InvalidOperation


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
    return dict(per_trade_risk_percent=str(per_entry_risk() * 100),
                aggregate_risk_percent='2.5', max_leverage=5, max_positions=5,
                equity_basis='Gate USDT-M total + unrealised_pnl',
                aggregate_cap_preserved=True)
