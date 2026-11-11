"""LUXALGO PRO: isolated Gate.io LIVE USDT-M execution foundation.

Real-money exchange writes are gated by an EXPLICIT, DEFAULT-LOCKED release
switch (LUXALGO_EXCHANGE_WRITE_RELEASE). It is read from the environment at
import time and defaults to False, so an unconfigured deployment can never
send a real order. Setting it to "true" is a deliberate operator action and
is still combined with the environment permission flags, the persistent
database HALT and the operator/certification gate before any POST is sent.
All read operations remain authenticated GET or pure planning. No Testnet.
"""
from __future__ import annotations
import hashlib
import hmac
import json
import os
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from live_risk_policy import per_entry_risk

HOST = "https://api.gateio.ws"
PREFIX = "/api/v4/futures/usdt"

def _env_release(name):
    """Explicit, default-locked release switch.

    Returns True only when the named environment variable is exactly "true"
    (case-insensitive). Absent, empty or any other value keeps the gate locked,
    so a missing configuration fails closed rather than open.
    """
    return os.getenv(name, "false").strip().lower() == "true"

# A code-reviewed release, in addition to explicit permission, is required.
# Defaults to False; an operator must deliberately set
# LUXALGO_EXCHANGE_WRITE_RELEASE=true in the deployment environment.
EXCHANGE_WRITE_RELEASE = _env_release("LUXALGO_EXCHANGE_WRITE_RELEASE")
MAX_POSITIONS = 5
MAX_AGGREGATE_RISK = Decimal("0.025")
MAX_LEVERAGE = Decimal("5")
# Owner's requested maximum trade exposure, independently of the risk model.
# Caps only; they never force an order or override the 0.5% equity risk limit.
MAX_POSITION_NOTIONAL_USDT = Decimal("100")
MAX_POSITION_INITIAL_MARGIN_USDT = Decimal("60")
MAX_SIGNAL_AGE_SECONDS = 60
CONTRACT_RE = re.compile(r"^[A-Z0-9]{2,28}_USDT$")
ORDER_RE = re.compile(r"^[1-9][0-9]{0,19}$")
READ_PATHS = {PREFIX + suffix for suffix in
              ("/accounts", "/positions", "/orders", "/price_orders", "/contracts")}
WRITE_PATHS = {PREFIX + "/orders", PREFIX + "/price_orders"}
MIGRATION_SQL = """
DO $luxalgo_schema$
BEGIN
-- Dashboard reconciliation keeps its transaction open during exchange GETs.
-- Even ALTER TABLE ... IF NOT EXISTS takes ACCESS EXCLUSIVE until commit.
-- Check the committed catalog first, without caching transaction-local state,
-- so a normal readback never queues behind a redundant schema migration.
IF to_regclass('luxalgo_prod_execution_control') IS NOT NULL
   AND to_regclass('idx_prod_ledger_state') IS NOT NULL
   AND EXISTS (
       SELECT 1 FROM pg_attribute
       WHERE attrelid = to_regclass('luxalgo_prod_execution_ledger')
         AND attname = 'position_mode' AND NOT attisdropped
   ) THEN
    RETURN;
END IF;
CREATE TABLE IF NOT EXISTS luxalgo_prod_execution_ledger (
 event_id TEXT PRIMARY KEY,
 client_text TEXT NOT NULL UNIQUE,
 contract TEXT NOT NULL,
 side TEXT NOT NULL CHECK(side IN ('LONG','SHORT')),
 timeframe TEXT NOT NULL CHECK(timeframe IN ('5m','15m')),
 position_mode TEXT NOT NULL DEFAULT 'UNVERIFIED',
 state TEXT NOT NULL CHECK(state IN
   ('RESERVED','SENDING','ACK_UNKNOWN','ENTRY_CONFIRMED',
    'PROTECTION_PENDING','PROTECTED','HALTED','CLOSED_VERIFIED')),
 risk_usdt NUMERIC NOT NULL CHECK(risk_usdt>0),
 entry_price NUMERIC NOT NULL,
 stop_loss NUMERIC NOT NULL,
 take_profit NUMERIC NOT NULL,
 contracts NUMERIC NOT NULL CHECK(contracts>0),
 entry_order_id TEXT,
 stop_order_id TEXT,
 take_order_id TEXT,
 detail JSONB NOT NULL DEFAULT '{}'::jsonb,
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE luxalgo_prod_execution_ledger
ADD COLUMN IF NOT EXISTS position_mode TEXT NOT NULL DEFAULT 'UNVERIFIED';
CREATE INDEX IF NOT EXISTS idx_prod_ledger_state
ON luxalgo_prod_execution_ledger(state,updated_at);
CREATE TABLE IF NOT EXISTS luxalgo_prod_execution_control (
 id SMALLINT PRIMARY KEY CHECK (id=1),
 halted BOOLEAN NOT NULL DEFAULT TRUE,
 reason TEXT NOT NULL DEFAULT 'NOT_VERIFIED_FOR_REAL_MONEY',
 updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO luxalgo_prod_execution_control(id,halted,reason)
VALUES (1,TRUE,'NOT_VERIFIED_FOR_REAL_MONEY') ON CONFLICT(id) DO NOTHING;
END;
$luxalgo_schema$;
"""
class LiveSafetyError(RuntimeError):
    pass

