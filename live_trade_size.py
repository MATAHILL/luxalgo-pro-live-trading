"""Operator-editable trade size (USDT notional) for new live Futures entries.

Before this module the terminal could only take its per-trade size from the
deployment environment (LUXALGO_LIVE_SIZING_MODE / LUXALGO_LIVE_FIXED_NOTIONAL_USDT),
which needs a Render restart and cannot be changed from the dashboard. This
module adds a single durable row the operator can edit from the terminal; the
live engine reads it when it builds each new plan.

It never places an order, never touches an exchange endpoint and never relaxes
a safety cap. It only selects a target notional.

UNLIMITED SIZE
--------------
There is NO upper limit on the value the operator may save. The per-position
notional ceiling and the per-position initial-margin ceiling have been switched
off (see live_futures_engine.MAX_POSITION_NOTIONAL_USDT / _INITIAL_MARGIN_USDT,
both None), so this module does not refuse a large number either.

A saved size is still only a TARGET, and the engine still bounds the order it
actually sends. What limits the executed size once the ceilings are gone:

  * the account's available margin -- the exchange will not fill what you
    cannot fund; the engine floors the contract count to what the balance
    supports and refuses the trade if that is below one lot,
  * the exchange lot limits for the specific contract (order_size_min /
    order_size_max, and market_order_size_max at preflight),
  * the leverage ceiling (MAX_LEVERAGE, default 5x),
  * the five-position limit and the duplicate-symbol rule,
  * the 2.5% aggregate open-risk budget across all open positions.

The aggregate open-risk budget is the bound that most often bites on a large
size: the modeled stop-distance loss of the new order is added to the risk
already reserved by open positions, and the sum may not exceed 2.5% of equity.
A size the account cannot fund is refused at plan time with
"Insufficient margin/risk for exchange minimum lot" -- nothing is sent.

Gate.io trades whole contracts, so the engine floors the contract count and the
executed notional is at or below the saved value.
"""
from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation

from live_risk_policy import MAX_FIXED_NOTIONAL_USDT, SIZING_MODE_FIXED

# live_futures_engine and live_execution_coordinator both reach back into this
# module's callers, so they are imported inside the functions that need them
# rather than at module scope. That keeps the import graph acyclic.
def _notional_cap():
    """The per-position notional ceiling, or None when it is switched off."""
    from live_futures_engine import MAX_POSITION_NOTIONAL_USDT
    return MAX_POSITION_NOTIONAL_USDT


def _lock_id():
    from live_execution_coordinator import LOCK_ID
    return LOCK_ID


# Smallest size the operator may save. Below roughly one contract of the
# cheapest supported symbol the engine floors to zero and refuses the trade,
# so a smaller number would only ever report a misleading "accepted".
MIN_TRADE_SIZE_USDT = Decimal("1")

SCHEMA = """
CREATE TABLE IF NOT EXISTS luxalgo_prod_trade_size_control (
 id SMALLINT PRIMARY KEY CHECK(id=1),
 enabled BOOLEAN NOT NULL DEFAULT FALSE,
 notional_usdt NUMERIC,
 changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 changed_by TEXT NOT NULL DEFAULT ''
);
INSERT INTO luxalgo_prod_trade_size_control(id,enabled,notional_usdt,changed_by)
VALUES (1,FALSE,NULL,'') ON CONFLICT(id) DO NOTHING;
"""


class TradeSizeBlocked(ValueError):
    """The requested size is not an acceptable live trade size."""


def cap_usdt():
    """The effective upper bound, or None when there is no upper bound.

    Both ceilings are switchable. When every ceiling is switched off this
    returns None, which the terminal renders as "no upper limit" and the
    validator treats as "accept any positive value".
    """
    ceilings = [c for c in (_notional_cap(), MAX_FIXED_NOTIONAL_USDT) if c is not None]
    if not ceilings:
        return None
    return min(Decimal(str(c)) for c in ceilings)


def _decimal(value):
    if isinstance(value, bool) or value is None:
        raise TradeSizeBlocked("TRADE_SIZE_MISSING")
    if isinstance(value, Decimal):
        amount = value
    else:
        text = str(value).strip()
        if not text:
            raise TradeSizeBlocked("TRADE_SIZE_MISSING")
        try:
            amount = Decimal(text)
        except (InvalidOperation, TypeError, ValueError):
            raise TradeSizeBlocked("TRADE_SIZE_NOT_A_NUMBER") from None
    if not amount.is_finite():
        raise TradeSizeBlocked("TRADE_SIZE_NOT_A_NUMBER")
    if amount <= 0:
        raise TradeSizeBlocked("TRADE_SIZE_MUST_BE_POSITIVE")
    return amount


