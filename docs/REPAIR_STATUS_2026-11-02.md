# Live preparation repair — 2 November 2026

Baseline: main 90b8152b50141aa0c38dc43f1544e529d7610451.

## Changes

- Share a validated LUXALGO_LIVE_RISK_PERCENT setting between contract sizing,
  fresh preflight, atomic reservation, and operator status. Range: positive,
  at most 3%; absent configuration retains 0.5%. Preserve the existing 2.5%
  account aggregate cap. Thus a configured 3% ceiling does not authorize 3%
  exposure while the stricter aggregate cap remains in place.
- Size against available margin including entry fees and the modeled slippage
  allowance, so a margin-limited plan need not fail unchanged preflight.
- Recheck operator/certification/HALT while holding the shared advisory lock
  in the same transaction as RESERVED -> SENDING. This serializes that claim
  with operator OFF and HALT; it does not eliminate the network submission gap.
- Expose the effective risk policy in authenticated operator status and boot logs.

## Release status

Exchange sender and continuous worker source locks remain false. No monetary
request was submitted. No certificate or HALT was overwritten. Manual positions
and the suspended lux-direct-bot branch/service were not changed.

Fresh Render deployment metadata identified the baseline commit as live.
Application logs reported authenticated read-only reconciliation, zero positions,
zero orders and zero triggers. These logs are not a new exchange snapshot or
evidence of a filled acceptance trade.

Terminal sign-in was rejected: incorrect credentials or too many attempts.
Authenticated terminal/DB and genuine TradingView alert review remain blocked.

Outstanding implementation/verification includes an independently authorized
bounded acceptance path (current executor requires final certification before
entry), integrated automatic post-entry emergency recovery, native orphan-trigger
cleanup, durable closure verification, and real database concurrency/restart
qualification. Genuine LuxAlgo payload completeness must be checked at source.
Do not enable continuous writes or claim acceptance on offline test results.