class AckUnknown(LiveSafetyError):
    pass

def dec(value, label="number"):
    if isinstance(value, bool) or value is None:
        raise LiveSafetyError(label + " missing")
    try:
        v = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise LiveSafetyError(label + " invalid") from exc
    if not v.is_finite() or v <= 0:
        raise LiveSafetyError(label + " must be positive")
    return v

def client_text(event_id):
    if not isinstance(event_id, str) or not re.fullmatch(r"[a-zA-Z0-9_.:-]{8,128}", event_id):
        raise LiveSafetyError("Invalid stable event identifier")
    return "t-lxp-" + hashlib.sha256(event_id.encode()).hexdigest()[:20]

def price_geometry(side, entry, sl, tp):
    e,s,t = dec(entry,"entry"),dec(sl,"stop"),dec(tp,"target")
    if side not in ("LONG","SHORT"):
        raise LiveSafetyError("Unknown direction")
    if not ((s < e < t) if side=="LONG" else (t < e < s)):
        raise LiveSafetyError("Invalid protective geometry")
    return e,s,t


def verified_account_equity(account):
    """Classic Gate USDT-M equity = total cash balance + unrealized P&L.

    Prevents a misleading risk limit based on total alone when an existing
    position has negative unrealized P&L. Unified account modes fail closed.
    """
    if not isinstance(account,dict) or account.get("currency")!="USDT":
        raise LiveSafetyError("USDT live account readback not verified")
    if account.get("margin_mode",0) not in (0,"0",None):
        raise LiveSafetyError("Unified Futures equity mapping unsupported")
    def signed_amount(field):
        value=account.get(field)
        if value is None or isinstance(value,bool):
            raise LiveSafetyError("Account field missing: "+field)
        try:
            x=Decimal(str(value))
        except (InvalidOperation,TypeError,ValueError) as exc:
            raise LiveSafetyError("Invalid account amount") from exc
        if not x.is_finite():
            raise LiveSafetyError("Non-finite account balance")
        return x
    equity=signed_amount("total")+signed_amount("unrealised_pnl")
    available=signed_amount("available")
    if equity<=0 or available<0:
        raise LiveSafetyError("No positive equity or usable available margin")
    return equity,available

def active_positions(positions):
    if not isinstance(positions,list):
        raise LiveSafetyError("Position inventory unavailable")
    out=[]
    for p in positions:
        if not isinstance(p,dict) or not CONTRACT_RE.fullmatch(str(p.get("contract",""))):
            raise LiveSafetyError("Unexpected live position")
        # Missing size is UNKNOWN exposure, never proof of a flat account.
        raw_size=p.get("size")
        if raw_size is None or isinstance(raw_size,bool) or str(raw_size).strip()=="":
            raise LiveSafetyError("Missing or invalid live position size")
        try:
            size=Decimal(str(raw_size))
        except (InvalidOperation,TypeError,ValueError) as exc:
            raise LiveSafetyError("Invalid live position size") from exc
        if not size.is_finite():
            raise LiveSafetyError("Non-finite live position size")
        if size: out.append(p)
    return out