def validate(value):
    """Return the size as a 2-decimal Decimal, or raise TradeSizeBlocked.

    There is no upper limit: a ceiling of None means the operator removed it,
    so no maximum is applied. Every refusal names the exact rule that blocked
    it so the terminal can print a reason the operator can act on.
    """
    amount = _decimal(value)
    cap = cap_usdt()
    if cap is not None and amount > cap:
        raise TradeSizeBlocked(
            "TRADE_SIZE_EXCEEDS_CAP: %s USDT is above the %s USDT per-position "
            "notional ceiling (MAX_POSITION_NOTIONAL_USDT)." % (amount, cap))
    if amount < MIN_TRADE_SIZE_USDT:
        raise TradeSizeBlocked(
            "TRADE_SIZE_TOO_SMALL: the smallest size the terminal accepts is "
            "%s USDT." % MIN_TRADE_SIZE_USDT)
    try:
        quantized = amount.quantize(Decimal("0.01"))
    except InvalidOperation:
        raise TradeSizeBlocked("TRADE_SIZE_NOT_A_NUMBER") from None
    if quantized != amount:
        raise TradeSizeBlocked(
            "TRADE_SIZE_TOO_PRECISE: use at most two decimal places.")
    return quantized


def _connect():
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is missing")
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(url, connect_timeout=5, row_factory=dict_row)


def install(con):
    con.execute(SCHEMA)


def _row(con):
    with con.cursor() as cur:
        cur.execute("""SELECT enabled,notional_usdt,changed_at,changed_by
                       FROM luxalgo_prod_trade_size_control WHERE id=1""")
        return cur.fetchone()


def state(con, *, install_schema=True):
    """Read the saved size without changing anything."""
    if install_schema:
        install(con)
    row = _row(con)
    if row is None:
        raise RuntimeError("Trade size control row unavailable")
    saved = row["notional_usdt"]
    active = bool(row["enabled"]) and saved is not None
    cap = cap_usdt()
    return dict(
        active=active,
        notional_usdt=str(Decimal(str(saved)).quantize(Decimal("0.01"))) if active else None,
        saved_value=str(saved) if saved is not None else None,
        enabled=bool(row["enabled"]),
        cap_usdt=str(cap) if cap is not None else None,
        cap_unlimited=cap is None,
        min_usdt=str(MIN_TRADE_SIZE_USDT),
        maximum_initial_margin_usdt="unlimited",
        maximum_leverage="5",
        aggregate_risk_percent="2.5",
        source="terminal_override" if active else "environment_policy",
        effective_mode=SIZING_MODE_FIXED if active else None,
        note=("Saved size applies to the next new trade. Whole contracts are "
              "floored, so the executed notional can land below this value. "
              "Available margin, exchange lot limits, leverage and the 2.5% "
              "aggregate risk budget still bound the order."),
        unchanged_safety_caps=True,
        orders_submitted=0,
        changed_at=row["changed_at"].isoformat() if row["changed_at"] else None,
        changed_by=row["changed_by"] or None,
    )


def set_size(con, value, *, actor=""):
    """Persist an operator-chosen size and make it active for new trades."""
    amount = validate(value)
    install(con)
    with con.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_id(),))
        cur.execute("""UPDATE luxalgo_prod_trade_size_control
                       SET enabled=TRUE,notional_usdt=%s,changed_at=NOW(),
                           changed_by=%s
                       WHERE id=1""", (amount, str(actor)[:80]))
        if cur.rowcount != 1:
            raise RuntimeError("Trade size persistence failed")
    _remember(amount)
    return state(con, install_schema=False)


def clear_size(con, *, actor=""):
    """Drop the override; new trades fall back to the environment policy."""
    install(con)
    with con.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_id(),))
        cur.execute("""UPDATE luxalgo_prod_trade_size_control
                       SET enabled=FALSE,changed_at=NOW(),changed_by=%s
                       WHERE id=1""", (str(actor)[:80],))
        if cur.rowcount != 1:
            raise RuntimeError("Trade size persistence failed")
    _remember(None)
    return state(con, install_schema=False)


# --- process-local cache -------------------------------------------------
# The engine builds plans on the request and worker threads, and a rebuild of
# the whole dashboard must not add a database round trip to every plan. The
# cache is authoritative only for this process; the database row is the record.
_cached = None
_loaded = False


def _remember(value):
    global _cached, _loaded
    _cached = value
    _loaded = True


def refresh():
    """Re-read the saved size from the database into the process cache.

    On a read failure the LAST KNOWN GOOD value is kept. Resetting the cache
    to None would mean "no override", which silently reverts the engine to the
    environment default (100 USDT) -- a size the operator never chose, with no
    warning anywhere in the terminal. Keeping the previous value is the
    fail-safe choice: it is the size the operator actually saved, and every
    other safety gate still runs unchanged.

    Before the first successful read there is no known value, so None is
    returned and the environment policy applies as before.
    """
    try:
        with _connect() as con:
            row = _row_after_install(con)
    except Exception:
        return _cached
    _remember(row)
    return row


def _row_after_install(con):
    try:
        install(con)
    except Exception:
        pass
    row = _row(con)
    if row is None or not row["enabled"] or row["notional_usdt"] is None:
        return None
    return Decimal(str(row["notional_usdt"]))


def active_notional_cached():
    """The saved override, or None. Never raises, never blocks a plan."""
    global _loaded
    if not _loaded:
        refresh()
        _loaded = True
    return _cached


def reset_cache():
    """Test hook: forget the cached value so the next read hits the database."""
    global _cached, _loaded
    _cached = None
    _loaded = False
