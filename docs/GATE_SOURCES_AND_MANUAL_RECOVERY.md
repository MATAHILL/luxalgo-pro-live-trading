# Gate source references and manual recovery implementation

Reference date: 11 November 2026. Source locks remain OFF for real exchange
writes. No live trade or recovery close was submitted during development.

## Maintained primary references

- Gate futures REST specification and Python examples:
  https://www.gate.com/docs/developers/apiv4/en/futures/
- Gate SDK's futures initial-order schema:
  https://github.com/gate/gateapi-python/blob/master/docs/FuturesInitialOrder.md
- Gate SDK's futures order schema:
  https://github.com/gate/gateapi-python/blob/master/docs/FuturesOrder.md
- CCXT is an alternative MIT-licensed library:
  https://github.com/ccxt/ccxt

This change implements the documented API contract with the existing transport;
it does not copy third-party SDK source files or introduce a paid dependency.
No strategy, LuxAlgo price or signal direction is changed.

## What changed

`manual_recovery.py` connects existing exact-position checks and one-shot close
claims to a manually invoked recovery flow. `/terminal/recovery` provides a
signed-in review interface. Review data is stored server-side, session-bound,
CSRF-protected, digest-checked and expires within 30 seconds. The close button
is source locked and requires explicit user acknowledgment after release.
HALT must already be active and stays active after recovery.

The new close variant sends an exact signed quantity with reduce_only=true.
It never sends size=0 close-all or hedge auto_size, so an exposure increase
after readback cannot expand the amount closed. Gate documents signed-size
hedge partial closing without auto_size. The original full-close helper is
retained for backward compatibility; the new manual flow uses exact size.

Preparation requires a known bot entry order ID, matching client text,
contract, direction, full fill and current exact position. Missing/partial
entry evidence, multiple positions, open orders or any protective trigger
cause rejection and require direct manual exchange review. No automatic
trigger cancellation or blanket close is added.

The recovery claim is committed before consumption and the only close POST.
Lost acknowledgment, partial close, non-flat readback or failed journal writes
never trigger another close. A verified close also requires a durably consumed
and finalized recovery claim before the production ledger becomes
CLOSED_VERIFIED. HALT remains active and certification remains false.

## Validation and limits

Local discovery: 231 tests; 224 passed and seven PostgreSQL tests skipped
because this runtime cannot open local sockets. Dedicated CI uses disposable
PostgreSQL and fake exchange transport. Added database coverage verifies the
recovery claim/close/finalization/ledger SQL and competing claim consumers.
CI completion must be checked before deployment.

These are development checks, not real Gate fill/protection/recovery evidence.
The implementation remains unsuitable for source release until independent
review and user-performed bounded exchange acceptance establish that evidence.
There is no automatic emergency close worker; ambiguous or protected exposure
still requires manual exchange intervention. This change does not enable new
entries, remove HALT, activate an operator or issue a certification.
