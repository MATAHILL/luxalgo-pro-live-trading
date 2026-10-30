# LUX DIRECT BOT — independent dry-run webhook service

This separate code branch does not touch production LUXALGO PRO.

- LuxAlgo Confirmation signal → TradingView HTTPS webhook → token check → validate BUY/SELL → deduplicate and record.
- No graphics or AI-generated signals.
- LIVE is deliberately hard-locked in the code; **no exchange API connection or real-money trading exists in this deployed build**.
- Allowed events: "Bullish Confirmation Signal" and "Bearish Confirmation Signal" (plus documented shortened forms).
- Default allowed symbol BTCUSDT, with optional BTCUSDT.P ticker mapping.
- Health: `GET /health`
- Authenticated logs: `GET /signals` header `X-Admin-Token: <ADMIN_TOKEN>`
- TradingView webhook: `POST /webhook/<WEBHOOK_SECRET>`

TradingView LuxAlgo alert JSON:
```json
{"alert":"{default}","ticker":"{ticker}","tf":"{tf}","bartime":{time}}
```

Required Render environment: `WEBHOOK_SECRET`, `ADMIN_TOKEN`, `ALLOWED_SYMBOLS=BTCUSDT`.
DB_PATH defaults to /tmp; on a free Render service, data are **ephemeral** and may be lost after restart or idle sleep. Free service is **only for webhook development**; never activate real trading here. For eventual live trading, use persistent, non-sleeping hosting plus a verified Binance Futures connector, exchange-native SL/TP readback, position reconciliation, exact symbol sizing and limits, orphan order cleanup, and explicit order authorization. Do not enable LIVE based on webhook tests alone.

## Dry-run operator checklist

- The private TradingView webhook URL and alert JSON are available inside the terminal after operator-token login.
- LuxAlgo **Signals & Overlays** > **Any alert() function call conditions**: enable **Bullish Confirmation** and **Bearish Confirmation** on a **BINANCE:BTCUSDT.P** 5-minute chart, then create the TradingView alert on **Any alert() function call**; leave other events off. Configure the indicator's own custom any-alert message (not the ordinary TradingView alert field) with the JSON above. `{time}` is a numeric Unix-millisecond value, not TradingView's `{{time}}` ISO timestamp.
- Allowlist is BTCUSDT only, timeframe is 5 only; all other symbols/timeframes are rejected.
- An operator-only **Test signal reception (dry run)** button tests parser/DB/duplicates using a rolled-back transaction. This is NOT an actual TradingView alert and records NO fabricated trades or signals.
- Actual TradingView connectivity is demonstrated only when a genuine confirmation alert arrives and appears in authenticated signal history with status `DRY_RUN`.
- The bot remains a free Render prototype with EPHEMERAL storage and no order sender. No real-money order should be enabled or trading-only API credentials installed until the full live system is independently verified on appropriate persistent hosting.

Reference: https://docs.luxalgo.com/platform/algos/signals-overlays/alerts
