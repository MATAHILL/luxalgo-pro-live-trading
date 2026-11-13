"""Detect exchange-closed bot positions and record them as CLOSED_VERIFIED.

Why this module exists
----------------------
Before this module, a bot position that closed on the exchange (native SL or TP
hit, or a manual close at Gate.io) left its ledger row stuck in ``PROTECTED``
forever. Nothing ever wrote ``CLOSED_VERIFIED`` for a normal close:

  * ``reconcile_ledger`` then reported ``POSITION_MISSING_OR_AMBIGUOUS``
  * ``reconcile_and_halt_if_needed`` treated that as an anomaly
  * the bot latched ``EMERGENCY_HALT`` and refused every later signal
  * the COMPLETED TRADES panel had no closed row to show

So one finished trade produced three visible symptoms: a halted bot, a blocked
second signal, and an empty exit record.

This module closes that loop. It is READ-ONLY against the exchange and writes
only the bot's own ledger row. It never places, cancels or modifies an order.

Safety rules
------------
A row is only closed when ALL of the following are true:

  1. the exchange snapshot is readable and authenticated
  2. the contract has NO open position
  3. the contract has NO open order
  4. the contract has NO resting trigger (SL/TP)
  5. the row is in a state that can legitimately end (PROTECTED / ENTRY_CONFIRMED
     / PROTECTION_PENDING / HALTED)

Anything else stays open and keeps the HALT latched. Unknown is never closed.
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from live_futures_engine import PREFIX, LiveSafetyError, GateLiveClient

# States that can legitimately end in a verified close.
CLOSEABLE_STATES = ("PROTECTED", "ENTRY_CONFIRMED", "PROTECTION_PENDING", "HALTED")


def _contract_exposure(snapshot, contract):
    """Return (positions, orders, triggers) belonging to one contract."""
    positions = [p for p in (snapshot.get("positions") or [])
                 if isinstance(p, dict) and p.get("contract") == contract]
    orders = [o for o in (snapshot.get("orders") or [])
              if isinstance(o, dict) and o.get("contract") == contract]
    triggers = []
    for o in (snapshot.get("triggers") or []):
        if not isinstance(o, dict):
            continue
        initial = o.get("initial") or {}
        if initial.get("contract") == contract:
            triggers.append(o)
    return positions, orders, triggers


def _realized_pnl(client, contract, entry_order_id):
    """Exchange-authoritative realized P&L from the closing fills.

    Gate's ``/my_trades`` reports a ``pnl`` per fill. We sum only the fills that
    are NOT the entry order, so the entry's own zero-pnl row cannot dilute the
    result. Returns ``None`` when the exchange cannot answer, never a guess.
    """
    try:
        trades = client.request("GET", PREFIX + "/my_trades",
                                params={"contract": contract, "limit": 100})
    except Exception:
        return None
    if not isinstance(trades, list):
        return None
    total = Decimal("0")
    found = False
    for t in trades:
        if not isinstance(t, dict):
            continue
        if str(t.get("contract")) != contract:
            continue
        if str(t.get("order_id") or "") == str(entry_order_id or ""):
            continue
        try:
            pnl = Decimal(str(t.get("pnl")))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if not pnl.is_finite():
            continue
        total += pnl
        found = True
    return total if found else None


def _close_fill_price(client, contract, entry_order_id):
    """Best-effort average fill price of the closing order(s)."""
    try:
        orders = client.request("GET", PREFIX + "/orders",
                                params={"contract": contract, "status": "finished",
                                        "limit": 100})
    except Exception:
        return None
    if not isinstance(orders, list):
        return None
    for o in orders:
        if not isinstance(o, dict):
            continue
        if str(o.get("id_string") or o.get("id") or "") == str(entry_order_id or ""):
            continue
        is_close = o.get("is_close") is True or o.get("close") is True
        if not is_close:
            continue
        try:
            price = Decimal(str(o.get("fill_price")))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if price.is_finite() and price > 0:
            return price
    return None


def detect_and_record_closes(con, client=None):
    """Record exchange-closed bot positions as CLOSED_VERIFIED.

    Returns a summary dict. Never raises for a single bad row; a row that cannot
    be proven closed is left untouched so the HALT stays latched.
    """
    client = client or GateLiveClient()
    snapshot = client.snapshot()  # authenticated GET only

    with con.cursor() as cur:
        cur.execute("""SELECT event_id,contract,side,state,contracts,entry_price,
                              stop_loss,take_profit,entry_order_id,client_text
                       FROM luxalgo_prod_execution_ledger
                       WHERE state = ANY(%s)""", (list(CLOSEABLE_STATES),))
        rows = [dict(r) for r in cur.fetchall()]

    recorded = []
    skipped = []
    for row in rows:
        contract = row.get("contract")
        if not contract:
            skipped.append({"event_id": row.get("event_id"), "reason": "NO_CONTRACT"})
            continue
        positions, orders, triggers = _contract_exposure(snapshot, contract)
        if positions or orders or triggers:
            # Still live, or still has resting protection: not a clean close.
            skipped.append({"event_id": row.get("event_id"),
                            "reason": "STILL_OPEN_OR_PROTECTED",
                            "positions": len(positions), "orders": len(orders),
                            "triggers": len(triggers)})
            continue

        realized = _realized_pnl(client, contract, row.get("entry_order_id"))
        exit_price = _close_fill_price(client, contract, row.get("entry_order_id"))
        detail = {
            "close_detected_by": "EXCHANGE_FLAT_READBACK",
            "close_note": ("exchange reported no position, no open order and no "
                           "resting trigger for this contract"),
            "realized_pnl_usdt": (str(realized) if realized is not None else None),
            "realized_pnl_source": ("GATE_MY_TRADES" if realized is not None
                                    else "UNAVAILABLE"),
            "exit_fill_price": (str(exit_price) if exit_price is not None else None),
            "previous_state": row.get("state"),
        }
        with con.cursor() as cur:
            cur.execute("""UPDATE luxalgo_prod_execution_ledger
                           SET state='CLOSED_VERIFIED',
                               detail=COALESCE(detail,'{}'::jsonb) || %s::jsonb,
                               updated_at=NOW()
                           WHERE event_id=%s AND state=%s""",
                        (json.dumps(detail), row["event_id"], row["state"]))
            if cur.rowcount == 1:
                recorded.append({"event_id": row["event_id"],
                                 "contract": contract,
                                 "realized_pnl_usdt": detail["realized_pnl_usdt"]})
            else:
                skipped.append({"event_id": row.get("event_id"),
                                "reason": "STATE_CHANGED_DURING_UPDATE"})
    return {"recorded": recorded, "skipped": skipped,
            "recorded_count": len(recorded), "skipped_count": len(skipped),
            "orders_sent": 0}


def record_closes_safely(con, client=None):
    """Best-effort close detection for READ endpoints.

    The COMPLETED TRADES panel and the position cards are read-only views, but
    they are also the only places an operator looks. Before this wrapper, close
    detection ran ONLY from the reconciliation worker, which is started only
    when PRODUCTION_TERMINAL_VIEW=true. On a deployment where that variable was
    unset, a position that closed on the exchange was never recorded: the exit
    list stayed empty AND the stale PROTECTED row kept the EMERGENCY_HALT
    latched, blocking every later signal.

    Calling this from the read path makes the panel self-healing. It is
    READ-ONLY against the exchange and writes only the bot's own ledger row.
    Any failure is swallowed: a read view must never 500 because the exchange
    was briefly unreachable, and a failure here can only leave a gate CLOSED.
    """
    try:
        return detect_and_record_closes(con, client)
    except Exception:
        return {"recorded": [], "skipped": [], "recorded_count": 0,
                "skipped_count": 0, "orders_sent": 0, "unavailable": True}


def read_completed_trades(con, limit=150):
    """Closed bot trades with their realized P&L, newest first."""
    with con.cursor() as cur:
        cur.execute("""SELECT event_id,contract,side,timeframe,state,contracts,
                              entry_price,stop_loss,take_profit,entry_order_id,
                              stop_order_id,take_order_id,detail,created_at,updated_at
                       FROM luxalgo_prod_execution_ledger
                       WHERE state='CLOSED_VERIFIED'
                       ORDER BY updated_at DESC LIMIT %s""", (int(limit),))
        rows = [dict(r) for r in cur.fetchall()]

    out = []
    for r in rows:
        detail = r.get("detail") or {}
        if isinstance(detail, str):
            try:
                detail = json.loads(detail)
            except (ValueError, TypeError):
                detail = {}
        realized = detail.get("realized_pnl_usdt")
        try:
            realized_num = float(realized) if realized is not None else None
        except (TypeError, ValueError):
            realized_num = None
        out.append(dict(
            event_id=r.get("event_id"),
            contract=r.get("contract"),
            symbol=str(r.get("contract") or "").replace("_", ""),
            side=r.get("side"),
            timeframe=r.get("timeframe"),
            contracts=str(r.get("contracts")),
            entry_price=str(r.get("entry_price")),
            exit_price=detail.get("exit_fill_price"),
            stop_loss=str(r.get("stop_loss")),
            take_profit=str(r.get("take_profit")),
            realized_pnl_usdt=realized_num,
            realized_pnl_source=detail.get("realized_pnl_source", "UNAVAILABLE"),
            close_reason=detail.get("close_detected_by") or detail.get("release_note")
                         or detail.get("recovery_close_order_id") and "RECOVERY_CLOSE"
                         or "CLOSED",
            closed_at=(r["updated_at"].isoformat() if r.get("updated_at") else None),
            opened_at=(r["created_at"].isoformat() if r.get("created_at") else None),
        ))
    return out
