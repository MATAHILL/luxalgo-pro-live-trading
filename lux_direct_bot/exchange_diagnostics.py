"""Read-only operator setup and Binance public diagnostics.

No trading or private account endpoints. Never put sensitive configuration in
rendered HTML or unauthenticated HTTP responses.
"""
import httpx

BASE = "https://fapi.binance.com"

def get_binance_public_status(client_factory=httpx.Client):
    """Fetch public USD-M Futures metadata only; fail closed on network problems."""
    try:
        with client_factory(timeout=3.0, follow_redirects=False) as client:
            timer = client.get(BASE + "/fapi/v1/time")
            timer.raise_for_status()
            clock = timer.json()
            if not isinstance(clock.get("serverTime"), int):
                raise ValueError("Missing serverTime")
            meta = client.get(BASE + "/fapi/v1/exchangeInfo", params={"symbol": "XRPUSDT"})
            meta.raise_for_status()
            data = meta.json()
            matching = [s for s in data.get("symbols", []) if s.get("symbol") == "XRPUSDT"]
            if not matching:
                return {"public_api": "REACHABLE", "xrp_usdt": "NOT_LISTED",
                        "api_credentials": "NOT_CONNECTED", "orders_enabled": False}
            row = matching[0]
            filter_summary = {}
            for f in row.get("filters", []):
                if f.get("filterType") in ("LOT_SIZE", "MARKET_LOT_SIZE", "MIN_NOTIONAL"):
                    filter_summary[f["filterType"]] = {
                        k: v for k, v in f.items()
                        if k in ("minQty", "maxQty", "stepSize", "notional")
                    }
            return {"public_api": "REACHABLE", "xrp_usdt": row.get("status", "UNKNOWN"),
                    "filters": filter_summary, "api_credentials": "NOT_CONNECTED",
                    "orders_enabled": False}
    except httpx.HTTPStatusError as e:
        return {"public_api": "UNAVAILABLE", "xrp_usdt": "UNVERIFIED",
                "api_credentials": "NOT_CHECKED", "orders_enabled": False,
                "reason": "BINANCE_PUBLIC_HTTP_ERROR",
                "http_status": e.response.status_code}
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as e:
        return {"public_api": "UNAVAILABLE", "xrp_usdt": "UNVERIFIED",
                "api_credentials": "NOT_CHECKED", "orders_enabled": False,
                "reason": type(e).__name__}
