"""Verified trading analytics for the LuxAlgo Pro live futures terminal.

Why this module exists
----------------------
Every summary figure on the dashboard (Futures Liquidity boxes, Performance
Analytics, Completed Trades exit records) used to be either a hardcoded dash or
a number computed in the browser from whatever happened to be on screen. That
produced three visible defects:

  * Total Realized P&L, Win Rate and the other summary boxes showed "—" even
    when closed trades existed.
  * The same trade could be counted twice when a row was re-read after a
    partial close, inflating trade counts and P&L.
  * Exit records had no exit price and no net P&L, because the close path that
    wrote them never stored the exchange's own fill data.

This module is the single place those numbers are computed. It is READ-ONLY
against the exchange and never writes a ledger row.

Rules
-----
1. Every figure comes from a verified record: the bot's own execution ledger
   plus Gate.io's own fills (``/my_trades``). Nothing is estimated.
2. A trade is counted ONCE, keyed by ``event_id``. Fills are de-duplicated by
   Gate's trade id, so a re-read cannot double count.
3. Fees are subtracted. ``net_realized_pnl`` is what the account actually kept.
4. When the exchange cannot answer, the figure is ``None`` — never zero, never
   a guess. The UI renders ``None`` as "—" and says why.
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

# Ledger states that represent a trade which actually reached the exchange.
EXECUTED_STATES = ("ENTRY_CONFIRMED", "PROTECTION_PENDING", "PROTECTED",
                   "HALTED", "CLOSED_VERIFIED")
# Ledger states that are still in flight (no confirmed fill yet).
PENDING_STATES = ("RESERVED", "SENDING", "ACK_UNKNOWN")


def _dec(value):
    """Finite Decimal or None. Never raises, never invents a zero."""
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _detail(row):
    """The row's JSONB detail as a dict, tolerating a text column."""
    detail = row.get("detail") or {}
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except (ValueError, TypeError):
            detail = {}
    return detail if isinstance(detail, dict) else {}


def aggregate_fills(trades, entry_order_id=None):
    """Aggregate one contract's closing fills into a single verified record.

    Gate reports one row per fill. A trade closed in several pieces (partial
    closes) therefore produces several rows, and a re-read of the same window
    can repeat them. Fills are de-duplicated by Gate's own trade id so a
    partial close is summed exactly once.

    ``fee`` on Gate is signed and normally negative (a deduction), so
    ``net = pnl + fees`` is the amount the account actually kept.

    Returns ``None`` when there is nothing verifiable to report.
    """
    if not isinstance(trades, list):
        return None
    seen = set()
    pnl = Decimal("0")
    fees = Decimal("0")
    qty = Decimal("0")
    notional = Decimal("0")
    last_ts = None
    count = 0
    for t in trades:
        if not isinstance(t, dict):
            continue
        trade_id = str(t.get("id") or t.get("trade_id") or "")
        if trade_id:
            if trade_id in seen:
                continue
            seen.add(trade_id)
        # The entry fill carries no realized P&L; including it would dilute
        # the result with a zero row.
        if entry_order_id and str(t.get("order_id") or "") == str(entry_order_id):
            continue
        row_pnl = _dec(t.get("pnl"))
        if row_pnl is None:
            continue
        pnl += row_pnl
        fee = _dec(t.get("fee"))
        if fee is not None:
            fees += fee
        size = _dec(t.get("size"))
        if size is not None:
            qty += abs(size)
        price = _dec(t.get("price"))
        if price is not None and size is not None:
            notional += abs(size) * price
        stamp = t.get("create_time") or t.get("time")
        try:
            stamp_int = int(float(stamp))
            if last_ts is None or stamp_int > last_ts:
                last_ts = stamp_int
        except (TypeError, ValueError):
            pass
        count += 1
    if not count:
        return None
    return {
        "pnl": pnl,
        "fees": fees,
        "net": pnl + fees,
        "qty": qty,
        "notional": notional,
        "avg_price": (notional / qty) if qty > 0 else None,
        "last_ts": last_ts,
        "fill_count": count,
    }


