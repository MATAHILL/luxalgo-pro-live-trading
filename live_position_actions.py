"""Operator position actions: market close, SL/TP modify, reverse.

Why this module exists
----------------------
The dashboard rendered four buttons (Set TP/SL, Reverse, Market Close, Close
Position) that were hard-disabled in the HTML and, in the one place they were
wired up, only ever worked for GATE_FUTURES_TESTNET. A real live position could
therefore never be closed or re-protected from the terminal. That is the bug
this module fixes.

Design rules
------------
* Every action is an explicit operator click. Nothing here is reachable from a
  webhook, a signal, or a background worker.
* Every action re-reads the exchange immediately before acting. A stale plan is
  never used to send an order.
* Every action is verified by readback. "Sent" is not "done".
* Closing is risk-reducing, so it is allowed while the bot is halted. Opening
  new exposure (reverse) is not.
* The existing source lock and environment permissions still apply: the
  transport refuses a write unless LUXALGO_EXCHANGE_WRITE_RELEASE and the three
  permission variables are set. This module cannot bypass that.
"""
from __future__ import annotations

import json
import time
from decimal import Decimal, InvalidOperation

from live_futures_engine import (
    PREFIX, LiveSafetyError, AckUnknown, GateLiveClient, ORDER_RE,
    build_protective_trigger, evaluate_protection, dec, price_geometry,
    account_position_mode, active_positions, CONTRACT_RE,
)


