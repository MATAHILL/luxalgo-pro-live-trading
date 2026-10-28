# LUXALGO PRO — Gate.io Futures monitoring and safety-locked execution

Production: https://luxalgo-pro-live-trading.onrender.com  
Repository: `MATAHILL/luxalgo-pro-live-trading`  
Render auto-deploy source: `main`

## Actual production capabilities

- Receives authenticated TradingView/LuxAlgo webhook messages and preserves receipt, level, bar-identity, and order-type audit records in PostgreSQL.
- Displays Gate.io USDT-M public radar/scanner data, authenticated read-only account reconciliation, incoming signals, and a BTC acceptance-test status panel.
- Implements code-level execution planning, risk caps, an emergency-halt ledger, exchange-readback helpers, and offline/mock regression tests.
- The **production live order sender is NOT released**. `EXCHANGE_WRITE_RELEASE = False`, `AUTOMATIC_WORKER_RELEASE = False`, and the persisted production emergency HALT are independent safety gates. The dashboard's BOT ON control cannot lift them.
- **No exchange-confirmed acceptance trade, verified native SL/TP placement, or completed real-money certification has been demonstrated.** Neither a passing test suite nor a healthy dashboard grants trade authorization.

## Verification

- `.github/workflows/live-market-cutover-ci.yml`: offline live/webhook/market safety tests.
- `.github/workflows/testnet-safety.yml`: legacy safety and UI regression suite. Historical Testnet recovery is retained separately; it is not a production execution authority.
- `/health`: process liveness only.
- `/ready`: required PostgreSQL schema connectivity; **not trade readiness**.
- `/terminal/production-bot-control`, `/terminal/acceptance-test/status`, `/terminal/live-execution/snapshot`: authenticated status/readback routes. Snapshot reconciliation can persist a safety HALT when it detects uncertainty; it never sends orders.
- `docs/LIVE_ACCEPTANCE_GATES.md`: live acceptance criteria and limitations.

## Change-control warning

The `live-execution-dev` branch has substantially diverged from `main`; **do not merge or deploy it directly** without selective review and independent regression tests. Only `main` auto-deploys to the current Render web service.

## Before live activation

Complete an independently authorized bounded acceptance window, current Gate.io account/contract and SL/TP capability readback, a genuine qualifying LuxAlgo alert, one accepted-and-filled minimum-size entry, native protective-order readback, failure/restart reconciliation, and final owner authorization. Leave general live trading OFF until every gate is verified.

Never publish API credentials, TradingView tokens, exchange secrets, or terminal login credentials.