def build_plan(signal, metadata, equity, available, positions, reserved_risk=0, now=None):
    """Contract-count plan with configured per-entry / preserved 2.5% aggregate cap.

    No paper base-unit sizing is ever converted directly into a Gate order.
    Fail closed if contract units, increments, leverage or account state unclear.
    """
    now = int(time.time()) if now is None else int(now)
    if not isinstance(signal,dict) or not isinstance(metadata,dict):
        raise LiveSafetyError("Signal or contract metadata missing")
    event = signal.get("event_id")
    text = client_text(event)
    side=signal.get("side")
    e,s,t=price_geometry(side,signal.get("entry"),signal.get("sl"),signal.get("tp"))
    tf=signal.get("timeframe")
    if tf not in ("5m","15m") or signal.get("levels_status")!="VALID":
        raise LiveSafetyError("Unvalidated LuxAlgo candle levels/timeframe")
    age=now-int(signal.get("received_epoch",0))
    if not 0<=age<=MAX_SIGNAL_AGE_SECONDS:
        raise LiveSafetyError("Signal delayed or future dated")
    contract=signal.get("contract")
    if not isinstance(contract,str) or not CONTRACT_RE.fullmatch(contract):
        raise LiveSafetyError("Invalid Futures contract")
    if metadata.get("name")!=contract or metadata.get("in_delisting") is True:
        raise LiveSafetyError("Unverified or delisting contract")
    position_mode=signal.get("position_mode")
    if position_mode not in ("single","dual"):
        raise LiveSafetyError("Live exchange position mode must be verified; split mode unsupported")
    lev=dec(signal.get("leverage",MAX_LEVERAGE),"leverage")
    if lev>MAX_LEVERAGE:
        raise LiveSafetyError("Leverage exceeds ceiling")
    actual=active_positions(positions)
    if len(actual)>=MAX_POSITIONS:
        raise LiveSafetyError("Maximum five existing positions")
    if any(p["contract"]==contract for p in actual):
        raise LiveSafetyError("Duplicate symbol or conflicting position")
    eq,av=dec(equity,"equity"),dec(available,"available")
    multiplier=dec(metadata.get("quanto_multiplier"),"contract multiplier")
    order_size_min=dec(metadata.get("order_size_min"),"minimum contract count")
    order_size_max=dec(metadata.get("order_size_max"),"maximum contract count")
    # Some Gate.io Futures contracts (including XRP_USDT) publish a fractional
    # minimum. This execution sender supports WHOLE contracts only. Accept
    # fractional exchange minimums solely when one whole contract is an exact
    # multiple of that minimum; never round a requested fractional order up.
    # Floor a fractional maximum so the integer sender cannot exceed it.
    if order_size_max < 1 or Decimal("1") % order_size_min != 0:
        raise LiveSafetyError("Whole-contract order incompatible with exchange lot limits")
    whole_order_size_max = order_size_max.to_integral_value(rounding=ROUND_FLOOR)
    reserve=Decimal(str(reserved_risk))
    if not reserve.is_finite() or reserve<0:
        raise LiveSafetyError("Invalid reserved risk")
    budget=min(eq*per_entry_risk(),eq*MAX_AGGREGATE_RISK-reserve)
    if budget<=0:
        raise LiveSafetyError("Aggregate risk exhausted")
    # Model stop-distance loss PLUS two taker fees and two 0.1% slippage
    # allowances per contract. This remains a modeled bound: a gap or
    # liquidation can exceed it. Never treat this as guaranteed max loss.
    fee=dec(metadata.get("taker_fee_rate"),"exchange taker fee rate")
    if fee>Decimal("0.01"):
        raise LiveSafetyError("Exchange taker fee exceeds modeled ceiling")
    slip=dec(signal.get("max_entry_slippage_ratio","0.001"),"allowed slippage")
    if slip>Decimal("0.001"):
        raise LiveSafetyError("Slippage exceeds 0.10% acceptance ceiling")
    unit_stop_loss=abs(e-s)*multiplier
    unit_fee_slip=(e*multiplier)*(2*fee+2*slip)
    unit_loss=unit_stop_loss+unit_fee_slip
    units_by_risk=(budget/unit_loss).to_integral_value(rounding=ROUND_FLOOR)
    unit_margin=(e*multiplier)*(1+slip)*(Decimal(1)/lev+fee)
    units_by_margin=(av/unit_margin).to_integral_value(rounding=ROUND_FLOOR)
    # A loose stop cannot silently turn a proposed 100-USDT order into an
    # arbitrarily larger exposure when the account balance is high.
    unit_notional=e*multiplier*(1+slip)
    units_by_notional=(MAX_POSITION_NOTIONAL_USDT/unit_notional).to_integral_value(rounding=ROUND_FLOOR)
    units_by_margin_cap=(MAX_POSITION_INITIAL_MARGIN_USDT/unit_margin).to_integral_value(rounding=ROUND_FLOOR)
    count=min(units_by_risk,units_by_margin,units_by_notional,
              units_by_margin_cap,whole_order_size_max)
    if count<order_size_min or count<1:
        raise LiveSafetyError("Insufficient margin/risk for exchange minimum lot")
    risk=count*unit_loss
    if risk>eq*per_entry_risk() or risk+reserve>eq*MAX_AGGREGATE_RISK:
        raise LiveSafetyError("Risk cap crossed")
    if count*unit_notional>MAX_POSITION_NOTIONAL_USDT or count*unit_margin>MAX_POSITION_INITIAL_MARGIN_USDT:
        raise LiveSafetyError("Maximum authorized notional or margin exceeded")
    return dict(event_id=event,client_text=text,contract=contract,side=side,timeframe=tf,
                received_epoch=int(signal['received_epoch']),
                contracts=int(count),entry=str(e),sl=str(s),tp=str(t),
                estimated_risk_usdt=str(risk),risk_cap_usdt=str(budget),
                risk_includes_modeled_taker_fees_and_slippage=True,
                modeled_stop_risk_usdt=str(count*unit_stop_loss),
                modeled_fee_slippage_usdt=str(count*unit_fee_slip),
                max_entry_slippage_ratio=str(slip),
                maximum_notional_usdt=str(MAX_POSITION_NOTIONAL_USDT),
                maximum_initial_margin_usdt=str(MAX_POSITION_INITIAL_MARGIN_USDT),
                leverage=str(lev),position_mode=position_mode,verified_live_order=False)