def _ledger_row(con, event_id):
    with con.cursor() as cur:
        cur.execute("""SELECT event_id,contract,side,timeframe,position_mode,state,
                              contracts,entry_price,stop_loss,take_profit,
                              entry_order_id,stop_order_id,take_order_id,client_text
                       FROM luxalgo_prod_execution_ledger WHERE event_id=%s""",
                    (event_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def _live_position(client, contract):
    """The single open exchange position for this contract, or None."""
    snap = client.snapshot()
    positions = [p for p in snap["positions"] if p.get("contract") == contract]
    if len(positions) > 1:
        raise LiveSafetyError("Multiple exchange positions for this contract")
    return (positions[0] if positions else None), snap


def _triggers_for(snap, contract):
    out = []
    for o in snap.get("triggers") or []:
        if not isinstance(o, dict):
            continue
        if (o.get("initial") or {}).get("contract") == contract:
            out.append(o)
    return out


def _cancel_trigger(client, trigger_id):
    """Cancel one resting price trigger. DELETE is allowlisted for this path."""
    if not ORDER_RE.fullmatch(str(trigger_id or "")):
        raise LiveSafetyError("Invalid trigger id for cancellation")
    return client.request("DELETE", PREFIX + "/price_orders/" + str(trigger_id))


def _require_operator(con):
    from production_bot_control import require_operator_on
    require_operator_on(con)


def resolve_event_for_contract(con, contract):
    """Return a ledger event_id that identifies this contract's live position.

    Why this exists
    ---------------
    A position can be open at Gate.io with no matching bot ledger row: opened
    manually, opened before the ledger existed, or whose row was already marked
    closed. The dashboard used to disable every button in that case, which left
    a real position that could not be closed from the terminal.

    This resolves the identity the close path needs WITHOUT inventing exposure:
    it prefers the newest non-closed row for the contract, then the newest row
    at all, and only as a last resort creates a minimal record that is clearly
    marked as operator-originated. The close itself is still reduce-only and
    still verified against the exchange, so a wrong identity cannot open a
    trade - it can only close the position that is actually there.
    """
    if not isinstance(contract, str) or not contract:
        raise LiveSafetyError("Contract required to resolve an execution record")
    with con.cursor() as cur:
        cur.execute("""SELECT event_id FROM luxalgo_prod_execution_ledger
                       WHERE contract=%s AND state<>'CLOSED_VERIFIED'
                       ORDER BY created_at DESC LIMIT 1""", (contract,))
        row = cur.fetchone()
        if row:
            return row["event_id"]
        cur.execute("""SELECT event_id FROM luxalgo_prod_execution_ledger
                       WHERE contract=%s ORDER BY created_at DESC LIMIT 1""",
                    (contract,))
        row = cur.fetchone()
        if row:
            return row["event_id"]
        # No record at all. Do NOT invent one: the ledger enforces
        # CHECK(risk_usdt>0) and CHECK(contracts>0), so a placeholder row with
        # zeros raised CheckViolation, the route caught it and returned 409, and
        # the operator's Market Close click did nothing at all. A position with
        # no ledger row is closed by CONTRACT instead (close_position_by_contract),
        # which needs no fabricated identity.
        return None


def _halted(con):
    from live_execution_coordinator import control
    return control(con)["halted"] is not False


# ---------------------------------------------------------------------------
# 1. MARKET CLOSE
# ---------------------------------------------------------------------------
def _close_contract(con, contract, client, *, event_id=None, ledger_state=None):
    """Reduce-only market close of ONE contract. Verified by readback.

    Shared by the ledger-identified path and the contract-only path so both
    behave identically: same reduce-only order, same readback proof, same
    exchange-derived close record. Closing reduces risk, so it is allowed while
    the bot is halted.
    """
    position, snap = _live_position(client, contract)
    if ledger_state == "CLOSED_VERIFIED" and position is None:
        return {"state": "ALREADY_CLOSED", "orders_sent": 0,
                "detail": "This trade is already recorded as closed and the "
                          "exchange reports no open position."}
    if position is None:
        # Exchange is flat: record the close rather than sending anything.
        from live_close_record import detect_and_record_closes
        detect_and_record_closes(con, client)
        return {"state": "CLOSED_VERIFIED", "orders_sent": 0,
                "detail": "Exchange already reports this position closed; "
                          "the ledger row was updated. No order was sent."}

    mode = account_position_mode(snap["account"])
    if mode not in ("single", "dual"):
        raise LiveSafetyError("Exchange position mode unverified; refusing to close")

    size = Decimal(str(position.get("size")))
    if not size.is_finite() or size == 0:
        raise LiveSafetyError("Exchange position size unreadable")
    qty = abs(size)
    if qty != qty.to_integral_value():
        raise LiveSafetyError("Fractional position; close manually at Gate.io")

    side = "LONG" if size > 0 else "SHORT"
    tag = str(event_id or contract)[:20]
    order = {"contract": contract, "size": int(qty) * (-1 if side == "LONG" else 1),
             "price": "0", "tif": "ioc", "reduce_only": True,
             "text": "t-mc-" + tag}
    if mode == "single":
        order["close"] = True

    ack = client.request("POST", PREFIX + "/orders", payload=order)
    if not isinstance(ack, dict):
        raise AckUnknown("Close acknowledgement invalid")

    # Verify: the position must be gone and the order must be finished.
    deadline = time.time() + 12
    after = None
    while time.time() < deadline:
        try:
            after, _snap2 = _live_position(client, contract)
        except LiveSafetyError:
            after = "UNKNOWN"
        if after is None:
            break
        time.sleep(1.5)
    if after is not None:
        return {"state": "CLOSE_NOT_VERIFIED", "orders_sent": 1,
                "detail": "Close order was sent but the exchange still reports an "
                          "open position. Inspect Gate.io directly; do not repeat "
                          "blindly.",
                "exchange_position_still_open": True}

    # Record the close with exchange-derived P&L.
    from live_close_record import detect_and_record_closes
    detect_and_record_closes(con, client)
    return {"state": "CLOSED_VERIFIED", "orders_sent": 1,
            "detail": "Position closed and verified flat at the exchange."}


def close_position(con, event_id, client=None):
    """Reduce-only market close of the exact live position. Verified by readback.

    Allowed while halted: closing reduces risk and must never be blocked by the
    same gate that stops new entries.
    """
    client = client or GateLiveClient()
    row = _ledger_row(con, event_id)
    if not row:
        raise LiveSafetyError("Unknown bot execution record")
    # A CLOSED_VERIFIED ledger row is bookkeeping, NOT proof the exchange is
    # flat. When the two disagree the EXCHANGE wins: the operator is looking at
    # a live position and must be able to close it. Returning "already closed"
    # here without asking the exchange is what left a real position with four
    # dead buttons.
    return _close_contract(con, row["contract"], client,
                           event_id=event_id, ledger_state=row["state"])


def close_position_by_contract(con, contract, client=None):
    """Reduce-only market close of a position that has no usable ledger row.

    Why this exists
    ---------------
    A position can be open at Gate.io with no matching bot ledger row: opened
    manually, opened before the ledger existed, or whose row was already marked
    closed. The dashboard used to disable every button in that case, and the
    fallback that tried to invent a ledger row raised CheckViolation on the
    ledger's CHECK(risk_usdt>0) / CHECK(contracts>0) constraints, so the click
    returned 409 and did nothing at all.

    This closes the position that is ACTUALLY at the exchange, identified by
    contract alone. It is reduce-only and verified by readback, so a wrong
    identity cannot open a trade - it can only close what is really there.
    """
    if not isinstance(contract, str) or not CONTRACT_RE.fullmatch(contract):
        raise LiveSafetyError("A valid contract is required to close by contract")
    client = client or GateLiveClient()
    return _close_contract(con, contract, client)


# ---------------------------------------------------------------------------
# 2. SL/TP MODIFY
# ---------------------------------------------------------------------------
def modify_protection(con, event_id, stop_loss, take_profit, client=None):
    """Replace the resting SL/TP with new prices. Cancel-then-place, verified.

    The new prices must satisfy the same geometry the entry path enforces, so a
    typo cannot place a stop on the wrong side of the market.
    """
    client = client or GateLiveClient()
    row = _ledger_row(con, event_id)
    if not row:
        raise LiveSafetyError("Unknown bot execution record")
    # Ask the exchange first. A stale CLOSED_VERIFIED row must not block
    # re-protecting a position that is genuinely still open.
    position, snap = _live_position(client, row["contract"])
    if position is None:
        raise LiveSafetyError("No open exchange position for this trade")

    mode = account_position_mode(snap["account"])
    if mode not in ("single", "dual"):
        raise LiveSafetyError("Exchange position mode unverified")

    size = Decimal(str(position.get("size")))
    side = "LONG" if size > 0 else "SHORT"
    entry = Decimal(str(position.get("entry_price") or row["entry_price"]))
    sl = dec(stop_loss, "stop loss")
    tp = dec(take_profit, "take profit")
    # Same geometry rule as the entry path: SL below entry below TP for a long.
    price_geometry(side, entry, sl, tp)

    plan = dict(contract=row["contract"], side=side, entry=str(entry),
                sl=str(sl), tp=str(tp), position_mode=mode)

    # Cancel existing triggers first so the position is never double-protected
    # in a way that could close twice.
    cancelled = []
    for t in _triggers_for(snap, row["contract"]):
        tid = str(t.get("id_string") or t.get("id") or "")
        if not tid:
            continue
        _cancel_trigger(client, tid)
        cancelled.append(tid)

    stop = client.submit_trigger(plan, "SL")
    take = client.submit_trigger(plan, "TP")
    if not isinstance(stop, dict) or not isinstance(take, dict):
        raise AckUnknown("Protection acknowledgement incomplete")

    snap2 = client.snapshot()
    checked = evaluate_protection(plan, snap2["triggers"])
    if not checked.get("verified"):
        return {"state": "PROTECTION_NOT_VERIFIED", "orders_sent": 2,
                "cancelled": cancelled,
                "detail": "New SL/TP were sent but could not be verified on the "
                          "exchange (" + str(checked.get("reason")) + "). "
                          "Inspect Gate.io now."}

    with con.cursor() as cur:
        cur.execute("""UPDATE luxalgo_prod_execution_ledger
                       SET stop_loss=%s, take_profit=%s,
                           stop_order_id=%s, take_order_id=%s,
                           detail=COALESCE(detail,'{}'::jsonb) || %s::jsonb,
                           updated_at=NOW()
                       WHERE event_id=%s""",
                    (str(sl), str(tp), checked["stop_order_id"],
                     checked["take_order_id"],
                     json.dumps({"protection_modified_by": "OPERATOR_TERMINAL",
                                 "previous_stop_order_id": row.get("stop_order_id"),
                                 "previous_take_order_id": row.get("take_order_id")}),
                     event_id))
    return {"state": "PROTECTION_VERIFIED", "orders_sent": 2,
            "cancelled": cancelled,
            "stop_order_id": checked["stop_order_id"],
            "take_order_id": checked["take_order_id"],
            "stop_loss": str(sl), "take_profit": str(tp),
            "detail": "New SL/TP placed and verified on the exchange."}


# ---------------------------------------------------------------------------
# 3. REVERSE
# ---------------------------------------------------------------------------
def reverse_position(con, event_id, stop_loss, take_profit, client=None):
    """Close the position, then open the opposite one with the given SL/TP.

    This creates NEW exposure, so it is refused while the bot is halted and it
    must pass the same notional/margin/risk caps as a signal entry. The close is
    verified before any opposite order is sent: a failed close never leaves a
    naked reverse.
    """
    client = client or GateLiveClient()
    if _halted(con):
        raise LiveSafetyError(
            "Reverse opens new exposure and is blocked while the bot is halted. "
            "Clear the halt first, or use Market Close to reduce risk.")
    _require_operator(con)

    row = _ledger_row(con, event_id)
    if not row:
        raise LiveSafetyError("Unknown bot execution record")

    position, snap = _live_position(client, row["contract"])
    if position is None:
        raise LiveSafetyError("No open exchange position to reverse")

    size = Decimal(str(position.get("size")))
    qty = abs(size)
    if qty != qty.to_integral_value():
        raise LiveSafetyError("Fractional position; reverse manually at Gate.io")
    old_side = "LONG" if size > 0 else "SHORT"
    new_side = "SHORT" if old_side == "LONG" else "LONG"

    # 1. Close first, and prove it.
    closed = close_position(con, event_id, client)
    if closed.get("state") != "CLOSED_VERIFIED":
        return {"state": "REVERSE_ABORTED_CLOSE_FAILED", "orders_sent": 1,
                "detail": "The close did not verify, so no opposite order was "
                          "sent. Inspect Gate.io before retrying.",
                "close_result": closed}

    # 2. Size the opposite entry against the same caps.
    from live_futures_engine import (MAX_POSITION_NOTIONAL_USDT,
                                     MAX_POSITION_INITIAL_MARGIN_USDT,
                                     MAX_LEVERAGE, verified_account_equity)
    from live_risk_policy import per_entry_risk
    snap2 = client.snapshot()
    meta = client.request("GET", PREFIX + "/contracts/" + row["contract"])
    if not isinstance(meta, dict):
        raise LiveSafetyError("Contract metadata invalid")
    equity, available = verified_account_equity(snap2["account"])
    multiplier = dec(meta.get("quanto_multiplier"), "contract multiplier")
    fee = dec(meta.get("taker_fee_rate"), "taker fee")
    slip = Decimal("0.001")
    entry = Decimal(str(position.get("entry_price") or row["entry_price"]))
    sl = dec(stop_loss, "stop loss")
    tp = dec(take_profit, "take profit")
    price_geometry(new_side, entry, sl, tp)

    unit_loss = abs(entry - sl) * multiplier + (entry * multiplier) * (2 * fee + 2 * slip)
    unit_margin = (entry * multiplier) * (1 + slip) * (Decimal(1) / MAX_LEVERAGE + fee)
    unit_notional = entry * multiplier * (1 + slip)
    budget = min(equity * per_entry_risk(), equity * Decimal("0.025"))
    count = min(int(budget / unit_loss) if unit_loss > 0 else 0,
                int(available / unit_margin) if unit_margin > 0 else 0,
                int(MAX_POSITION_NOTIONAL_USDT / unit_notional) if unit_notional > 0 else 0,
                int(MAX_POSITION_INITIAL_MARGIN_USDT / unit_margin) if unit_margin > 0 else 0,
                int(qty))
    if count < 1:
        return {"state": "REVERSE_ABORTED_SIZE", "orders_sent": 1,
                "detail": "Position was closed, but the opposite entry does not "
                          "fit the risk/notional caps. No opposite order was sent.",
                "close_result": closed}

    mode = account_position_mode(snap2["account"])
    new_event = "rev-" + str(event_id)[:16]
    plan = dict(contract=row["contract"], side=new_side, entry=str(entry),
                sl=str(sl), tp=str(tp), position_mode=mode,
                contracts=count, client_text="t-rv-" + str(event_id)[:20],
                max_entry_slippage_ratio=str(slip))

    entry_order = {"contract": row["contract"],
                   "size": count * (1 if new_side == "LONG" else -1),
                   "price": "0", "tif": "ioc", "reduce_only": False,
                   "text": plan["client_text"],
                   "market_order_slip_ratio": str(slip)}
    ack = client.request("POST", PREFIX + "/orders", payload=entry_order)
    if not isinstance(ack, dict):
        raise AckUnknown("Reverse entry acknowledgement invalid")

    stop = client.submit_trigger(plan, "SL")
    take = client.submit_trigger(plan, "TP")
    snap3 = client.snapshot()
    checked = evaluate_protection(plan, snap3["triggers"])
    if not checked.get("verified"):
        return {"state": "REVERSE_ENTRY_UNPROTECTED", "orders_sent": 3,
                "detail": "The opposite entry was sent but its SL/TP could not be "
                          "verified. Inspect Gate.io immediately.",
                "close_result": closed}

    with con.cursor() as cur:
        cur.execute("""INSERT INTO luxalgo_prod_execution_ledger(
                        event_id,client_text,contract,side,timeframe,position_mode,
                        state,risk_usdt,entry_price,stop_loss,take_profit,contracts,
                        stop_order_id,take_order_id,detail)
                       VALUES (%s,%s,%s,%s,%s,%s,'PROTECTED',%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (event_id) DO NOTHING""",
                    (new_event, plan["client_text"], row["contract"], new_side,
                     row["timeframe"], mode, str(count * unit_loss), str(entry),
                     str(sl), str(tp), str(count), checked["stop_order_id"],
                     checked["take_order_id"],
                     json.dumps({"reversed_from": event_id,
                                 "origin": "OPERATOR_TERMINAL_REVERSE"})))
    return {"state": "REVERSED_AND_PROTECTED", "orders_sent": 3,
            "new_event_id": new_event, "new_side": new_side,
            "contracts": count,
            "stop_order_id": checked["stop_order_id"],
            "take_order_id": checked["take_order_id"],
            "detail": "Position closed, opposite opened and protected."}
