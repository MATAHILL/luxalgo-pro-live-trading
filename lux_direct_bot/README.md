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
{"alert":"{default}","ticker":"{ticker}","tf":"{tf}","bartime":"{time}"}
```

Required Render environment: `WEBHOOK_SECRET`, `ADMIN_TOKEN`, `ALLOWED_SYMBOLS=BTCUSDT`.
DB_PATH defaults to /tmp; on a free Render service, data are **ephemeral** and may be lost after restart or idle sleep. Free service is **only for webhook development**; never activate real trading here. For eventual live trading, use persistent, non-sleeping hosting plus a verified Binance Futures connector, exchange-native SL/TP readback, position reconciliation, exact symbol sizing and limits, orphan order cleanup, and explicit order authorization. Do not enable LIVE based on webhook tests alone.
