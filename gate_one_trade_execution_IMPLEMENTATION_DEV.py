"""Gate.io USDT Futures one-trade execution transport (DEVELOPMENT, LOCKED).

Implementation milestone: authenticated GET/POST transport, deterministic client order ID,
attached TP/SL order entry payload, exchange acknowledgment evaluation, and authenticated
read-only status lookups. It is NOT connected to the production webhook.

Live writes are intentionally hard blocked pending on-exchange attached-TP/SL verification,
hedge/one-way account-mode validation, atomic single-trade ownership in PostgreSQL,
position reconciliation, and safe emergency-close behavior.
Do NOT enable real money trading by simply editing a flag.
"""
from __future__ import annotations
import hashlib, hmac, json, os, re, time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlencode

ROOT = 'https://api.gateio.ws/api/v4'
WRITES_VERIFIED = False  # intentionally hard-locked; never read enable flag from environment

class GateExecutionBlocked(RuntimeError):
    pass

class GateTransportError(RuntimeError):
    pass

def positive(value, label):
    try:
        if isinstance(value, bool): raise ValueError()
        x = Decimal(str(value))
        if not x.is_finite() or x <= 0: raise ValueError()
        return x
    except (InvalidOperation, TypeError, ValueError):
        raise GateExecutionBlocked(f'Invalid {label}') from None

@dataclass(frozen=True)
class ProtectedIntent:
    event_id: str
    contract: str
    side: str
    contracts: int
    entry: str
    sl: str
    tp: str


def prepare_intent(event_id, contract, side, contracts, entry, sl, tp):
    if not isinstance(event_id, str) or not re.fullmatch(r'[a-zA-Z0-9_.:-]{4,100}', event_id):
        raise GateExecutionBlocked('Invalid event ID')
    if not isinstance(contract, str) or not re.fullmatch(r'[A-Z0-9]{2,25}_USDT', contract):
        raise GateExecutionBlocked('Invalid contract')
    if side not in ('LONG','SHORT') or type(contracts) is not int or contracts < 1:
        raise GateExecutionBlocked('Invalid side or contract quantity')
    ep, stop, take = positive(entry,'entry'),positive(sl,'SL'),positive(tp,'TP')
    if not ((side=='LONG' and stop<ep<take) or (side=='SHORT' and take<ep<stop)):
        raise GateExecutionBlocked('Invalid protective levels')
    return ProtectedIntent(event_id,contract,side,contracts,str(ep),str(stop),str(take))

def client_id(event_id):
    if not isinstance(event_id,str) or not re.fullmatch(r'[a-zA-Z0-9_.:-]{4,100}',event_id):
        raise GateExecutionBlocked('Invalid event ID')
    return 't-lux-' + hashlib.sha256(event_id.encode()).hexdigest()[:24]

def entry_body(intent):
    """Proposed Gate.io one-way-mode futures market entry with attached TP/SL.

    API documentation lists attached tpsl_* fields, but account-mode compatibility
    and actual accepted stop protection must be verified before activation.
    """
    if not isinstance(intent,ProtectedIntent): raise GateExecutionBlocked('Invalid intent')
    return {'contract':intent.contract, 'size':intent.contracts if intent.side=='LONG' else -intent.contracts,
            'price':'0','tif':'ioc','text':client_id(intent.event_id),
            'tpsl_tp_trigger_price':intent.tp,'tpsl_sl_trigger_price':intent.sl}

def sign(method, endpoint, query, body_text, key, secret, timestamp):
    if method not in ('GET','POST') or not endpoint.startswith('/api/v4/futures/usdt/'):
        raise GateExecutionBlocked('Unexpected endpoint')
    if not key or not secret: raise GateExecutionBlocked('Credentials unavailable')
    payload_hash=hashlib.sha512(body_text.encode()).hexdigest()
    message='\n'.join((method, endpoint, query, payload_hash, str(timestamp)))
    return hmac.new(secret.encode(),message.encode(),hashlib.sha512).hexdigest()

