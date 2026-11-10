# Controlled single-order acceptance implementation

Status: implemented for offline review, SOURCE LOCKED, not certified or deployed.
No exchange order, production schema migration, operator activation or HALT
clearance was performed while building this change.

## Integration boundary

`controlled_acceptance.prepare(scope, stored_signal, client)` reads current
exchange inventory and contract metadata, preserves original LuxAlgo levels,
and returns a canonical review digest. `submit_once(connection_factory,
prepared, reviewed_digest, stored_signal)` is the manual integration entry
point. It is deliberately not registered as a Flask route, webhook handler,
worker or runnable CLI. Importing this module never accesses an account.

The integrator must load the signal from the authenticated stored handoff;
browser JSON must never become an alternative source for signal levels or
timestamps. A later user-facing submit control needs authenticated session,
CSRF, explicit exact-plan review and transaction-specific user confirmation.
This revision does not provide that control or authorize submission.

## Scope and safeguards

The caller supplies an explicit unique scope ID, contract, LONG/SHORT,
start/end epoch seconds, contracts=1, leverage, and maximum margin, notional
and modeled loss. Nothing defaults to an owner approval or a historical test.
Window length is at most 15 minutes. Implementation ceilings are 1.70 USDT
margin, 8.30 USDT notional, 0.10 USDT modeled loss, 0.5% equity risk and 5x
leverage; these ceilings are not a recommendation or an authorization.

Preparation and dispatch reject stale signals (>60s), stale readbacks (>30s),
wrong contract/direction, missing bar identity, invalid original levels,
off-tick protections, exchange minimum-lot conflicts, unknown inventories,
existing exposure, excess mark-price slippage or changed reviewed plans.
Risk includes modeled taker fees and slippage, not a guaranteed loss bound.

Submission requires ALL separately reviewed source/env permissions, including
the new `ACCEPTANCE_RELEASE` constant. It remains False. The existing exchange
sender remains False and automatic-worker release must remain False even for
a future isolated trial. Global production HALT must already be independently
reconciled and cleared. This code has no function to clear it.

A PostgreSQL advisory lock serializes the single-use acceptance claim with
the existing production risk ledger and HALT. Scope ID and source event are
unique. Claim and SENDING reservation commit before the only entry attempt.
The final entry attempt is also serialized against HALT. A consumed claim,
crash, timeout, unknown acknowledgment or partial fill never replays an entry.
Original native SL/TP acknowledgments must match independent open-trigger
readbacks and exact isolated exposure. Protection completion later than the
five-second acceptance target is a failure, not a certification.

Successful readback stores `PROTECTED_REVIEW_REQUIRED` and halts new entries.
This does not close the position or remove its protective orders. Any
post-claim failure records unknown exposure, attempts durable HALT and
requires manual exchange review. No automatic emergency close or trigger
cancellation is attempted. Failure to persist HALT/evidence is reported
explicitly. The existing production ledger permits read-only restart
reconciliation; neither restart nor an empty exchange snapshot resets claims.

## Validation and remaining release work

Local suite: `python -m unittest discover -s tests -p 'test_live_*.py'`:
202 passing tests, including 20 new acceptance tests. Exchange transports,
claims and journals in lifecycle tests are simulated. PostgreSQL SQL guards
are inspected with mocked cursors; competing submissions use a simulated
atomic store. These tests do NOT prove real PostgreSQL transaction behavior,
exchange acceptance, protection latency, restart recovery or live readiness.

Before release: run migration and true concurrent/commit-failure/restart
integration tests against disposable PostgreSQL; complete independent source
review; implement the authenticated user review/submit boundary; verify a
fresh exact bounded owner scope; then let the user perform the financial
submission. Retain source/env locks until those release prerequisites pass.
Only real readbacks can establish live fills/native protection evidence.
Continuous automation and final certification are separate work.
