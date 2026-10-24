"""Gate.io one-trade API transport development module.

Authenticated GET requests are available with credentials in environment variables.
ALL exchange writes remain unconditionally blocked. POST/DELETE/PATCH may be
validated via a *local fake transport* only; they never reach an exchange.
This module does not authorize live trading or prove protective order coverage.
"""
from __future__ import annotations
import hashlib
import hmac
import json
import os
import re
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = 'https://api.gateio.ws'
PREFIX = '/api/v4'
ALLOWED_GET = frozenset({
    '/futures/usdt/accounts',
    '/futures/usdt/positions',
    '/futures/usdt/orders',
    '/futures/usdt/price_orders',
})
WRITE_PATHS = frozenset({'/futures/usdt/orders','/futures/usdt/price_orders'})


def sign(method, path, query, body, key, secret, timestamp):
    """Gate API V4 SHA512/HMAC signature for precisely serialized payload."""
    if not path.startswith('/api/v4/'):
        raise ValueError('Invalid signed path')
    if not key or not secret:
        raise ValueError('Missing credentials')
    digest = hashlib.sha512(body.encode('utf-8')).hexdigest()
    message = '\n'.join((method.upper(), path, query, digest, str(timestamp)))
    signature = hmac.new(secret.encode('utf-8'), message.encode('utf-8'), hashlib.sha512).hexdigest()
    return {'KEY':key, 'Timestamp':str(timestamp), 'SIGN':signature,
            'Accept':'application/json', 'Content-Type':'application/json'}


def authenticated_get(endpoint, params=None, timeout=8):
    """GET-only allowlisted read; secrets never returned or printed."""
    if endpoint not in ALLOWED_GET:
        raise PermissionError('GET endpoint not allowlisted')
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ValueError('Invalid query parameters')
    if any(not re.fullmatch(r'[a-zA-Z_]+', str(k)) for k in params):
        raise ValueError('Invalid query key')
    query = urlencode(sorted(params.items()))
    key = os.getenv('GATE_API_KEY','').strip()
    secret = os.getenv('GATE_API_SECRET','').strip()
    if not key or not secret:
        return {'status':'NOT_VERIFIED', 'reason':'Credentials unavailable', 'safe_to_trade':False}
    headers = sign('GET', PREFIX + endpoint, query, '', key, secret, int(time.time()))
    url = ROOT + PREFIX + endpoint + ('?' + query if query else '')
    req = Request(url, headers=headers, method='GET')
    with urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    return {'status':'READ_ONLY_RESPONSE', 'data':data, 'safe_to_trade':False}


def exchange_write(method, endpoint, payload):
    """Explicit fail-closed production guard: no exchange writes by this module."""
    raise PermissionError('LIVE EXCHANGE WRITES DISABLED: protective lifecycle not verified')


def mock_write(method, endpoint, payload, fake_transport):
    """Exercise serialization/order acknowledgments against injected fake only."""
    if method not in ('POST', 'DELETE') or endpoint not in WRITE_PATHS:
        raise ValueError('Unsupported simulated write')
    if not isinstance(payload, dict) or not isinstance(fake_transport, FakeTransport):
        raise TypeError('Local fake transport and object payload required')
    data = json.dumps(payload, sort_keys=True, separators=(',',':'))
    return fake_transport.send(method, endpoint, data)


class FakeTransport:
    def __init__(self):
        self.calls = []
    def send(self, method, endpoint, encoded):
        self.calls.append((method, endpoint, json.loads(encoded)))
        return {'status':'SIMULATED_ACK','fake_order_id':'SIM-001',
                'exchange_verified':False, 'safe_to_trade':False}


def self_test():
    s = sign('GET','/api/v4/futures/usdt/positions','','','key','secret',123)
    assert len(s['SIGN']) == 128
    fake = FakeTransport()
    answer = mock_write('POST','/futures/usdt/orders',{'contract':'BTC_USDT','size':1},fake)
    assert answer['status']=='SIMULATED_ACK' and len(fake.calls)==1
    for m in ('POST','DELETE'):
        try:
            exchange_write(m,'/futures/usdt/orders',{'size':1})
        except PermissionError:
            pass
        else:
            raise AssertionError('Unexpected exchange write permitted')
    try:
        authenticated_get('/futures/usdt/orders/123')
    except PermissionError:
        pass
    else:
        raise AssertionError('Unallowlisted endpoint was permitted')
    return {'signature_test':'PASSED','mock_order_ack':'PASSED',
            'actual_exchange_write':'BLOCKED','protective_order_verification':'NOT_IMPLEMENTED',
            'live_trading':False}


if __name__=='__main__':
    print(json.dumps(self_test(),indent=2))
