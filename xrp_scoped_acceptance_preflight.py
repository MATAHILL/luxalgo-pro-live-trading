"""Strictly non-executing XRP_USDT acceptance-test preflight.

Owner's historical single-clock-time approvals are NOT reusable. The operator
must independently capture a new, exact, bounded approval interval in an
audited authorization record before attempting a future monetary test.

No import of the Gate client, no database connection, no exchange POST,
no live source-lock release. This module can NEVER grant order authority.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from live_signal_identity import validated_bar_id


@dataclass(frozen=True)
class BoundedOwnerScope:
    contract: str
    side: str
    start_utc: datetime
    end_utc: datetime
    approval_reference: str
    max_contracts: Decimal = Decimal("1")
    max_margin: Decimal = Decimal("1.70")
    max_notional: Decimal = Decimal("8.30")
    max_modeled_loss: Decimal = Decimal("0.10")
    max_leverage: Decimal = Decimal("5")
    max_entry_slippage: Decimal = Decimal("0.001")


class Reject(Exception):
    pass


def num(value, name, *, zero=False):
    if value is None or isinstance(value, bool):
        raise Reject("MISSING_" + name)
    try:
        v = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise Reject("INVALID_" + name) from exc
    if not v.is_finite() or (v < 0 if zero else v <= 0):
        raise Reject("INVALID_" + name)
    return v


def instant(value, name):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise Reject("INVALID_" + name)
    return value.astimezone(timezone.utc)


def assess_xrp_single_order(data, owner_scope, now):
    """Only returns DRY_PREFLIGHT_* / REJECT_NO_ORDER, never an order permission."""
    safe = dict(status="REJECT_NO_ORDER", orders_sent=0, can_execute=False,
                exchange_writes_enabled=False, continuous_trading_authorized=False)
    try:
        if not isinstance(owner_scope, BoundedOwnerScope):
            raise Reject("EXPLICIT_BOUNDED_APPROVAL_REQUIRED")
        if not isinstance(data, dict):
            raise Reject("INDEPENDENT_READBACK_REQUIRED")
        start = instant(owner_scope.start_utc, "START")
        end = instant(owner_scope.end_utc, "END")
        current = instant(now, "NOW")
        if (not isinstance(owner_scope.approval_reference, str)
                or not owner_scope.approval_reference.strip()
                or owner_scope.approval_reference.startswith("TEST_ONLY")):
            raise Reject("AUDITED_OWNER_APPROVAL_REFERENCE_REQUIRED")
        if not start < end or end - start > timedelta(minutes=15):
            raise Reject("INVALID_OWNER_AUTHORIZATION_WINDOW")
        if not start <= current < end:
            raise Reject("OUTSIDE_OWNER_AUTHORIZED_WINDOW")
        if (owner_scope.contract != "XRP_USDT" or owner_scope.side != "LONG"
                or data.get("contract") != "XRP_USDT"
                or data.get("side") != "LONG"
                or data.get("exchange") != "GATE_IO_LIVE_USDT_FUTURES"):
            raise Reject("OWNER_AUTHORIZED_XRP_LONG_ONLY")
        # Fail closed if owner-scope caps do not precisely match granted limits.
        if (num(owner_scope.max_contracts, "OWNER_CONTRACTS") != 1
                or num(owner_scope.max_margin, "OWNER_MARGIN") != Decimal("1.70")
                or num(owner_scope.max_notional, "OWNER_NOTIONAL") != Decimal("8.30")
                or num(owner_scope.max_modeled_loss, "OWNER_RISK") != Decimal("0.10")
                or num(owner_scope.max_leverage, "OWNER_LEVERAGE") != 5
                or num(owner_scope.max_entry_slippage, "OWNER_SLIPPAGE") != Decimal("0.001")):
            raise Reject("OWNER_LIMITS_CANNOT_BE_EXPANDED")
        if num(data.get("contracts"), "CONTRACTS") != 1:
            raise Reject("ONE_WHOLE_CONTRACT_ONLY")
        minimum = num(data.get("exchange_min_contracts"), "MINIMUM_CONTRACTS")
        maximum = num(data.get("exchange_max_contracts"), "MAXIMUM_CONTRACTS")
        if minimum > 1 or maximum < 1 or Decimal("1") % minimum != 0:
            raise Reject("EXCHANGE_LOT_INCOMPATIBLE_WITH_ONE_WHOLE_CONTRACT")
        for field in ("account_authenticated", "xrp_metadata_authenticated",
                      "native_sl_tp_capability_verified",
                      "emergency_close_recovery_certified",
                      "emergency_halt_reconciled_and_cleared",
                      "operator_authorized_for_one_order",
                      "no_unknown_exchange_ack"):
            if data.get(field) is not True:
                raise Reject("MISSING_MANDATORY_SAFETY_EVIDENCE_" + field.upper())
        readback = instant(data.get("exchange_snapshot_utc"), "EXCHANGE_READBACK")
        if readback > current or current - readback > timedelta(seconds=30):
            raise Reject("STALE_EXCHANGE_READBACK")
        for field in ("open_positions", "open_orders", "open_triggers", "reserved_orders"):
            if num(data.get(field), field.upper(), zero=True) != 0:
                raise Reject("UNRESOLVED_EXCHANGE_EXPOSURE")
        if data.get("xrp_delisting") is not False:
            raise Reject("XRP_CONTRACT_STATUS_UNKNOWN")
        if (data.get("signal_side") != "LONG" or data.get("signal_contract") != "XRP_USDT"
                or data.get("signal_levels") != "VALID"
                or data.get("order_type") != "MARKET"
                or data.get("timeframe") not in ("5m", "15m")
                or not validated_bar_id(data.get("tv_bar_id"))):
            raise Reject("ORIGINAL_QUALIFIED_LUXALGO_SIGNAL_REQUIRED")
        received = instant(data.get("signal_received_utc"), "SIGNAL_TIME")
        if received > current or current - received > timedelta(seconds=60):
            raise Reject("STALE_LUXALGO_SIGNAL")
        lev = num(data.get("actual_leverage"), "LEVERAGE")
        slippage = num(data.get("max_entry_slippage_ratio"), "SLIPPAGE", zero=True)
        fee = num(data.get("taker_fee_rate"), "TAKER_FEE", zero=True)
        if lev > 5 or slippage > Decimal("0.001") or fee > Decimal("0.01"):
            raise Reject("LEVERAGE_SLIPPAGE_OR_FEE_EXCEEDS_LIMIT")
        entry = num(data.get("entry"), "ENTRY")
        stop = num(data.get("sl"), "SL")
        target = num(data.get("tp"), "TP")
        mark = num(data.get("mark_price"), "MARK")
        multiplier = num(data.get("quanto_multiplier"), "MULTIPLIER")
        if not stop < entry < target or not stop < mark < target:
            raise Reject("LONG_PROTECTIVE_GEOMETRY_INVALID")
        equity = num(data.get("equity_usdt"), "EQUITY")
        available = num(data.get("available_usdt"), "AVAILABLE")
        margin = num(data.get("required_margin_usdt"), "MARGIN")
        notional = max(entry, mark) * multiplier * (1 + slippage)
        modeled_loss = (entry - stop) * multiplier + (
            max(entry, mark) * multiplier * (2 * fee + 2 * Decimal("0.001")))
        if (margin > Decimal("1.70") or margin > available
                or notional > Decimal("8.30")
                or modeled_loss > min(Decimal("0.10"), equity * Decimal("0.005"))):
            raise Reject("OWNER_FINANCIAL_LIMIT_OR_RISK_CAP_EXCEEDED")
        return dict(status="DRY_PREFLIGHT_PASS_NOT_ORDER_AUTHORITY",
                    modeled_loss_usdt=str(modeled_loss),
                    notional_usdt=str(notional), required_margin_usdt=str(margin),
                    orders_sent=0, can_execute=False,
                    exchange_writes_enabled=False, continuous_trading_authorized=False)
    except (Reject, OverflowError, ArithmeticError) as exc:
        return dict(safe, reason=str(exc))
