"""LUXALGO PRO: isolated Gate.io LIVE USDT-M execution foundation.

NO REAL-MONEY ORDERS ARE PERMITTED IN THIS REVISION.  Changing Render env
variables cannot release exchange writes. This module intentionally contains
a transport and order-construction code for a separately reviewed cutover.
All operations default to authenticated GET or pure planning. No Testnet.
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

HOST = "https://api.gateio.ws"
PREFIX = "/api/v4/futures/usdt"
# A code-reviewed release, in addition to explicit permission, is required.
# This is NOT an environment variable and MUST stay False until verification.
EXCHANGE_WRITE_RELEASE = False
MAX_POSITIONS = 5
PER_ENTRY_RISK = Decimal("0.005")
MAX_AGGREGATE_RISK = Decimal("0.025")
MAX_LEVERAGE = Decimal("5")
MAX_SIGNAL_AGE_SECONDS = 60
CONTRACT_RE = re.compile(r"^[A-Z0-9]{2,28}_USDT$")
ORDER_RE = re.compile(r"^[1-9][0-9]{0,19}$")
READ_PATHS = {PREFIX + suffix for suffix in
              ("/accounts", "/positions", "/orders", "/price_orders", "/contracts")}
WRITE_PATHS = {PREFIX + "/orders", PREFIX + "/price_orders"}
MIGRATION_SQL = """
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
        size=Decimal(str(p.get("size","0")))
        if not size.is_finite():
            raise LiveSafetyError("Invalid position size")
        if size: out.append(p)
    return out

def build_plan(signal, metadata, equity, available, positions, reserved_risk=0, now=None):
    """Contract-count plan, conservative 0.5% per entry / 2.5% aggregate.

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
    if order_size_min != order_size_min.to_integral_value() or order_size_max != order_size_max.to_integral_value():
        raise LiveSafetyError("Unsupported fractional contract-count semantics")
    reserve=Decimal(str(reserved_risk))
    if not reserve.is_finite() or reserve<0:
        raise LiveSafetyError("Invalid reserved risk")
    budget=min(eq*PER_ENTRY_RISK,eq*MAX_AGGREGATE_RISK-reserve)
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
    units_by_margin=(av*lev/(e*multiplier)).to_integral_value(rounding=ROUND_FLOOR)
    count=min(units_by_risk,units_by_margin,order_size_max)
    if count<order_size_min or count<1:
        raise LiveSafetyError("Insufficient margin/risk for exchange minimum lot")
    risk=count*unit_loss
    if risk>eq*PER_ENTRY_RISK or risk+reserve>eq*MAX_AGGREGATE_RISK:
        raise LiveSafetyError("Risk cap crossed")
    return dict(event_id=event,client_text=text,contract=contract,side=side,timeframe=tf,
                contracts=int(count),entry=str(e),sl=str(s),tp=str(t),
                estimated_risk_usdt=str(risk),risk_cap_usdt=str(budget),
                risk_includes_modeled_taker_fees_and_slippage=True,
                modeled_stop_risk_usdt=str(count*unit_stop_loss),
                modeled_fee_slippage_usdt=str(count*unit_fee_slip),
                max_entry_slippage_ratio=str(slip),
                leverage=str(lev),position_mode=position_mode,verified_live_order=False)

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
                    (bool(x.get("close")) or bool(x.get("reduce_only")) or
                     bool(x.get("is_close")) or bool(x.get("is_reduce_only"))) and
                    (x.get("auto_size")==expected["initial"].get("auto_size")
                     if plan["position_mode"]=="dual"
                     else x.get("auto_size") in (None,"")) and
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

    def request(self,method,path,*,params=None,payload=None,timeout=8):
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
                "orders":orders,"triggers":triggers,"source":"GATE_IO_LIVE_FUTURES",
                "fetched_at":int(time.time()),"orders_enabled":False}

    def submit_entry(self,plan):
        """Deliberately source-locked until separate live trial authorization."""
        return self.request("POST",PREFIX+"/orders",payload=build_entry_order(plan))

    def submit_trigger(self,plan,kind):
        return self.request("POST",PREFIX+"/price_orders",
                            payload=build_protective_trigger(plan,kind))

def reconcile_ledger(ledger_rows,positions,trigger_orders):
    """Read-only restart comparison; unknown exposure is never called CLOSED."""
    if not isinstance(ledger_rows,list):
        raise LiveSafetyError("Durable ledger unavailable")
    openpos=active_positions(positions)
    results=[]
    for item in ledger_rows:
        same=[p for p in openpos if p.get("contract")==item.get("contract")]
        state=item.get("state")
        if state=="RESERVED" and not same:
            results.append({"event_id":item.get("event_id"),"state":"RESERVED_NOT_SENT",
                            "reason":"DURABLE_RESERVATION_ONLY"})
            continue
        if state=="CLOSED_VERIFIED":
            results.append({"event_id":item.get("event_id"),
                            "state":"HALT_RECONCILIATION" if same else "CLOSED_READBACK",
                            "reason":"CLOSED_LEDGER_BUT_EXCHANGE_POSITION_OPEN" if same else "NO_CURRENT_EXCHANGE_POSITION"})
            continue
        if len(same)!=1:
            results.append({"event_id":item.get("event_id"),"state":"HALT_RECONCILIATION",
                            "reason":"POSITION_MISSING_OR_AMBIGUOUS"})
            continue
        p=same[0]
        try:
            side="LONG" if Decimal(str(p["size"]))>0 else "SHORT"
            if side!=item.get("side"): raise LiveSafetyError("Direction mismatch")
            plan={"contract":item["contract"],"side":item["side"],
                  "entry":item["entry_price"],"sl":item["stop_loss"],"tp":item["take_profit"],"position_mode":item["position_mode"]}
            protection=evaluate_protection(plan,trigger_orders)
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