def fetch_contract_fills(client, contract):
    """Read one contract's fills from Gate. Returns a list or None."""
    try:
        from live_futures_engine import PREFIX
        trades = client.request("GET", PREFIX + "/my_trades",
                                params={"contract": contract, "limit": 100})
    except Exception:
        return None
    return trades if isinstance(trades, list) else None


def _row_net_pnl(row, client=None):
    """Net realized P&L for one closed ledger row, exchange-first.

    Order of preference:
      1. Gate's own fills (pnl + fees) — authoritative, includes partial closes
      2. the value the close path already stored in ``detail``
      3. None (unknown) — never zero
    """
    detail = _detail(row)
    contract = row.get("contract")
    if client is not None and contract:
        fills = fetch_contract_fills(client, contract)
        agg = aggregate_fills(fills, row.get("entry_order_id"))
        if agg is not None:
            return agg["net"], "GATE_MY_TRADES", agg
    stored = _dec(detail.get("net_realized_pnl_usdt"))
    if stored is None:
        stored = _dec(detail.get("realized_pnl_usdt"))
    if stored is not None:
        return stored, detail.get("realized_pnl_source") or "LEDGER_DETAIL", None
    return None, "UNAVAILABLE", None


def compute_analytics(con, client=None, limit=500):
    """Every Performance Analytics figure, from verified records only.

    A trade is counted once, keyed by ``event_id``. Only rows that actually
    reached the exchange are counted as trades; a reservation that never filled
    is not a trade and must not appear in win rate or P&L.
    """
    with con.cursor() as cur:
        cur.execute("""SELECT event_id,contract,side,state,contracts,entry_price,
                              stop_loss,take_profit,entry_order_id,detail,
                              created_at,updated_at
                       FROM luxalgo_prod_execution_ledger
                       ORDER BY updated_at DESC LIMIT %s""", (int(limit),))
        rows = [dict(r) for r in cur.fetchall()]

    seen = set()
    closed = []
    executed_open = 0
    pending = 0
    for row in rows:
        event_id = row.get("event_id")
        if not event_id or event_id in seen:
            continue
        seen.add(event_id)
        state = row.get("state")
        if state == "CLOSED_VERIFIED":
            closed.append(row)
        elif state in EXECUTED_STATES:
            executed_open += 1
        elif state in PENDING_STATES:
            pending += 1

    wins = losses = flat = 0
    total_net = Decimal("0")
    pnl_known = 0
    pnl_unknown = 0
    for row in closed:
        net, _source, _agg = _row_net_pnl(row, client)
        if net is None:
            pnl_unknown += 1
            continue
        pnl_known += 1
        total_net += net
        if net > 0:
            wins += 1
        elif net < 0:
            losses += 1
        else:
            flat += 1

    decided = wins + losses
    total_closed = len(closed)
    return {
        "total_closed_trades": total_closed,
        "winning_trades": wins,
        "losing_trades": losses,
        "flat_trades": flat,
        "win_rate_pct": (round(wins / decided * 100, 2) if decided else None),
        "losing_rate_pct": (round(losses / decided * 100, 2) if decided else None),
        # Share of executed trades that reached a verified close.
        "total_close_rate_pct": (round(total_closed / (total_closed + executed_open) * 100, 2)
                                 if (total_closed + executed_open) else None),
        "total_realized_pnl_usdt": (float(total_net) if pnl_known else None),
        "realized_pnl_trades_counted": pnl_known,
        "realized_pnl_trades_unknown": pnl_unknown,
        "open_executed_trades": executed_open,
        "pending_trades": pending,
        "source": "GATE_MY_TRADES_AND_LEDGER",
        "deduplicated_by": "event_id",
    }


