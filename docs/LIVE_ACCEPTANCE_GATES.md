# LUXALGO PRO — Production acceptance gates and evidence specification

**Status: NOT CERTIFIED FOR REAL-MONEY AUTOMATION.**
Owner-controlled Gate.io USDT-M Futures account. All implementation remains SOURCE_LOCKED.
No execution may occur because a dashboard banner, CI success, a test fixture, or an environment
variable says 'ready'. Only a separately authorized, tightly capped live acceptance procedure
may establish real exchange evidence.

## Hard stop conditions (enforced conceptually; release gate remains OFF)

**STOP-01 — Build and evidence.** Latest deployed Git SHA is unknown, Render deploy not LIVE,
GitHub Actions regression run lacks verified SUCCESS, offline suite fails, or tests run against
a different commit. Action: abort acceptance, preserve logs, no new orders.

**STOP-02 — Authorization.** User has not separately authorized ONE specified contract,
side, maximum USDT margin/notional and loss exposure, expiry time, and permission for emergency
reduce-only close. Action: prohibit every exchange-writing request. Never infer consent
from a prior request to implement code.

**STOP-03 — TradingView source.** Invalid/missing stable tv_bar_id; repeated symbol/side/timeframe/bar,
untrusted payload, wrong TF, signal older than 60 s, wrong-side SL/TP, or absent MARKET declaration.
Legacy alerts remain signal-only. Action: reject dispatch, retain audit record.

**STOP-04 — Exchange inventory.** Authenticated GET failures, API rate limit, incomplete pagination,
invalid USDT account, order status unknown, untracked position/order, orphan trigger, dual/one-way
mode uncertain, mismatched leverage/margin mode, concurrent worker ambiguity. Action: durable HALT.

**STOP-05 — Risk.** Planned trade loss >0.5% exchange-verified equity, total reserved loss >2.5%,
more than five distinct active/pending contracts, leverage >5x, duplicate contract,
negative/unknown free margin, contract minimum too large for risk budget, invalid multiplier,
order precision/tick uncertainty, mark-price spread outside authorized slippage tolerance,
or liquidation risk exceeding approved terms. Action: do not submit.

**STOP-06 — Entry acknowledgment / partial fill.** POST timeout or uncertain acknowledgment,
unmatched deterministic client text, inconsistent order ID/fill/position, partial IOC fill,
or current position unlike the approved count/side. Action: never retry entry POST;
quarantine in ledger, durable HALT, determine exposure by fresh exchange GET.
Only separately authorized, verified reduce-only emergency handling may be attempted.

**STOP-07 — Protective order.** Actual filled entry cannot be linked to two independent
exchange-native OPEN SL and TP trigger IDs with correct contract, direction, price,
size/reduce-only semantics and their recorded IDs; trigger placement/readback >5 s from
first confirmed fill (acceptance target, not proof of guarantee); unexpected cancellation,
expiry, partial closing, side changes or trigger mismatch. Action: HALT and execute
authorized one-shot emergency recovery only when live exposure is positively verified;
otherwise manual exchange intervention. No fabricated 'protected' status.

**STOP-08 — Restart/recovery.** Crash after any accepted POST, recovery cannot read the
order/position/trigger ledgers, live order still unverified after restart, stale/corrupt
database state, repeated ambiguous close outcomes, orphan residual triggers. Action:
persistent HALT; no replay of original entry; record manual-intervention requirement.

**STOP-09 — Monitoring.** Exchange synchronization stale >60 s, two consecutive missed
account/order/trigger readbacks, exchange P&L inconsistent with confirmed position,
or an apparent auto-close cannot be confirmed flat and trigger-free.
Action: HALT new entries, show UNVERIFIED (not zero P&L or 'closed').
Public radar/scanner outage never automatically authorizes/arms execution.

**STOP-10 — Test completion.** A single mandatory negative test fails, a privacy/security
issue or unapproved write appears in logs, or acceptance evidence lacks an exchange order
ID, exact filled size, both native trigger IDs and post-restart readbacks.
Action: NOT CERTIFIED; no activation of continuous automation.

## Mandatory preliminary (no-order) evidence

1. Exact GitHub commit, Render deploy ID and LIVE status, version/date in UTC and IST.
2. Actual GitHub Actions workflow run ID, URL and conclusion=success at the SAME SHA.
   Deployment success / generic GitHub status is NOT equivalent.
3. Render offline test output, exit 0, test count and failing test names if any.
4. PostgreSQL control: exactly one row, halted=TRUE, ledger presence, zero unauthorized sends.
5. Read-only Gate USDT account and ALL open positions/orders/trigger orders inventories;
   pagination and one-way/dual-mode evidence. Never log API secrets.
6. Public Futures ticker/candle/5m/15m scanner and independent crypto news checks.
7. Signed webhook positive example and duplicate/restart negative examples with stable bar ID,
   and legacy missing-bar example correctly retained as SIGNAL_ONLY.