def verify_execution_preflight(plan, metadata, snapshot, now=None):
    """Verify actual exchange settings; never change leverage or round signals."""
    now=int(time.time()) if now is None else int(now)
    try:
        age=now-int(plan['received_epoch'])
    except (KeyError,TypeError,ValueError,OverflowError) as exc:
        raise LiveSafetyError('Signal timestamp unavailable for execution') from exc
    if not 0<=age<=MAX_SIGNAL_AGE_SECONDS:
        raise LiveSafetyError('Signal expired before dispatch')
    if (not isinstance(metadata,dict) or metadata.get('name')!=plan['contract'] or
            metadata.get('in_delisting') is not False or metadata.get('status')!='trading'):
        raise LiveSafetyError('Contract not independently verified as trading')
    quantity=dec(plan.get('contracts'),'contracts')
    minimum=dec(metadata.get('order_size_min'),'minimum lot')
    maximum=dec(metadata.get('order_size_max'),'maximum lot')
    raw_market=metadata.get('market_order_size_max')
    if raw_market is None or isinstance(raw_market,bool):
        raise LiveSafetyError('Market order limit missing')
    try:market_max=Decimal(str(raw_market))
    except (InvalidOperation,ValueError,TypeError) as exc:
        raise LiveSafetyError('Market order limit invalid') from exc
    if not market_max.is_finite() or market_max<0:
        raise LiveSafetyError('Market order limit invalid')
    market_max=maximum if market_max==0 else min(maximum,market_max)
    if quantity!=quantity.to_integral_value() or not minimum<=quantity<=market_max:
        raise LiveSafetyError('Order exceeds verified market lot constraints')
    tick=dec(metadata.get('order_price_round'),'price tick')
    for key in ('sl','tp'):
        if dec(plan.get(key),key)%tick!=0:
            raise LiveSafetyError('Original LuxAlgo protective price is off tick')
    mode=plan.get('position_mode')
    if mode not in ('single','dual') or snapshot['account'].get('position_mode')!=mode:
        raise LiveSafetyError('Account position mode changed or unknown')
    settings=snapshot.get('position_settings')
    expected_mode='single' if mode=='single' else ('dual_long' if plan['side']=='LONG' else 'dual_short')
    if not isinstance(settings,list):
        raise LiveSafetyError('Authenticated contract leverage settings unavailable')
    matching=[p for p in settings if isinstance(p,dict) and
              p.get('contract')==plan['contract'] and p.get('mode')==expected_mode]
    if len(matching)!=1:
        raise LiveSafetyError('Contract leverage configuration missing or ambiguous')
    raw=matching[0].get('leverage')
    if raw is None or isinstance(raw,bool):
        raise LiveSafetyError('Authenticated leverage missing')
    try:isolated=Decimal(str(raw))
    except (InvalidOperation,ValueError,TypeError) as exc:
        raise LiveSafetyError('Authenticated leverage invalid') from exc
    if not isolated.is_finite() or isolated<0:
        raise LiveSafetyError('Authenticated leverage invalid')
    actual=dec(matching[0].get('cross_leverage_limit'),'cross leverage') if isolated==0 else isolated
    planned=dec(plan.get('leverage'),'planned leverage')
    if (actual!=planned or actual>MAX_LEVERAGE or
        not dec(metadata.get('leverage_min'),'minimum leverage')<=actual<=dec(metadata.get('leverage_max'),'maximum leverage')):
        raise LiveSafetyError('Actual leverage does not match verified plan limits')
    entry,sl,tp=price_geometry(plan['side'],plan['entry'],plan['sl'],plan['tp'])
    mark=dec(metadata.get('mark_price'),'live mark')
    price_geometry(plan['side'],mark,sl,tp)
    equity,available=verified_account_equity(snapshot['account'])
    multiplier=dec(metadata.get('quanto_multiplier'),'contract multiplier')
    slip=dec(plan.get('max_entry_slippage_ratio'),'slippage')
    fee=dec(metadata.get('taker_fee_rate'),'taker fee')
    if slip>Decimal('0.001') or fee>Decimal('0.01'):
        raise LiveSafetyError('Fee/slippage ceiling exceeded')
    notional=quantity*multiplier*max(entry,mark)*(1+slip)
    margin_with_fees=notional/planned+notional*fee
    if notional>MAX_POSITION_NOTIONAL_USDT or margin_with_fees>MAX_POSITION_INITIAL_MARGIN_USDT:
        raise LiveSafetyError('Fresh exposure exceeds authorized notional or margin ceiling')
    if margin_with_fees>available:
        raise LiveSafetyError('Insufficient fresh margin including entry fee and slippage')
    reserved=dec(plan.get('estimated_risk_usdt'),'modeled risk')
    fresh_risk=quantity*multiplier*(abs(entry-sl)+max(entry,mark)*(2*fee+2*slip))
    if reserved>equity*per_entry_risk() or fresh_risk>min(reserved,equity*per_entry_risk()):
        raise LiveSafetyError('Fresh equity no longer supports modeled risk')
    return {'verified':True,'orders_sent':0,'actual_leverage':str(actual)}

