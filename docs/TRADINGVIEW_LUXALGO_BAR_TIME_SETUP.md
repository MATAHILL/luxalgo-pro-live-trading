# LUXALGO PRO — Existing LuxAlgo Any-alert() integration qualification

**Exchange target: Gate.io LIVE USDT-M. Bot OFF, SOURCE_LOCKED, emergency HALT ON.**
This message configuration receives and validates alerts only. It never sends an
exchange order unless separate financial authorization and all code/DB gates pass.

## Verified behavior and prerequisite

TradingView's **Any alert() function call** sends the message created by the
indicator's `alert()` calls; the alert dialog itself cannot override those
dynamic messages. LuxAlgo Signals & Overlays documents a **custom message**
under its *ANY ALERT() FUNCTION CALL CONDITIONS* settings, including placeholders
`{default}`, `{ticker}`, `{tf}`, `{close}`, `{sl1}`, `{tp1}`, `{time}`.
Source: https://docs.luxalgo.com/platform/algos/signals-overlays/alerts

Current production webhook receipts have valid levels but no `tv_bar_id` or
`order_type`; those are **legacy SIGNAL_ONLY** until the TradingView-side
message is updated. No production alert has yet passed stable bar identification.

## Paste in LuxAlgo Signals & Overlays indicator custom alert() message setting

```json
{
  "token": "<KEEP_YOUR_EXISTING_PRIVATE_WEBHOOK_TOKEN>",
  "ticker": "{ticker}",
  "alert": "{default}",
  "tf": "{tf}",
  "bartime": {time},
  "entry": "{close}",
  "sl": "{sl1}",
  "tp1": "{tp1}",
  "order_type": "MARKET"
}
```

**Never paste the private token into GitHub, Render logs, or ChatGPT.**
Replace only the placeholder in the already-private TradingView indicator
configuration using the existing configured webhook token. Keep the current
webhook URL `https://luxalgo-pro-live-trading.onrender.com/webhook` and
**Any alert() function call** alert. Use only actual LuxAlgo signals and
enable their related SL1/TP1 overlays; do not fabricate prices.

The `bartime` is the documented Unix-millisecond indicator timestamp;
the server derives a stable `TVBAR_5m_xxx` / `TVBAR_15m_xxx` identity.
It only qualifies if it is fresh for the signal timeframe. Repeats of the
same completed candle+direction are deduplicated.

`{default}` must resolve to an exact, recognized bullish/bearish
confirmation signal. Exit, stop-hit, take-profit, unsupported, or ambiguous
labels do NOT open positions; the webhook rejects them. The `entry`
placeholder is the TradingView bar CLOSE value, a reference price and
**not** a guaranteed executable fill. If LuxAlgo's intended entry differs
from close, DO NOT authorize trading until the alert sends its true entry.
`{sl1}` and `{tp1}` are taken directly from LuxAlgo; unresolved values
cause INVALID/MISSING status and block execution.

This is a signal transport configuration, not authorization or a change to
LuxAlgo's indicator calculations. Alert message changes must be performed
in the user's own connected TradingView account; ChatGPT's GitHub and Render
connectors cannot edit TradingView alerts.

## Qualification steps, without a trade

1. Keep BOT OFF. In TradingView, edit LuxAlgo's indicator custom message,
   save, and ensure the existing alert is attached to that updated indicator
   instance. Check the webhook URL is `/webhook`, not Testnet.
2. On the next genuine closed-candle bullish/bearish confirmation,
   confirm TradingView's alert logs report webhook delivered. The response
   must be HTTP 200 `signal_received` with `orders_placed: 0`,
   `stable_bar_identity: true`, `order_type_qualified: true`, and
   `level_status: VALID`.
3. Server DB must have a new row in
   `luxalgo_live_signal_handoff` with non-null `source_bar_id`,
   `order_type=MARKET`, valid geometry and deduped event ID.
4. A true replay of the same alert must show duplicate in candle and no
   second execution. Invalid credentials, stale/future bar, non-confirmation
   alerts and missing levels must fail closed.
5. All acceptance reports remain **SIGNAL_ONLY / NO ORDERS** until a
   separately approved bounded real order test passes its complete preflight.

## Expected behavior by input

| Input | Qualification |
| --- | --- |
| Fresh valid `bartime`, bullish/bearish confirmation, MARKET, valid SL1/TP1 | Stored, stable bar identity, no orders |
| Existing legacy alerts missing `bartime` | Stored signal-only, never dispatched |
| Any TP/SL/exit alert | Rejected for entry automation |
| Stale, invalid or future bar timestamp | No stable bar identity, no dispatch |
| Duplicate event with same symbol, side, timeframe, bar | Same event ID and one handoff |
| Missing/malformed SL/TP | INVALID / MISSING; no exchange order |

**Real-money authorization is still NONE, source lock OFF and emergency HALT ON.**
