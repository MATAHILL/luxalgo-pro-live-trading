"""Binance USD-M Futures signed transport, permanently write-locked in this build.

Only GET requests can leave this module. POST/DELETE order functions throw before
any HTTP attempt, irrespective of API credentials, environment variables, or
the UI operator ON/OFF switch. Tests exercise signing and the hard write lock.
"""
import hashlib
import hmac
import time
from urllib.parse import urlencode
import httpx

BASE_URL = "https://fapi.binance.com"
LIVE_ORDER_TRANSMISSION = False
ALLOWED_READS = frozenset({
    "/fapi/v3/balance",
    "/fapi/v3/positionRisk",
    "/fapi/v1/order",
    "/fapi/v1/algoOrder",
    "/fapi/v1/openOrders",
    "/fapi/v1/openAlgoOrders",
    "/fapi/v1/positionSide/dual",
})

class BinanceTransportBlocked(Exception):
    pass

def signed_parameters(params, secret, timestamp=None):
    if not secret:
        raise BinanceTransportBlocked("Missing private signing key")
    values=dict(params)
    if "signature" in values or "timestamp" in values or "recvWindow" in values:
        raise BinanceTransportBlocked("Reserved signing parameter")
    values["timestamp"] = int(time.time()*1000) if timestamp is None else int(timestamp)
    values["recvWindow"] = 5000
    raw=urlencode(values)
    return raw + "&signature=" + hmac.new(secret.encode(),raw.encode(),hashlib.sha256).hexdigest()

class BinanceReadOnly:
    def __init__(self, api_key, api_secret, client=None):
        self.api_key=api_key
        self.api_secret=api_secret
        self._client=client

    def get(self, path, params=None, timestamp=None):
        if path not in ALLOWED_READS:
            raise BinanceTransportBlocked("Endpoint outside read-only allowlist")
        if not self.api_key or not self.api_secret:
            raise BinanceTransportBlocked("Binance credentials missing")
        if params and params.get("symbol") not in (None,"XRPUSDT"):
            raise BinanceTransportBlocked("Only XRPUSDT queries allowed")
        if self._client is None:
            with httpx.Client(timeout=4,follow_redirects=False) as client:
                return self._signed_get(client,path,params,timestamp)
        return self._signed_get(self._client,path,params,timestamp)

    def _signed_get(self,client,path,params,timestamp):
        encoded=signed_parameters(params or {},self.api_secret,timestamp)
        response=client.get(BASE_URL+path+"?"+encoded,headers={"X-MBX-APIKEY":self.api_key})
        response.raise_for_status()
        return response.json()

    def submit_entry(self, payload):
        raise BinanceTransportBlocked("Live Binance entry dispatch locked; no POST executed")

    def submit_protection(self, payload):
        raise BinanceTransportBlocked("Live Binance protection dispatch locked; no POST executed")

    def close_reduce_only(self, payload):
        raise BinanceTransportBlocked("Live Binance emergency close locked; no POST executed")

    def cancel_protection(self, client_algo_id):
        raise BinanceTransportBlocked("Live Binance cancellation locked; no DELETE executed")