8. Fault injection: GET timeout/rate limit, partial-fill replay, POST-ack unknown,
   duplicate webhook, double reservation, positions 4→5 and 5→6,
   trigger cancellation, orphan orders, database failure, process restart and emergency
   close ambiguity. All must fail closed.

## Strictly limited live acceptance procedure — NOT AUTHORIZED YET

**Separate authorization must contain:** exact market/contract, LONG or SHORT, UTC start/end,
maximum USDT total margin, maximum USDT notional exposure, maximum USDT loss risk (at most
0.5% current equity), maximum slippage, max 5x leverage, number of tests (initially ONE),
and specific permission to place native stops/targets and attempt ONE reduce-only
emergency close if exchange protection fails. No keys or private wallet seed phrases.

**Procedure after all preliminary evidence passes:**
A. Freeze commit and verify signed intended instructions/position-mode. Re-read equity,
contract multiplier, minimum lot, prices, live positions, working orders, trigger orders
and fresh DB HALT. If minimum valid lot exceeds ANY cap, STOP.
B. Authorize only the one contract/one entry in a bounded test window; persist reservation
and client text BEFORE submitting. Record request timestamp and exchange acknowledgment.
Never repeat POST after a timeout.
C. Reconcile actual exchange entry order ID, fill price, settled fee, signed contract size
and current position via independent authenticated GET. A partial fill is STOP-06.
D. Place native SL+TP using exchange-approved single/hedge mode, and verify two actual
OPEN price-order IDs plus trigger geometry and close/reduce-only semantics. Failure is STOP-07.
E. Restart/redeploy the execution worker without resending entry. Independently re-read
position, order, SL and TP IDs; confirm no duplicate orders or orphan protection.
F. Exercise authorized isolated emergency HALT (new entries blocked) and, if separately
authorized, ONE safe reduce-only close. Confirm exchange flat and zero orphan triggers.
No 'CLOSED_VERIFIED' or P&L result without actual exchange history.
G. Reconcile fees, funding, fills, P&L, post-test balance, snapshots, dashboard and logs.
Preserve immutable evidence. Re-lock all source/env write gates.
H. Only after successful limited single-position acceptance may a separately approved
multi-position test be proposed. No automatic promotion to 5-position live trading.

## Required evidence record format (one record per checkpoint)

Use JSONL with **one immutable record per stage**. The exact schema is:

```json
{
  "evidence_id": "unique-redacted-id",
  "stage": "PRECHECK|WEBHOOK|ENTRY|FILL|STOP|TARGET|RESTART|HALT|CLOSE|FINAL",
  "outcome": "PASS|FAIL|BLOCKED|NOT_TESTED",
  "timestamp_utc": "ISO-8601 timestamp",
  "timestamp_ist": "ISO-8601 timestamp with +05:30",
  "git_commit_sha": "full 40-char SHA",
  "render_deploy_id": "exact deployment ID",
  "github_actions_run_id": null,
  "github_actions_url": null,
  "runner_test_count": 0,
  "environment": "GATE_IO_LIVE_USDT_FUTURES",
  "mode": "READ_ONLY|MOCK|EXPLICITLY_AUTHORIZED_LIVE_TEST",
  "event_id_hash": null,
  "tv_bar_id_hash": null,
  "client_text": null,
  "contract": null,
  "side": null,
  "exchange_entry_order_id": null,
  "exchange_filled_contracts": null,
  "exchange_fill_price": null,
  "exchange_stop_order_id": null,
  "exchange_take_profit_order_id": null,
  "stop_order_status": null,
  "take_profit_order_status": null,
  "position_after_restart": null,
  "equity_usdt": null,
  "risk_reserved_usdt": null,
  "aggregate_risk_usdt": null,
  "approved_max_margin_usdt": null,
  "approved_max_notional_usdt": null,
  "approved_max_loss_usdt": null,
  "orders_created_this_stage": 0,
  "untracked_open_orders": null,
  "untracked_open_triggers": null,
  "emergency_halt_persisted": true,
  "source_log_reference": "Render/GitHub API log id and timestamp",
  "source_exchange_reference": "Exchange GET endpoint and redacted response digest",
  "failure_reason": null,
  "remediation_commit_sha": null
}
```

For non-trading stages, keep exchange fields NULL, not fabricated 0 or sample IDs.
Include the exact original signed TradingView payload hash (not the webhook secret)
and its event/key derivation. Source logs must be tamper-evident; preserve original
redacted exchange API readbacks and hashes. Never include API key/secret or PII.

## Certification decision

**READY = ALL** CI success for target SHA, offline suite success,
valid TradingView end-to-end identity, read-only account + all positions/orders/triggers,
successful real-entry and BOTH live native protection readbacks, partial/unknown-ACK
handling, five-position atomic stress tests, restart and emergency acceptance, no orphan
orders, accurate P&L/dashboard and signed limited authorization. Any missing item:
**NOT CERTIFIED / LIVE ORDERS OFF**.

Operational minimum risk remains 0.5% per entry, 2.5% aggregate, at most 5
concurrent contracts, at most 5x leverage. The initial acceptance must be
one tiny approved position, and never exceed the separately specified maxima.
