# LUXALGO PRO — ONE-ORDER LIVE ACCEPTANCE REQUEST

**PROPOSED / NOT AUTHORIZED / SOURCE-LOCKED.**

## Verified source and evidence
- Exchange: Gate.io LIVE USDT-M Futures, verified in authenticated GET account and public contract/ticker endpoints.
- Read-only snapshot timestamp: 2026-10-28T13:23:10.404131Z (2026-10-28 18:53:10 IST).
- Account equity: **70.00000001 USDT**; available: **70.00000001 USDT**.
- Account position mode: **dual (hedge)**; the acceptance order must be LONG ONLY.
- Contract: **BTC_USDT**, minimum **1 contract**, multiplier **0.0001 BTC per contract**.
- Mark reference: **80,659.1 USDT/BTC**, minimum-lot notional **8.06591 USDT**.
- Reference margin at 5x: **1.613182 USDT**; taker fee rate **0.075%**.
- Earlier authenticated readback: 0 open positions, 0 working orders, 0 open triggers; reconfirm immediately before authorization execution.
- Account-wide caps: 0.5% per trade = **0.35000000 USDT modeled**, 2.5% aggregate = **1.75000000 USDT modeled**; max 5 simultaneously tracked distinct positions; max 5x leverage.
- BTC contract not reported as delisting at snapshot.

## Precise authorization terms for separate owner approval
- **One-time test only:** BTC_USDT **LONG**, 1 contract; no automatic second entry, hedged opposite trade or another contract.
- **Test window:** 2026-10-29 10:00 to 10:15 IST (2026-10-29 04:30 to 04:45 UTC). Outside window: REJECT.
- **Maximum live notional:** 8.30 USDT at execution; if one-lot current notional exceeds this, REJECT.
- **Maximum margin:** 1.70 USDT (subject to authenticated margin-mode, margin/fees/leverage verification); if one-lot requirement exceeds this, REJECT.
- **Maximum modeled entry-to-stop loss (including modeled two taker fees and 0.10% entry + 0.10% exit slippage):** 0.10 USDT; never exceed the 0.5%-equity risk budget. This is not a guaranteed maximum realized loss.
- **Maximum leverage:** 5x; reduce where necessary, but never increase.
- **Maximum entry slippage setting:** 0.10%; no retry after unknown POST ACK.
- **Side and mode:** LONG only on Gate.io LIVE dual mode; no unverified single/dual_plus conversion.
- **Native SL + TP:** one actual open exchange stop order ID and a separate actual open target order ID, with matching contract/side/trigger geometry, both confirmed through authenticated GET before declaring PROTECTED.
- **Emergency handling:** require *additional explicit permission in the same signed authorization* to make at most one exchange-native reduce-only close when a filled LONG position is positively verified but native protections cannot be established; reject if identity/side/size unknown. Confirm flat and zero orphan protective triggers afterward. Unknown result means HALT + human exchange intervention.
- **Bot mode during test:** permanently HALTED for unattended operation; allow only the individually authorized acceptance transaction within the bounded window, through a separately reviewed/scope-limited execution path. Do not enable unrestricted BOT ON.

## Reference illustration only (not executable prices)
Mark 80,659.1, illustrative 0.5% stop 80,255.8 and 1.0% target 81,465.7.
For 1 contract: modeled stop loss 0.04033000 USDT + two taker fees
0.01209887 + modeled slippage 0.01613182 = **0.06856069 USDT**.
These illustration prices **must not replace validated LuxAlgo alert levels**.
Re-evaluate against the freshest mark, lot, available margin, existing exposure,
actual signed LuxAlgo entry/SL/TP and minimum tick/trigger rules immediately
before submitting. If any constraint fails, NO ORDER.

## Mandatory go/no-go before presenting an executable approval
- Exact production commit and successful Github Actions + Render test evidence at a matching SHA.
- Authenticated wallet and all live positions/working orders/price-orders readback,
  contract minimum, fee and margin-mode confirmed fresh.
- Real TradingView LuxAlgo alert with valid stable `tv_bar_id`, signature/token,
  deduplicated durable receipt, 5m or 15m completed-candle values, and validated MARKET declaration.
- No unknown exposures, pending historical order ACKs, database or market-data outage,
  orphan triggers, unsupported hedge-mode semantics, unreliable margin leverage.
- Detailed exchange native trigger geometry readback simulation passes.
- User explicitly approves each individual exposure cap, direction, time window,
  native SL/TP placement and emergency reduce-only authority.
- A limited test authorization is separate from unrestricted production activation.

## Execution evidence required if and only if the exact test is approved
Unique event ID and exchange client_text, actual signed order ID, filled size,
entry fee, fill average, verified account position, TWO independent native
protective-order IDs, trigger prices/directions and exchange status, complete
restart reconciliation, permitted emergency close result and no orphan orders,
realized/unrealized P&L and final equity. Redact keys and sensitive data.
Any missing evidence => FAIL / HALT / NOT CERTIFIED.

**CURRENT AUTHORIZATION: NONE.** Source-level writing locks = OFF; DB emergency
HALT = ON; operator BOT = OFF. Any approval text must be a new separate explicit
statement from the user of the whole bounded test. Never infer it from a general
instruction to finish development, earlier preparations, or approval of code.