def build_entry_order(plan):
    if not isinstance(plan,dict) or plan.get("side") not in ("LONG","SHORT"):
        raise LiveSafetyError("Plan missing")
    qty=int(plan["contracts"])*(1 if plan["side"]=="LONG" else -1)
    if qty==0 or not CONTRACT_RE.fullmatch(plan["contract"]):
        raise LiveSafetyError("Invalid live order quantity/contract")
    slip=dec(plan.get("max_entry_slippage_ratio","0.001"),"slippage")
    if slip>Decimal("0.001"):
        raise LiveSafetyError("Entry slippage permission exceeds 0.10%")
    return dict(contract=plan["contract"],size=qty,price="0",tif="ioc",
                text=plan["client_text"],reduce_only=False,
                market_order_slip_ratio=str(slip))

def build_protective_trigger(plan, kind):
    """Full-close exchange-native stop/target with authenticated position mode.

    Gate USDT Futures hedge mode requires auto_size for full close. No default
    position mode: an absent/unknown mode is a hard stop before exchange POST.
    """
    if kind not in ("SL","TP") or plan.get("side") not in ("LONG","SHORT"):
        raise LiveSafetyError("Unsupported protection")
    mode=plan.get("position_mode")
    if mode not in ("single","dual"):
        raise LiveSafetyError("Authenticated account position mode missing or unsupported")
    islong=plan["side"]=="LONG"
    price=plan["sl"] if kind=="SL" else plan["tp"]
    rule=(2 if islong else 1) if kind=="SL" else (1 if islong else 2)
    price_geometry(plan["side"],plan["entry"],plan["sl"],plan["tp"])
    initial={"contract":plan["contract"],"size":0,"price":"0",
             "reduce_only":True,"tif":"ioc"}
    if mode=="single":
        initial["close"]=True
    else:
        initial["auto_size"]="close_long" if islong else "close_short"
    return {"initial":initial,
            "trigger":{"strategy_type":0,"price_type":1,"price":str(price),
                       "rule":rule,"expiration":86400},
            "order_type":"close-long-position" if islong else "close-short-position"}

