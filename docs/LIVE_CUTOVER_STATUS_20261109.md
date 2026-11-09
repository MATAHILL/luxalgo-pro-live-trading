# 9 November 2026 — Gate.io live cutover preparation (non-trading)

This report records the *read-only* audit and the code-preparation branch. It does **not** grant authority for exchange orders, change safety locks, or certify the bot.

## Production facts recorded

- Render service: `luxalgo-pro-live-trading` (not suspended). Last successful deployment: 2026-11-06, commit `79cfcd5267df08dad16744768bf5294f0716623e`.
- PostgreSQL emergency control: `halted=true`, reason `UNPROTECTED_OR_UNKNOWN_EXCHANGE_EXPOSURE` as of 2026-11-07.
- Production operator: OFF, reason `NOT_CERTIFIED`.
- Production activation certification: no row.
- Production execution ledger: zero entries; this is **not proof of an earlier live trade**.
- Production signal handoff: 169 stored alerts, 79 with stable bar identity, valid LuxAlgo levels, and declared MARKET order type.
- Most recent stored signal: 2026-11-03 07:25:03 UTC. A quiet LuxAlgo strategy **may simply have no new valid signals**. Verify TradingView alert activity and webhook delivery before blaming the integration.
- Exchange monitoring recently reported read-only reconciled positions=0, orders=0, triggers=0. A flat snapshot is *not* certification.

## Change in this branch

The authenticated read-only `GET /terminal/live-execution/unlock-readiness` endpoint includes a `signal_feed` diagnostic based on the production handoff table, explicitly labeling stale or unavailable data without claiming a trade or lifting safety locks. It performs no exchange POST. Existing webhooks are unchanged. The production main branch is not modified until code review.

## Required steps before any real-money order

1. Compare TradingView's active alert log with the latest production webhook timestamp. Confirm the URL, HTTP delivery, paid indicator condition, 5-minute symbol/timeframe, event/bar identity, MARKET declaration and real LuxAlgo entry/SL/TP1 levels. Do not invent trade signals just to test.
2. Check authenticated Gate.io USDT account, positions, open standard orders, and native conditional orders. Confirm account position mode, leverage/margin assumptions, contract metadata, equity and risk caps. Review why the durable HALT was recorded. Never clear a HALT simply because the account appears flat.
3. Review and independently test the **unreleased** live sender, acknowledgment handling, full/partial fills, native stop + target readback, restart recovery, and bounded emergency close. GitHub CI and mocked regression tests are not exchange execution evidence.
4. Obtain a *new, transaction-specific* authorization for exactly one minimum-size acceptance trade: contract, side, validity window, notional/margin/loss caps, slippage, and emergency close permission. Do not reuse an earlier authorization.
5. Verify actual exchange fill, BOTH native protective orders, all IDs and post-restart state. Only after that may the owner separately approve unattended operation. Preserve `EXCHANGE_WRITE_RELEASE=False`, `AUTOMATIC_WORKER_RELEASE=False`, and durable HALT until the complete process.

Operational limits remain 0.5% risk per entry, 2.5% aggregate, maximum five positions, maximum 5x leverage. **No order was placed by this code-preparation change.**
