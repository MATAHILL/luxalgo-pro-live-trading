# Controlled acceptance release review — 10 November 2026

Decision: HOLD FOR LIVE RELEASE. Code review is not independent live certification.

Reviewed baseline: 3558d506ba8658aefa6aef0ddb9aadf69e7e424b (PR #36).
The baseline passed 214 CI tests, including five real disposable PostgreSQL
tests. Exchange order/fill/protection responses in those tests were mocked.

## Finding corrected

`submit_once` rebuilt a fresh review and passed that object to the durable
claim with the original approved digest. Rebuilt timestamps could renew the
30-second review lifetime during a slow exchange refresh and the persisted
review could no longer hash to its stored digest. Claim the original reviewed
object instead. Fresh account checks still validate the exact plan; they do
not renew consent. Regression tests cover digest linkage and expiry during
an exchange refresh, with no entry call on expiry.

## Controls reviewed

- Sender, acceptance and continuous-worker source gates remain disabled.
- Authenticated session and CSRF precede manual submission; the stored review
  is session-bound and expires. Original signal levels remain server-sourced.
- Exact one-contract scope, financial ceilings and short expiry are checked.
- A committed single-use claim and risk reservation precede any entry attempt.
- Existing global advisory locking serializes claims and HALT/dispatch checks.
- Unknown acknowledgment and partial fill never retry entry; missing or late
  linked native SL/TP readback leads to HALT and manual exchange review.
- Reconnect/reconciliation does not reset claims or resend orders.

## Evidence still absent

A read-only query of the production execution ledger returned no rows during
this review. There is no recorded bot entry order ID, SL ID, TP ID or restart
acceptance result to independently read back. No real order was submitted by
this review. A manually placed exchange trade cannot substitute for bot-linked
entry/protection/recovery evidence.

This implementation deliberately has no automatic emergency close. The
separate one-shot recovery helper has not been live-certified or wired into
acceptance. Protection failure requires manual exchange intervention.

## Release boundary

Do not manufacture a certification row, lift HALT, change source constants or
enable automatic execution merely because code and database tests pass.
Independent release review and an exact bounded user-performed live test are
still needed. Confirm actual fill, both native protective IDs, protection
timing, and post-restart/recovery readbacks before deciding on certification.
No certification or trading-safety control was changed during this work.