def _verified_full_close_initial(initial, mode, expected):
    """Prove full close from Gate native trigger readback, fail closed.

    Gate documents auto_size as write-only. A missing echo of auto_size in
    dual mode is not evidence of a missing full close when the read-only
    is_close/is_reduce_only flags confirm it and order_type matches.
    If auto_size IS returned, it must match exactly; never accept the
    opposite side. One-way size=0 requires close/is_close, not merely
    reduce_only. No order submission is performed by this function.
    """
    if not isinstance(initial, dict):
        return False
    if not (initial.get("reduce_only") is True or
            initial.get("is_reduce_only") is True):
        return False
    reported_auto_size = initial.get("auto_size")
    if mode == "single":
        return (reported_auto_size in (None, "") and
                (initial.get("close") is True or initial.get("is_close") is True))
    if mode == "dual":
        expected_auto_size = expected["initial"]["auto_size"]
        if reported_auto_size not in (None, ""):
            return reported_auto_size == expected_auto_size
        return (initial.get("is_close") is True and
                initial.get("is_reduce_only") is True)
    return False


def evaluate_protection(plan, price_orders):
    """Strict readback: identifies two independent live, correct-side trigger orders.

    Never infer protection from local IDs or journal state alone.
    """
    if not isinstance(price_orders,list):
        return {"verified":False,"reason":"TRIGGER_READBACK_FAILED"}
    matched={}
    for kind in ("SL","TP"):
        expected=build_protective_trigger(plan,kind)
        candidates=[]
        for o in price_orders:
            if not isinstance(o,dict) or o.get("status")!="open": continue
            x=o.get("initial") or {}; z=o.get("trigger") or {}
            try:
                if (x.get("contract")==plan["contract"] and
                    str(o.get("order_type"))==expected["order_type"] and
                    int(z.get("rule",-1))==expected["trigger"]["rule"] and
                    int(z.get("price_type",-1))==1 and
                    Decimal(str(z.get("price")))==Decimal(expected["trigger"]["price"]) and
                    _verified_full_close_initial(x,plan["position_mode"],expected) and
                    Decimal(str(x.get("size","0")))==0 and
                    str(x.get("price")) in ("0","0.0") and
                    str(x.get("tif","ioc")).lower()=="ioc" and
                    ORDER_RE.fullmatch(str(o.get("id_string") or o.get("id") or ""))):
                    candidates.append(str(o.get("id_string") or o.get("id")))
            except (TypeError,ValueError,InvalidOperation):
                continue
        if len(candidates)!=1:
            return {"verified":False,"reason":kind+"_MISSING_OR_AMBIGUOUS"}
        matched[kind]=candidates[0]
    if matched["SL"]==matched["TP"]:
        return {"verified":False,"reason":"PROTECTIVE_ID_COLLISION"}
    return {"verified":True,"reason":"EXCHANGE_NATIVE_TRIGGER_READBACK",
            "stop_order_id":matched["SL"],"take_order_id":matched["TP"]}

