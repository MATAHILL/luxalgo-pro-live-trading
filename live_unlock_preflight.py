"""Non-monetary Gate.io Live Futures unlock prerequisite report.

This module does not clear a HALT, issue a certificate, enable a worker,
or submit an exchange order. A flat account does NOT prove an accepted trade.
"""
from __future__ import annotations


def assess_unlock_readiness(operator_state, exchange_snapshot, *,
                            sender_released, worker_released, unresolved_ledger_rows):
    """Fail closed on missing evidence; never grant order-dispatch permission."""
    state = operator_state if isinstance(operator_state, dict) else {}
    gates = state.get("safety_gates")
    snapshot = exchange_snapshot if isinstance(exchange_snapshot, dict) else {}
    try:
        verified_source = snapshot["source"] == "GATE_IO_LIVE_FUTURES"
        positions = snapshot["positions"]
        orders = snapshot["orders"]
        triggers = snapshot["triggers"]
        mode = snapshot["account"]["position_mode"]
        exchange_readback = bool(
            verified_source
            and all(isinstance(items, list) for items in (positions, orders, triggers))
            and mode in ("single", "dual")
        )
        flat = exchange_readback and not positions and not orders and not triggers
    except (KeyError, TypeError, AttributeError):
        exchange_readback = False
        flat = False
        mode = "UNKNOWN"
    ledger_clear = type(unresolved_ledger_rows) is int and unresolved_ledger_rows == 0
    halted = state.get("halt_active") is not False
    certified = state.get("certified") is True
    operator_on = state.get("operator_requested_on") is True
    env_enabled = (isinstance(gates, list) and
                   "ENVIRONMENT_LOCKED" not in gates)
    checks = {
        "authenticated_exchange_readback": bool(exchange_readback),
        "flat_exchange_for_limited_acceptance": bool(flat),
        "no_unresolved_execution_rows": ledger_clear,
        "emergency_halt_cleared": not halted,
        "acceptance_and_final_certification": certified,
        "environment_permission_enabled": env_enabled,
        "operator_enabled": operator_on,
        "exchange_sender_source_released": sender_released is True,
        "automatic_worker_source_released": worker_released is True,
        # No historical ledger or flat account may substitute for genuine
        # independent fill + native protection + emergency recovery evidence.
        "independent_live_fill_native_protection_and_recovery": False,
    }
    return {
        "status": "BLOCKED_UNTIL_INDEPENDENT_ACCEPTANCE",
        "phase": "PRE_RELEASE_READ_ONLY_AUDIT",
        "exchange": "GATE_IO_LIVE_USDT_FUTURES",
        "position_mode": mode if exchange_readback else "UNKNOWN",
        "checks": checks,
        "passed": sum(checks.values()),
        "total": len(checks),
        "blockers": [name for name, passed in checks.items() if not passed],
        "source_sender_locked": sender_released is not True,
        "automatic_worker_locked": worker_released is not True,
        "halt_active": halted,
        "ready_for_independent_acceptance_review": bool(flat and ledger_clear),
        "automatic_real_money_trading_certified": False,
        "orders_enabled_by_this_check": False,
        "orders_submitted": 0,
        "note": "Read-only prerequisite report; never release source locks or override HALT.",
    }
