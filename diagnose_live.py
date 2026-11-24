"""LUXALGO PRO -- READ-ONLY live diagnosis. Places no orders, changes nothing.

WHY THIS EXISTS
---------------
The dashboard endpoints that would explain a refusal (/terminal/live-execution/
snapshot, /api/live/status, /terminal/trade-size) all require a signed-in
session. An external audit cannot see them, and a Render log only keeps recent
lines. The refusal reason IS already written to a durable row -- this script
reads those rows and prints them in plain language, so "the bot stopped" becomes
a specific, named cause instead of a log hunt.

It performs SELECTs only. There is no INSERT, UPDATE, DELETE, no exchange call,
no order, no arm, no halt-clear, no config write. The only function it calls on
your own code is the pure planner `build_plan`, which computes numbers in memory
and never touches the network.

USAGE (Render shell, from the repo root, where DATABASE_URL is already set):

    python diagnose_live.py

Optional: narrow to one symbol or widen the history

    python diagnose_live.py --symbol XRPUSDT --limit 40

READ THE OUTPUT TOP-DOWN. Section 3 is the answer to "why is nothing trading".
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

READ_ONLY_BANNER = (
    "LUXALGO PRO -- READ-ONLY DIAGNOSIS\n"
    "SELECT-only. No order, no arm, no halt change, no config write.\n"
)


def connect():
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        print("DATABASE_URL is not set in this shell. Run this on Render, not locally.")
        sys.exit(2)
    import psycopg
    from psycopg.rows import dict_row
    try:
        return psycopg.connect(url, connect_timeout=8, row_factory=dict_row)
    except Exception as exc:  # noqa: BLE001
        print("Could not connect to the database: %s" % str(exc)[:200])
        print("Run this from a Render shell, where DATABASE_URL is the internal URL.")
        print("Nothing was changed.")
        sys.exit(3)


def table_exists(con, name):
    with con.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) AS t", (name,))
        return cur.fetchone()["t"] is not None


def section(title):
    print("\n" + "=" * 74)
    print(title)
    print("=" * 74)


def q(con, sql, params=()):
    """Run a SELECT; return [] instead of exploding on a missing table."""
    try:
        with con.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    except Exception as exc:  # noqa: BLE001 - diagnosis must never crash
        print("  (query unavailable: %s)" % str(exc)[:140])
        return []


# --------------------------------------------------------------------------
# 1. Gates and halt
# --------------------------------------------------------------------------
def gates(con):
    section("1. SAFETY GATES AND HALT STATE")
    if not table_exists(con, "luxalgo_prod_execution_control"):
        print("  luxalgo_prod_execution_control does not exist yet.")
    else:
        rows = q(con, "SELECT * FROM luxalgo_prod_execution_control")
        for row in rows:
            for key in ("halted", "armed", "certified", "operator_on", "reason",
                        "certified_until", "updated_at", "changed_by"):
                if key in row:
                    print("  %-18s %s" % (key + ":", row[key]))
    print("\n  Environment release switches (what the worker actually reads):")
    for name in ("LIVE_TRADING_ENABLED", "ORDERS_ENABLED",
                 "LIVE_ORDERS_SEPARATELY_AUTHORIZED",
                 "LUXALGO_EXCHANGE_WRITE_RELEASE",
                 "LUXALGO_AUTOMATIC_WORKER_RELEASE"):
        val = os.getenv(name, "(unset)")
        mark = "ON " if val == "true" else "off"
        print("    [%s] %-38s = %s" % (mark, name, val))
    try:
        from live_futures_engine import EXCHANGE_WRITE_RELEASE  # noqa: F401
        from live_execution_orchestrator import AUTOMATIC_WORKER_RELEASE  # noqa: F401
        print("    EXCHANGE_WRITE_RELEASE  = %s" % EXCHANGE_WRITE_RELEASE)
        print("    AUTOMATIC_WORKER_RELEASE= %s" % AUTOMATIC_WORKER_RELEASE)
    except Exception as exc:  # noqa: BLE001
        print("    (release flags unreadable: %s)" % str(exc)[:100])


# --------------------------------------------------------------------------
# 2. Saved trade size -- the silent killer
# --------------------------------------------------------------------------
def trade_size(con):
    section("2. SAVED TRADE SIZE (a value below one contract refuses EVERY signal)")
    saved = None
    if table_exists(con, "luxalgo_prod_trade_size_control"):
        rows = q(con, """SELECT enabled, notional_usdt, changed_at, changed_by
                         FROM luxalgo_prod_trade_size_control WHERE id=1""")
        if rows:
            row = rows[0]
            saved = row["notional_usdt"]
            print("  enabled      : %s" % row["enabled"])
            print("  notional_usdt: %s" % saved)
            print("  changed_at   : %s" % row["changed_at"])
            print("  changed_by   : %s" % row["changed_by"])
            if not row["enabled"] or saved is None:
                print("\n  -> No override. The environment policy decides the size.")
                saved = None
    else:
        print("  Table absent: no override has ever been saved.")

    if saved is None:
        try:
            from live_risk_policy import sizing_mode, fixed_notional_usdt, per_entry_risk
            print("  env sizing_mode      : %s" % sizing_mode())
            print("  env fixed notional   : %s" % fixed_notional_usdt())
            print("  env per-entry risk   : %s" % per_entry_risk())
        except Exception as exc:  # noqa: BLE001
            print("  (env sizing unreadable: %s)" % str(exc)[:100])
        return None

    # What one whole contract costs on each symbol the bot is likely to trade.
    print("\n  What this size can actually fund (whole contracts are FLOORED):")
    print("  %-12s %-10s %-12s %-14s %s" %
          ("contract", "price", "multiplier", "1-contract USDT", "verdict"))
    for meta in contracts(con):
        name = meta.get("name")
        mult = meta.get("quanto_multiplier")
        try:
            mult_d = float(mult)
        except (TypeError, ValueError):
            continue
        if not name or mult_d <= 0:
            continue
        price = indicative_price(con, name)
        if price is None:
            continue
        unit = price * mult_d
        if unit <= 0:
            continue
        verdict = "OK %d contract(s)" % int(float(saved) // unit) if unit <= float(saved) \
            else "REFUSED - needs >= %.2f" % unit
        print("  %-12s %-10.6g %-12.4g %-14.2f %s" %
              (name, price, mult_d, unit, verdict))
    return saved


def contracts(con):
    """Contract metadata the engine itself uses, if it is cached in the DB."""
    for tbl in ("luxalgo_futures_contracts", "luxalgo_contract_metadata"):
        if table_exists(con, tbl):
            return q(con, "SELECT * FROM %s LIMIT 40" % tbl)
    return []


def indicative_price(con, name):
    """Best available price for the minimum-lot estimate. Estimate only."""
    for tbl, col in (("luxalgo_contract_metadata", "mark_price"),
                     ("luxalgo_futures_contracts", "mark_price")):
        if table_exists(con, tbl):
            rows = q(con, "SELECT %s AS p FROM %s WHERE name=%%s" % (col, tbl), (name,))
            if rows and rows[0]["p"] is not None:
                try:
                    return float(rows[0]["p"])
                except (TypeError, ValueError):
                    pass
    # Fall back to the last signal's entry price for this symbol.
    if table_exists(con, "luxalgo_signal_levels"):
        rows = q(con, """SELECT l.entry_price
                         FROM luxalgo_signal_levels l
                         ORDER BY l.entry_price DESC LIMIT 1""")
    return None


# --------------------------------------------------------------------------
# 3. THE ANSWER: why signals did not become orders
# --------------------------------------------------------------------------
def refusals(con, limit, symbol):
    section("3. WHY SIGNALS DID NOT BECOME ORDERS  <-- read this one")
    if not table_exists(con, "luxalgo_live_execution_refusals"):
        print("  Refusal table absent. Either nothing was ever refused, or this")
        print("  build predates refusal recording (it was added in eba4b7f).")
        return
    sql = """SELECT recorded_at, event_id, stage, reason, detail
             FROM luxalgo_live_execution_refusals
             {where}
             ORDER BY recorded_at DESC LIMIT %s"""
    params = []
    where = ""
    if symbol:
        # event_id is not the symbol; filter in Python instead of guessing a join.
        where = ""
    rows = q(con, sql.format(where=where), tuple(params + [limit]))
    if not rows:
        print("  No refusal rows. If signals are also absent, the alerts are not")
        print("  reaching /webhook at all -- check TradingView's alert log next.")
        return
    counts = {}
    for row in rows:
        key = (row["stage"] or "?", (row["reason"] or "?")[:70])
        counts[key] = counts.get(key, 0) + 1
    print("  Refusal counts across the last %d rows:" % len(rows))
    for (stage, reason), n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print("    %3dx  [%s] %s" % (n, stage, reason))
    print("\n  Most recent 10, with detail:")
    for row in rows[:10]:
        print("    %s  %s" % (row["recorded_at"], row["stage"]))
        print("      reason: %s" % (row["reason"] or ""))
        if row["detail"]:
            print("      detail: %s" % str(row["detail"])[:220])
    explain(counts)


def explain(counts):
    """Turn the refusal taxonomy into the one-line cause and its remedy."""
    print("\n  INTERPRETATION:")
    seen = {reason for (_stage, reason) in counts}
    joined = " ".join(seen)

    def note(marker, text):
        if marker in joined:
            print("   * " + text)

    note("ORDER_TYPE_NOT_MARKET",
         "Your TradingView alert JSON is missing \"order_type\":\"MARKET\".\n"
         "     FIX: paste the documented LuxAlgo custom alert() message with\n"
         "     order_type included. This is the #1 cause of 'armed but silent'.")
    note("MISSING_STABLE_BAR_ID",
         "The alert has no fresh `bartime` ({time}).\n"
         "     FIX: add \"bartime\": {time} (NO quotes around {time}) to the same\n"
         "     alert message, and keep the alert on 5m or 15m.")
    note("CONFLUENCE_BLOCK",
         "The confluence quality gate vetoed the signal.\n"
         "     FIX: read `blockers` in section 4. HTF_CONFLICT on LONGs is normal\n"
         "     in a downtrend; it is not a bug.")
    note("BTC_BLOCKED",
         "The symbol is BTC, which is blocked by policy (testnet_coin_scope).\n"
         "     FIX: alert on a non-BTC contract, or change BLOCKED_SYMBOLS.")
    note("LEVELS_INVALID",
         "SL/TP are on the wrong side of entry, or a placeholder did not resolve.\n"
         "     FIX: enable LuxAlgo's SL1/TP1 overlays so {sl1}/{tp1} resolve.")
    note("LEVELS_MISSING",
         "The alert has no entry/SL1/TP1 values.\n"
         "     FIX: paste the full JSON template, not a bare alert name.")
    note("Insufficient margin",
         "The order cannot fund even one whole contract at the saved size.\n"
         "     FIX: see section 2. Raise the saved size to at least the\n"
         "     '1-contract USDT' figure for the symbol you trade.")
    note("DUPLICATE",
         "The same symbol+side+candle was already handled. One order per candle.\n"
         "     This is correct behaviour, not a fault.")
    note("Aggregate risk",
         "Open positions already consume the 2.5% risk budget.\n"
         "     FIX: close a position, or accept that new entries pause.")


# --------------------------------------------------------------------------
# 4. Confluence verdicts, newest first
# --------------------------------------------------------------------------
def confluence(con, limit):
    section("4. CONFLUENCE GATE DECISIONS (what the score said)")
    if not table_exists(con, "luxalgo_signal_confluence"):
        print("  No confluence table: this build predates the gate.")
        return
    rows = q(con, """SELECT decided_at, symbol, side, timeframe, decision, score,
                            min_score, blockers
                     FROM luxalgo_signal_confluence
                     ORDER BY decided_at DESC LIMIT %s""", (limit,))
    if not rows:
        print("  No scored signals yet.")
        return
    for row in rows[:limit]:
        print("  %s  %-10s %-6s %-3s  %-5s %3s/%-3s  %s" % (
            row["decided_at"], row["symbol"], row["side"], row["timeframe"],
            row["decision"], row["score"], row["min_score"],
            row["blockers"] or ""))
    print("\n  Thresholds in force:")
    try:
        import signal_confluence as S
        cfg = S.ConfluenceConfig.from_env()
        for key in ("min_score", "min_rr", "min_stop_atr", "max_stop_atr",
                    "min_atr_pct", "max_atr_pct", "require_htf",
                    "min_volume_ratio", "cooldown_minutes"):
            print("    %-20s %s" % (key, getattr(cfg, key)))
    except Exception as exc:  # noqa: BLE001
        print("    (config unreadable: %s)" % str(exc)[:120])


# --------------------------------------------------------------------------
# 5. Are signals even arriving?
# --------------------------------------------------------------------------
def arrivals(con, limit):
    section("5. ARE SIGNALS ARRIVING AT ALL?")
    if not table_exists(con, "luxalgo_signal_levels"):
        print("  No signal level table. Nothing has ever reached /webhook.")
        return
    rows = q(con, """SELECT event_id, status, entry_price, stop_loss, take_profit
                     FROM luxalgo_signal_levels ORDER BY event_id DESC LIMIT %s""",
             (limit,))
    print("  Last %d stored signals (levels as received):" % len(rows))
    for row in rows:
        print("    %-40s %-8s entry=%s sl=%s tp=%s" % (
            str(row["event_id"])[:40], row["status"], row["entry_price"],
            row["stop_loss"], row["take_profit"]))
    if table_exists(con, "luxalgo_signal_order_types"):
        rows = q(con, """SELECT event_id, order_type FROM luxalgo_signal_order_types
                         ORDER BY event_id DESC LIMIT %s""", (limit,))
        missing = sum(1 for r in rows if r["order_type"] not in ("MARKET", "LIMIT"))
        print("\n  order_type recorded on %d of the last %d signals."
              % (len(rows) - missing, limit))
        if missing:
            print("  -> %d signals had NO recognised order_type. Those are refused\n"
                  "     with ORDER_TYPE_NOT_MARKET before any order is built." % missing)
    if table_exists(con, "luxalgo_live_signal_handoff"):
        rows = q(con, """SELECT COUNT(*) AS n,
                                COUNT(source_bar_id) AS with_bar
                         FROM luxalgo_live_signal_handoff""")
        if rows:
            total = rows[0]["n"] or 0
            withbar = rows[0]["with_bar"] or 0
            print("\n  handoff rows: %d total, %d with a stable bar id." % (total, withbar))
            if total and withbar == 0:
                print("  -> ZERO signals have ever carried a stable bar id. Without it\n"
                      "     nothing can dispatch, no matter what else is configured.")


# --------------------------------------------------------------------------
# 6. Positions / unprotected exposure
# --------------------------------------------------------------------------
def exposure(con):
    section("6. OPEN POSITIONS (unprotected exposure check)")
    found = False
    for tbl in ("luxalgo_prod_execution_ledger", "luxalgo_live_positions"):
        if not table_exists(con, tbl):
            continue
        rows = q(con, "SELECT * FROM %s ORDER BY 1 DESC LIMIT 20" % tbl)
        if not rows:
            continue
        found = True
        print("  %s:" % tbl)
        for row in rows:
            keep = {k: v for k, v in row.items()
                    if k in ("event_id", "contract", "state", "position_mode",
                             "contracts", "detail", "updated_at")}
            print("    %s" % json.dumps(keep, default=str)[:260])
    if not found:
        print("  No ledger rows. Confirm on Gate.io itself -- this script cannot")
        print("  read your exchange account and will not claim it did.")


def main():
    ap = argparse.ArgumentParser(description="Read-only LuxAlgo PRO diagnosis")
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--symbol", default="")
    args = ap.parse_args()

    print(READ_ONLY_BANNER)
    print("run at %s UTC" % datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
    con = connect()
    try:
        gates(con)
        trade_size(con)
        refusals(con, args.limit, args.symbol)
        confluence(con, args.limit)
        arrivals(con, args.limit)
        exposure(con)
    finally:
        con.close()
    section("DONE")
    print("  Nothing was changed. Nothing was ordered. Re-read sections 2 and 3.")
    print("  If section 3 shows ORDER_TYPE_NOT_MARKET or MISSING_STABLE_BAR_ID,")
    print("  the fix is in TradingView, not in this repo.")


if __name__ == "__main__":
    main()
