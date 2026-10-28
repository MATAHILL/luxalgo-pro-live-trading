"""Pure Testnet-only UI labels; does not authorize trades or change journal states.

LIVE requires both protected journal state and a freshly verified exchange position.
An unverified order is never shown as live or closed.
"""
from datetime import datetime, timezone
from typing import Optional

CLOSED_STATES={"CLOSED_VERIFIED","EMERGENCY_CLOSED"}
UNKNOWN_STATES={"HALTED_UNKNOWN","HALTED_UNPROTECTED","RECOVERING",
                "EMERGENCY_CLOSE_REQUIRED","ENTRY_ACK","SUBMISSION_UNKNOWN"}

def classify_signal(*, journal_state=None, attempt_state=None, attempt_detail=None,
                    queue_state=None, age_seconds=0, exchange_open=None, exchange_checked=False):
    if journal_state=="PROTECTED_VERIFIED":
        if not exchange_checked: return "VERIFY EXCHANGE"
        return "LIVE" if exchange_open is True else "VERIFY EXCHANGE"
    if journal_state in CLOSED_STATES:
        return "CLOSED"
    if journal_state in UNKNOWN_STATES:
        return "VERIFY EXCHANGE"
    if journal_state:
        return "NOT EXECUTED"
    if (attempt_state=="TESTNET_PREFLIGHT_BLOCKED"
        and attempt_detail=="CONTRACT_NOT_LISTED_ON_TESTNET"):
        return "UNSUPPORTED ON TESTNET"
    if attempt_state in UNKNOWN_STATES or attempt_state in ("EXECUTION_EXCEPTION",):
        return "VERIFY EXCHANGE"
    if attempt_state in ("TESTNET_PREFLIGHT_BLOCKED","TESTNET_MARKET_READBACK_UNAVAILABLE",
                         "TESTNET_DISABLED","SIGNAL_EXPIRED","SIGNAL_NOT_ELIGIBLE",
                         "ACCOUNT_UNCERTAIN_EXPOSURE_SAFETY_PAUSE","BLOCKED",
                         "DUPLICATE_BLOCKED","INVALID_LEVELS"):
        return "NOT EXECUTED"
    if attempt_state in CLOSED_STATES or attempt_state=="PROTECTED_VERIFIED":
        return "VERIFY EXCHANGE"  # Never trust an unbacked worker label as a fill.
    if (attempt_state in ("WORKER_QUEUED",None) and queue_state in ("PENDING","CREATED")
            and 0<=age_seconds<=60):
        return "WAITING FOR EXECUTION"
    return "NOT EXECUTED"


def parse_reset_time(value):
    if not value: return None
    try:
        dt=datetime.fromisoformat(value.replace("Z","+00:00"))
        if dt.tzinfo is None: raise ValueError("Reset time must specify timezone")
        return dt.astimezone(timezone.utc)
    except (ValueError,TypeError,AttributeError):
        raise ValueError("Invalid incoming display reset time") from None