class GateLiveClient:
    """Allowlisted APIv4 transport. No Web or Testnet fallback for account writes."""
    def __init__(self,key=None,secret=None,transport=None):
        self.key=key if key is not None else os.getenv("GATE_API_KEY","").strip()
        self.secret=secret if secret is not None else os.getenv("GATE_API_SECRET","").strip()
        self.transport=transport or urlopen

    def request(self,method,path,*,params=None,payload=None,timeout=8,expires_at_ms=None):
        if method not in ("GET","POST"):
            raise LiveSafetyError("Unsupported method")
        if method=="GET":
            allow=(path in READ_PATHS or
                   bool(re.fullmatch(PREFIX+r"/orders/[1-9][0-9]{0,19}",path)) or
                   bool(re.fullmatch(PREFIX+r"/contracts/[A-Z0-9]{2,28}_USDT",path)))
            if not allow or payload is not None:
                raise LiveSafetyError("GET path not allowlisted")
        else:
            if not EXCHANGE_WRITE_RELEASE:
                raise LiveSafetyError("SOURCE_LOCKED: real-money API writes prohibited")
            if (os.getenv("LIVE_TRADING_ENABLED")!="true" or
                os.getenv("ORDERS_ENABLED")!="true" or
                os.getenv("LIVE_ORDERS_SEPARATELY_AUTHORIZED")!="true"):
                raise LiveSafetyError("Real-money authorization incomplete")
            if path not in WRITE_PATHS or not isinstance(payload,dict):
                raise LiveSafetyError("Write path/body not allowlisted")
        if expires_at_ms is not None:
            # Gate enforces this deadline on receipt, beyond local age checks.
            # Never inherit an entry deadline for protection or recovery.
            now_ms=int(time.time()*1000)
            if (method != "POST" or path != PREFIX+"/orders"
                    or type(expires_at_ms) is not int
                    or not now_ms < expires_at_ms <= now_ms+60000):
                raise LiveSafetyError("Invalid or expired exchange request deadline")
        if not self.key or not self.secret:
            raise LiveSafetyError("Production Gate.io credentials absent")
        query=urlencode(params or {})
        body=json.dumps(payload,separators=(",",":"),allow_nan=False).encode() if payload is not None else b""
        ts=str(int(time.time()))
        signing="\n".join((method,path,query,hashlib.sha512(body).hexdigest(),ts))
        sign=hmac.new(self.secret.encode(),signing.encode(),hashlib.sha512).hexdigest()
        url=HOST+path+("?" + query if query else "")
        req=Request(url,method=method,data=body if method=="POST" else None,
                    headers={"KEY":self.key,"Timestamp":ts,"SIGN":sign,
                             "Accept":"application/json","Content-Type":"application/json"})
        if expires_at_ms is not None:
            req.add_header("x-gate-exptime",str(expires_at_ms))
        try:
            with self.transport(req,timeout=timeout) as response:
                data=json.loads(response.read(800000))
        except Exception as exc:
            # Especially after POST, timeout does not prove that exchange rejected order.
            if method=="POST":
                raise AckUnknown("POST result unknown: no automatic retry") from exc
            raise LiveSafetyError("Authenticated live GET failed") from exc
        if not isinstance(data,(dict,list)):
            raise LiveSafetyError("Unexpected exchange response")
        return data

    def snapshot(self):
        """Read account and all exposure sources or fail closed."""
        account=self.request("GET",PREFIX+"/accounts")
        positions=self.request("GET",PREFIX+"/positions")
        orders=self.request("GET",PREFIX+"/orders",params={"status":"open","limit":100})
        triggers=self.request("GET",PREFIX+"/price_orders",params={"status":"open","limit":100})
        if not isinstance(account,dict) or account.get("currency")!="USDT":
            raise LiveSafetyError("Unverified production account")
        if not isinstance(orders,list) or not isinstance(triggers,list):
            raise LiveSafetyError("Incomplete exchange order inventory")
        if len(orders)>=100 or len(triggers)>=100:
            raise LiveSafetyError("Order pagination required; exposure ambiguous")
        return {"account":account,"positions":active_positions(positions),
                "position_settings":positions,
                "orders":orders,"triggers":triggers,"source":"GATE_IO_LIVE_FUTURES",
                "fetched_at":int(time.time()),"orders_enabled":False}

    def submit_entry(self,plan):
        """Deliberately source-locked until separate live trial authorization."""
        if not EXCHANGE_WRITE_RELEASE:
            raise LiveSafetyError("SOURCE_LOCKED: real-money API writes prohibited")
        now=time.time()
        received=plan.get("received_epoch")
        if type(received) is not int or not 0 <= now-received < MAX_SIGNAL_AGE_SECONDS:
            raise LiveSafetyError("Entry signal deadline expired or invalid")
        deadline=(received+MAX_SIGNAL_AGE_SECONDS)*1000
        if "submission_expires_epoch" in plan:
            reviewed=plan["submission_expires_epoch"]
            if type(reviewed) is not int:
                raise LiveSafetyError("Invalid reviewed entry deadline")
            deadline=min(deadline,reviewed*1000)
        return self.request("POST",PREFIX+"/orders",payload=build_entry_order(plan),
                            expires_at_ms=deadline)

    def submit_trigger(self,plan,kind):
        return self.request("POST",PREFIX+"/price_orders",
                            payload=build_protective_trigger(plan,kind))

