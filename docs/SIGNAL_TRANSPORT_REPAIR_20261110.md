# Native LuxAlgo signal transport repair — 10 November 2026

## Result

The TradingView signal transport was repaired in the owner's signed-in account. This change did not release exchange-write or automatic-worker code locks, enable the production operator, clear HALT, or place a test order.

Owner selected **Signals & Overlays confirmations with its own SL1/TP1**, rather than PAC CHoCH/order-block entry conditions.

## Saved TradingView configuration

Four existing active alerts were converted to Signals & Overlays [7.3.3], **Any alert() function call**, 5m:

- LUX S&O JCTUSDT 5M BUY SELL SL1 TP1
- LUX S&O DOGEUSDT 5M BUY SELL SL1 TP1
- LUX S&O AKEUSDT 5M BUY SELL SL1 TP1
- LUX S&O ONEUSDT 5M BUY SELL SL1 TP1

Each uses the existing Render /webhook destination. Saved source snapshots were inspected again after reload. ONE was explicitly refreshed from the older indicator snapshot to the updated snapshot. The separate PAC ONE SELL alert was already stopped with a calculation error and remains stopped, avoiding a second entry source. Other stopped AlgoWay and UT alerts were not reactivated.

Indicator settings retain sensitivity 12, TP/SL mode Signals and distance 5. Only the existing normal bullish and bearish confirmation alert conditions are enabled; contrarian, exit, TP-hit, SL-hit, custom creator and scripted-alert event conditions remain disabled. This repair does not change indicator calculations.

## Message correction

The indicator's previously saved full-price message had a different token from the repaired Render alerts. Its token was replaced privately with the existing webhook credential used by those alerts. No credentials are stored in this document.

The native indicator message sends:

```json
{
  "token": "<existing private webhook token>",
  "ticker": "{ticker}",
  "alert": "{default}",
  "tf": "{tf}",
  "bartime": "{time}",
  "entry": "{close}",
  "sl": "{sl1}",
  "tp1": "{tp1}",
  "order_type": "MARKET"
}
```

These single-brace placeholders belong in the **indicator's native any-alert message**, not as a TradingView regular custom-alert message. The native message is captured by each saved alert snapshot.

Official reference: https://docs.luxalgo.com/platform/algos/signals-overlays/alerts

Entry is the confirmation candle's close reference, **not an exchange fill or a promise of an executable entry at that price**. SL1/TP1 come directly from the indicator. No ATR substitute or invented price was attached to a PAC signal.

## Genuine production receipt evidence

Read-only database inspection found a new ONEUSDT LONG signal at **2026-11-10 06:00:09.270937 UTC / 11:30:09 IST**, after the configuration repair:

- timeframe: 5m
- entry reference: 0.0013941
- SL1: 0.0013519
- TP1: 0.0014363
- level validation: VALID
- order declaration: MARKET
- stable source bar ID: TVBAR_5m_00005980967
- stored handoff status: SIGNAL_ONLY

This demonstrates a genuine ONE alert reached the authenticated receiver and its original numeric levels were stored. It does **not** verify all four symbols, a bearish receipt, an exchange order, or exchange-native protection.

The prior AKE receipt at 2026-11-10 02:17:18 UTC was MISSING levels and came from the previous PAC transport.

## Execution state retained

Read-only inspection during this repair:

- production operator_on: false, reason NOT_CERTIFIED
- production execution HALT: true; historical reason UNPROTECTED_OR_UNKNOWN_EXCHANGE_EXPOSURE
- production execution ledger: 0 rows
- activation certification: 0 rows
- source sender and automatic worker: still False in the inspected main code
- latest reconciliation log at 06:01:37 UTC: READ_ONLY_RECONCILED, positions=0, orders=0, triggers=0

The retained HALT reason is historical; the latest readback reports no exchange exposure. It was not cleared.

## Offline verification

Source inspected at main commit e7a7f74dace14d21b96c34026e133b2f329e6187. Installed the repository's declared Python dependencies in the local test environment, then ran:

```
python -m unittest discover -s tests -p 'test_live_*.py'
```

Result: **178 tests passed**. These are offline/mock safety and webhook tests, not live exchange acceptance.

No production source code or environment was changed by the transport repair. Continuous live execution remains unverified and disabled. Further activation requires independent bounded exchange acceptance, verified native protection/recovery evidence and the applicable owner-authorized release process; a signal receipt or test pass cannot substitute for that evidence.
