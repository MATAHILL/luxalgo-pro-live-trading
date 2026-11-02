# Bounded acceptance implementation — review only

This change removes the certificate-before-first-trade dependency for an
explicitly approved single event. It is not deployed or certified for live use.
It does not change the source release constants or any environment permission.

`acceptance_authorization.create_approval` stores one immutable reviewed plan,
an operator audit reference and an explicit UTC interval of at most 15 minutes.
The plan must request exactly one whole contract from XRP/NEAR/BNB/INJ/SAND.
Its original entry, SL, TP, direction, leverage, risk, timestamp and event identity
are bound by a digest. The caller commits this approval in a trusted operator
context. No webhook or HTTP endpoint can create an approval in this change.

`execute_confirmed_signal(..., acceptance_approval_id=...)` consumes the record
inside the same transaction as risk reservation. Row locking and unique event
identity prevent simultaneous use. Normal automated calls omit this argument
and continue to require production operator state and acceptance certification.
Immediately before sending, the acceptance path rechecks expiry, revocation,
the plan digest and emergency HALT. An attempt is never automatically rearmed.
The existing exchange preflight, atomic risk reservation, fill readback,
SL/TP readback and post-acknowledgment failure handling remain in use.

## Remaining release work

- Integrate an authenticated operator review UI/CLI that obtains a current
  stored genuine signal, builds a one-contract plan from fresh exchange data,
  displays its notional, margin and modeled loss, and records explicit approval.
  The current general position-sizing function must not be used unchanged:
  it can size more than one contract, which this new path rejects.
- Verify actual PostgreSQL transaction/concurrency behavior and rollback.
  Included tests use mocks and do not establish production database evidence.
- Complete emergency recovery integration. Existing post-ack failure handling
  persists HALT and requires manual exchange review; it does not automatically
  close a potentially unprotected position.
- Review the deliberate source release, environment controls and runtime smoke
  expectations together. This patch does not lift any of those locks or HALT.
- Have the operator perform the bounded live acceptance and capture actual
  entry, fill, protection, restart and recovery evidence before certification.

No new acceptance record may serve as proof that a real trade occurred.
No successful mock test enables continuous trading. Do not merge this draft
as a claim that the bot is ready for real-money use.