def liquidity_summary(con, client=None, snapshot=None):
    """The six Futures Liquidity boxes, from verified records only.

    ``snapshot`` is an optional already-fetched Gate snapshot so a caller can
    avoid a second round trip. When absent, the account is read directly.
    """
    account = {}
    positions = []
    if snapshot is not None:
        account = snapshot.get("account") or {}
        positions = snapshot.get("positions") or []
    elif client is not None:
        try:
            account = client.request("GET", "/api/v4/futures/usdt/accounts") or {}
        except Exception:
            account = {}
        try:
            raw = client.request("GET", "/api/v4/futures/usdt/positions") or []
            positions = [p for p in raw
                         if isinstance(p, dict) and _dec(p.get("size")) not in (None, Decimal("0"))]
        except Exception:
            positions = []

    equity = _dec(account.get("total"))
    available = _dec(account.get("available"))
    balance = _dec(account.get("total")) if equity is None else equity
    unrealized = _dec(account.get("unrealised_pnl"))
    if unrealized is None and positions:
        parts = [_dec(p.get("unrealised_pnl")) for p in positions]
        unrealized = (sum((p for p in parts if p is not None), Decimal("0"))
                      if any(p is not None for p in parts) else None)

    analytics = compute_analytics(con, client)
    return {
        "equity_usdt": float(equity) if equity is not None else None,
        "available_usdt": float(available) if available is not None else None,
        "balance_usdt": float(balance) if balance is not None else None,
        "total_realized_pnl_usdt": analytics["total_realized_pnl_usdt"],
        "win_rate_pct": analytics["win_rate_pct"],
        "unrealized_pnl_usdt": float(unrealized) if unrealized is not None else None,
        "open_positions": len(positions),
        "total_closed_trades": analytics["total_closed_trades"],
        "winning_trades": analytics["winning_trades"],
        "losing_trades": analytics["losing_trades"],
        "source": "GATE_ACCOUNT_AND_VERIFIED_LEDGER",
    }


def signal_execution_state(ledger_row, refusal_reason=None, refusal_stage=None):
    """Truthful execution state for one incoming signal.

    Before this, the signals panel hardcoded ``state='NOT EXECUTED'`` for every
    row, so a signal that had genuinely filled was still labelled "Not
    Executed". The ledger is the authority: if a row exists and reached the
    exchange, the signal executed.

    Returns ``(state, label, detail)``.
    """
    if refusal_reason:
        return ("REJECTED", "REJECTED",
                "Refused at " + str(refusal_stage or "DISPATCH") + ": " + str(refusal_reason))
    if not ledger_row:
        return ("PENDING", "PENDING",
                "Signal received; no exchange order has been sent yet.")
    state = ledger_row.get("state")
    order_id = ledger_row.get("entry_order_id")
    if state in ("RESERVED", "SENDING"):
        return ("PENDING", "PENDING",
                "Risk slot reserved; entry order not yet acknowledged by Gate.io.")
    if state == "ACK_UNKNOWN":
        return ("PENDING", "PENDING",
                "Order acknowledgement unknown; the exchange must be checked before any retry.")
    if state == "ENTRY_CONFIRMED":
        return ("EXECUTED", "EXECUTED",
                "Entry filled at Gate.io" + (" (order " + str(order_id) + ")" if order_id else "")
                + "; protective SL/TP not yet confirmed.")
    if state == "PROTECTION_PENDING":
        return ("PARTIALLY_FILLED", "PROTECTION PENDING",
                "Entry filled; native SL/TP still being placed.")
    if state == "PROTECTED":
        return ("EXECUTED", "EXECUTED",
                "Entry filled and protected at Gate.io"
                + (" (order " + str(order_id) + ")" if order_id else "") + ".")
    if state == "HALTED":
        return ("FAILED", "HALTED",
                "Execution halted after the entry; operator review required.")
    if state == "CLOSED_VERIFIED":
        return ("EXECUTED", "CLOSED",
                "Executed and closed; see the Completed Trades exit record.")
    return ("PENDING", "PENDING", "Signal recorded; execution state not yet verified.")
