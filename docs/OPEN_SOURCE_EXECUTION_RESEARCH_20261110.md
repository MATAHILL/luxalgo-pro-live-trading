# Free Python sources and the existing Gate executor

Researched 10 November 2026. Baseline main:
`5f27cdab8b69fff506cac655c972441674108c4e`.

## Decision

Continue the existing LuxAlgo -> TradingView -> Render -> Gate USDT futures
project. It already contains a native REST transport, contract-aware sizing,
entry/protection readback, durable one-shot claims and owner-reviewed recovery.
Replacing it with a generic webhook example would discard those integrations.
No new paid software or SDK dependency is required for the change in this review.
This is an engineering recommendation, not a statement of live readiness.

## Primary sources and applicability

| Source | Useful material | Decision for this repository |
| --- | --- | --- |
| [Gate's official Python SDK](https://github.com/gate/gateapi-python) | `FuturesApi`, `create_futures_order`, `get_futures_order`, contract and trigger APIs | Authoritative Python reference. Keep the existing small native client; adding a second transport does not resolve release gates. |
| [Gate perpetual futures REST specification](https://www.gate.com/docs/developers/apiv4/en/futures/) | Contract multipliers, signed sizes, one-way/hedge closing semantics, native protective orders, request expiry | Use API semantics to improve and verify the existing implementation. |
| [CCXT manual](https://docs.ccxt.com/docs/manual) and [Gate adapter](https://docs.ccxt.com/docs/exchanges/gate) | Unified Python orders and exchange/market feature detection | An alternative adapter, not an activation switch. Attached stop/target support is capability-dependent. No migration justified here. |
| [LuxAlgo Trade Relay](https://github.com/LuxAlgo/trade-relay/blob/main/README.md) | Idempotency, audit and explicit broker capabilities | Node-based implementation. Published broker table does not establish Gate futures live execution; not a drop-in replacement for this Python bot. |
| [CryptoGnome TradingView webhook example](https://github.com/CryptoGnome/Tradingview-Webhook-Bot) | Basic Flask webhook routing | Published exchange list identifies Bybit/Binance futures, not a verified Gate futures adapter. Do not substitute it for this executor. |

Public examples demonstrate API usage. None proves that this deployed bot has
filled, protected or recovered an actual live order. Do not run generic demos
that transfer wallets, cancel unrelated orders or change leverage implicitly.
No third-party source files were copied; this patch implements a documented
API contract, avoiding extra dependencies and copied-license obligations.

## Reuse the earlier Testnet evidence

A read-only query of `luxalgo_testnet_protected_journal` found October 27-28
records: three `CLOSED_VERIFIED`, five `EMERGENCY_CLOSED`, one
`PROTECTED_VERIFIED`, and one `RECOVERING`. Recent examples include:

- XRP: `NATIVE_TPSL_RECHECK_PASSED`, with entry and two native protection IDs.
- ETH and DOGE: `POSITION_FLAT_ON_TESTNET`, each with entry/protection IDs.
- AIN: `RECOVERY_READBACK_UNCERTAIN`, with no entry/protection IDs in those
  journal details.

These records establish that earlier Testnet execution work occurred. They
are historical journal evidence, not a fresh independent exchange readback,
and include an unresolved recovery result. Missing current Testnet credentials
does not erase this history. No Testnet trades were repeated for this research.
Existing records should be mapped to the relevant code paths and retained as
evidence; they cannot automatically certify a different live execution path.

## Specific improvement implemented from the research

Gate documents `x-gate-exptime` on order creation: an absolute millisecond
deadline, rejected if the request reaches Gate after expiry. Local checks
alone cannot reject a request delayed after leaving the process.

- Every `GateLiveClient.submit_entry` derives the expiry from the original
  signal receive time plus 60 seconds; future, missing or expired times fail.
- Controlled acceptance also carries the earlier of the original 30-second
  review deadline and authorized scope end. Refreshing account data cannot
  extend the user's review. The reviewed plan/digest is not mutated.
- The native transport checks the deadline immediately before constructing
  the authenticated request and sends the documented header. It rejects
  invalid types, expired deadlines and deadlines beyond 60 seconds.
- Protection creation and recovery closes do not inherit an old entry expiry.
- Source/environment/HALT/certification/operator controls are unchanged.
  An unknown acknowledgement still halts without retry; expiry does not prove
  rejection when Gate received a request before the deadline.

Offline tests use fake credentials, fake transport and fake exchange responses.
They cover wire headers, review/signal/scope expiry, elapsed time, invalid
inputs, retained locks and no replay on timeout. Dedicated CI runs the existing
seven PostgreSQL tests against a disposable local database. No real exchange
write is part of these checks.

## What still separates this from live operation

The software change does not release the sender or continuous worker, clear
HALT, enable the operator or write an acceptance certificate. The baseline
production ledger had no bot execution records from which to establish live
fill, two native protective orders and recovery. Historical Testnet records
must not be relabelled as those live records.

The current release review remains HOLD. The bounded acceptance and recovery
interfaces exist, but are source locked. A future release must define and
review the exact permitted execution scope, resolve the uncertain historical
recovery evidence where applicable, and obtain actual matching exchange
readback before claiming live acceptance. No market script or free library
supplies those account-specific facts.