def verify_ledger_position(item, position, trigger_orders):
    """Prove lifecycle, exact exposure and journal-linked native protection."""
    try:
        if item.get('state') != 'PROTECTED':
            return {'verified': False, 'reason': 'UNVERIFIED_LEDGER_LIFECYCLE_STATE'}
        if item.get('contract') != position.get('contract'):
            raise LiveSafetyError('Contract mismatch')
        quantity = dec(item.get('contracts'), 'journal contracts')
        if quantity != quantity.to_integral_value():
            raise LiveSafetyError('Fractional journal contracts')
        if item.get('side') not in ('LONG', 'SHORT'):
            raise LiveSafetyError('Unknown journal side')
        expected = quantity if item['side'] == 'LONG' else -quantity
        if Decimal(str(position.get('size'))) != expected:
            return {'verified': False, 'reason': 'POSITION_SIZE_OR_SIDE_MISMATCH'}
        plan = dict(contract=item['contract'], side=item['side'],
                    entry=item['entry_price'], sl=item['stop_loss'],
                    tp=item['take_profit'], position_mode=item['position_mode'])
        result = evaluate_protection(plan, trigger_orders)
        if result['verified'] and (
            result['stop_order_id'] != str(item.get('stop_order_id') or '') or
            result['take_order_id'] != str(item.get('take_order_id') or '')
        ):
            return {'verified': False, 'reason': 'PROTECTION_ORDER_IDS_NOT_LINKED_TO_LEDGER'}
        return result
    except (LiveSafetyError, KeyError, TypeError, InvalidOperation, ValueError):
        return {'verified': False, 'reason': 'MALFORMED_POSITION_OR_LEDGER'}


def reconcile_ledger(ledger_rows,positions,trigger_orders):
    """Read-only restart comparison; unknown exposure is never called CLOSED."""
    if not isinstance(ledger_rows,list):
        raise LiveSafetyError("Durable ledger unavailable")
    openpos=active_positions(positions)
    results=[]
    active_contracts=[r.get('contract') for r in ledger_rows
                      if r.get('state') != 'CLOSED_VERIFIED']
    for item in ledger_rows:
        same=[p for p in openpos if p.get("contract")==item.get("contract")]
        state=item.get("state")
        if state != 'CLOSED_VERIFIED' and active_contracts.count(item.get('contract')) != 1:
            results.append(dict(event_id=item.get('event_id'), state='HALT_RECONCILIATION',
                                reason='DUPLICATE_ACTIVE_LEDGER_CONTRACT'))
            continue
        if state=="RESERVED" and not same:
            results.append({"event_id":item.get("event_id"),"state":"RESERVED_NOT_SENT",
                            "reason":"DURABLE_RESERVATION_ONLY"})
            continue
        if state=="CLOSED_VERIFIED":
            # A later tracked trade on the same contract does not reopen history.
            same = same if item.get('contract') not in active_contracts else []
            results.append({"event_id":item.get("event_id"),
                            "state":"HALT_RECONCILIATION" if same else "CLOSED_READBACK",
                            "reason":"CLOSED_LEDGER_BUT_EXCHANGE_POSITION_OPEN" if same else "NO_CURRENT_EXCHANGE_POSITION"})
            continue
        if state!="PROTECTED":
            # Open exposure cannot become certified merely because a trigger
            # happens to exist while the durable entry/ack journal is pending.
            results.append({"event_id":item.get("event_id"),"state":"HALT_RECONCILIATION",
                            "reason":"UNVERIFIED_LEDGER_LIFECYCLE_STATE"})
            continue
        if len(same)!=1:
            results.append({"event_id":item.get("event_id"),"state":"HALT_RECONCILIATION",
                            "reason":"POSITION_MISSING_OR_AMBIGUOUS"})
            continue
        p=same[0]
        try:
            protection=verify_ledger_position(item,p,trigger_orders)
            results.append({"event_id":item["event_id"],
                            "state":"PROTECTED_READBACK" if protection["verified"] else "HALT_UNPROTECTED",
                            "reason":protection["reason"],"protection":protection})
        except (LiveSafetyError,KeyError,TypeError,InvalidOperation,ValueError):
            results.append({"event_id":item.get("event_id"),"state":"HALT_RECONCILIATION",
                            "reason":"MALFORMED_POSITION_OR_LEDGER"})
    known={r.get("contract") for r in ledger_rows}
    for p in openpos:
        if p["contract"] not in known:
            results.append({"event_id":None,"state":"HALT_UNKNOWN_EXPOSURE",
                            "contract":p["contract"],"reason":"UNTRACKED_EXCHANGE_POSITION"})
    return {"safe_to_dispatch":False,"results":results,
            "requires_halt":any(x["state"].startswith("HALT") for x in results),
            "orders_enabled":False}