def _signed_request(method, route, params=None, body=None, *, get_only=True):
    """Authenticated transport. In this version any exchange write is ALWAYS blocked."""
    if not re.fullmatch(r'/futures/usdt/(?:positions|orders|price_orders|contracts)(?:/[a-zA-Z0-9_-]+)?',route):
        raise GateExecutionBlocked('Unapproved Gate.io route')
    if method != 'GET' or not get_only:
        if not WRITES_VERIFIED:
            raise GateExecutionBlocked('Exchange account changes are not verified or enabled')
        # No functional write dispatch in this file: cannot be enabled by editing a flag.
        raise GateExecutionBlocked('Safe production POST transaction coordinator not implemented')
    key=os.getenv('GATE_API_KEY','').strip()
    secret=os.getenv('GATE_API_SECRET','').strip()
    if not key or not secret: raise GateExecutionBlocked('Gate.io read-only credentials missing')
    query=urlencode(params or {})
    path='/api/v4'+route
    timestamp=int(time.time())
    headers={'Accept':'application/json','KEY':key,'Timestamp':str(timestamp),
             'SIGN':sign('GET',path,query,'',key,secret,timestamp)}
    url=ROOT+route+('?' + query if query else '')
    try:
        with urlopen(Request(url,headers=headers,method='GET'),timeout=8) as response:
            if response.status != 200: raise GateTransportError('Non-success Gate response')
            return json.load(response)
    except (HTTPError,URLError,TimeoutError,ValueError) as exc:
        # Avoid emitting sensitive error bodies or headers
        raise GateTransportError('Gate.io authenticated read failed: '+type(exc).__name__) from None

def account_snapshot():
    """Authenticated GET: no orders placed, state may change between calls."""
    positions=_signed_request('GET','/futures/usdt/positions')
    open_orders=_signed_request('GET','/futures/usdt/orders',{'status':'open'})
    stops=_signed_request('GET','/futures/usdt/price_orders',{'status':'open'})
    if not all(isinstance(x,list) for x in (positions,open_orders,stops)):
        raise GateTransportError('Unexpected exchange response structure')
    return {'positions':positions,'open_orders':open_orders,'open_trigger_orders':stops,
            'snapshot_atomic':False,'live_trading':False}

def review_order_ack(ack, expected_event_id):
    """An order ACK NEVER establishes the presence of an active protected position."""
    if not isinstance(ack,dict): return {'state':'REVIEW_REQUIRED','reason':'Unrecognized response'}
    expected=client_id(expected_event_id)
    if ack.get('text')!=expected or not ack.get('id'):
        return {'state':'REVIEW_REQUIRED','reason':'Mismatched order acknowledgement'}
    return {'state':'ACK_ONLY_PROTECTION_UNVERIFIED','order_id':str(ack['id']),
            'reason':'Must confirm filled size, position, active SL and active TP on exchange'}

def submit_live_order(*_args, **_kwargs):
    raise GateExecutionBlocked('Blocked: real-order lifecycle and protection not verified')

def verify_exchange_protection(*_args, **_kwargs):
    raise GateExecutionBlocked('Blocked: authenticated exact SL/TP coverage checker not integrated')

def emergency_close(*_args, **_kwargs):
    raise GateExecutionBlocked('Blocked: mode-specific safe emergency close not implemented')

def self_test():
    long=prepare_intent('SIGNAL-0001','BTC_USDT','LONG',2,'80000','79700','80600')
    short=prepare_intent('SIGNAL-0002','BTC_USDT','SHORT',2,'80000','80300','79400')
    assert entry_body(long)['size']==2 and entry_body(short)['size']==-2
    assert entry_body(long)['tpsl_sl_trigger_price']=='79700'
    assert client_id('SIGNAL-0001')==client_id('SIGNAL-0001')
    assert len(sign('POST','/api/v4/futures/usdt/orders','',json.dumps(entry_body(long)), 'test','secret',1))==128
    assert review_order_ack({'id':123,'text':client_id(long.event_id)},long.event_id)['state']=='ACK_ONLY_PROTECTION_UNVERIFIED'
    assert review_order_ack({'id':123,'text':'t-wrong'},long.event_id)['state']=='REVIEW_REQUIRED'
    for op in (submit_live_order, verify_exchange_protection, emergency_close):
        try: op()
        except GateExecutionBlocked: pass
        else: raise AssertionError('Write unexpectedly enabled')
    try: _signed_request('POST','/futures/usdt/orders',body=entry_body(long),get_only=False)
    except GateExecutionBlocked: pass
    else: raise AssertionError('POST transport unexpectedly enabled')
    return {'entry_payloads':'PASSED','client_order_id':'PASSED','signing':'PASSED',
            'unknown_ack_blocks':'PASSED','authenticated_GET':'AVAILABLE_REQUIRES_ENV',
            'real_order_POST':'BLOCKED','exit_protection':'NOT_VERIFIED','production_connected':False}

if __name__=='__main__':
    print(json.dumps(self_test(),indent=2))
