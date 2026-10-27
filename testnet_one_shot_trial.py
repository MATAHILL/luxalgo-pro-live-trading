"""One-shot one-contract Gate.io FUTURES TESTNET entry + native TP/SL verification.

Requires three independent opt-in flags. Never touches production keys or hosts.
A fixed journal signal/client ID prevents any retry on redeploy or ambiguous POST.
The one-contract trial is capped at 25 USDT notional, 2.5% of margin, and 0.5% stop risk.
Both webhook automation and real-money dispatch MUST remain off.
"""
import os
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING, InvalidOperation
from testnet_execution_journal import PostgresTestnetJournal
from testnet_protected_coordinator import TestnetIntent, execute_protected_testnet
from gate_testnet_broker import GateFuturesTestnetBroker, ROOT
from testnet_readiness import inspect_testnet_readiness, _nonzero_position

CONTRACT="ETH_USDT"
SIGNAL_ID="testnet-trial-20261027-a"
CLIENT_ID="t-lx-trial-20261027-a"

def _rounded(value,tick,mode):
    return (value/tick).to_integral_value(rounding=mode)*tick

def run_one_shot_protected_testnet_trial(*,request=None,broker=None,journal=None):
    """Execute AT MOST once, after read-only safety preflight.

    The one-time flag is not equivalent to an authorized live order.
    """
    required=("GATE_TESTNET_ONE_SHOT_PROTECTED_TRIAL",
              "GATE_TESTNET_PROTECTED_EXECUTION_ENABLED",
              "GATE_TESTNET_RECOVERY_ENABLED")
    if any(os.getenv(k,"false").lower()!="true" for k in required):
        return {"state":"TRIAL_DISABLED","submitted":False}
    if (os.getenv("GATE_TESTNET_WEBHOOK_AUTOMATION_ENABLED","false").lower()=="true"
        or os.getenv("LIVE_TRADING_ENABLED","false").lower()=="true"
        or os.getenv("ORDERS_ENABLED","false").lower()=="true"):
        return {"state":"UNSAFE_EXECUTION_FLAG_COMBINATION","submitted":False}
    gateway=broker or GateFuturesTestnetBroker(request=request)
    read=gateway.request
    ledger=journal or PostgresTestnetJournal()
    # Global flat account, no pending orders/triggers; never disturb other trades.
    try:
        report=inspect_testnet_readiness(request=read)
        if report.get("ready") is not True or report.get("probe_contract")!=CONTRACT:
            return {"state":"TESTNET_READINESS_NOT_VERIFIED","submitted":False,
                    "blockers":report.get("blockers",["UNKNOWN"])}
        account=read("GET",ROOT+"/accounts")
        spec=read("GET",ROOT+"/contracts/"+CONTRACT)
        positions=read("GET",ROOT+"/positions")
        orders=read("GET",ROOT+"/orders",query="status=open")
        triggers=read("GET",ROOT+"/price_orders",query="status=open")
        if (not isinstance(account,dict) or not isinstance(spec,dict)
            or spec.get("name")!=CONTRACT or not isinstance(positions,list)
            or not isinstance(orders,list) or not isinstance(triggers,list)
            or not all(isinstance(r,dict) for r in positions+orders+triggers)
            or any(_nonzero_position(p) for p in positions)
            or orders or triggers):
            return {"state":"TESTNET_ACCOUNT_NOT_EMPTY_OR_UNVERIFIED","submitted":False}
        mark=Decimal(str(spec["mark_price"]))
        tick=Decimal(str(spec["order_price_round"]))
        mult=Decimal(str(spec["quanto_multiplier"]))
        available=Decimal(str(account["available"]))
        if not all(x.is_finite() and x>0 for x in (mark,tick,mult,available)):
            raise ValueError("Invalid market metadata")
        # One contract, 1.0% price-stop, 1.5% target, rounded to Gate tick size.
        stop=_rounded(mark*Decimal("0.990"),tick,ROUND_FLOOR)
        target=_rounded(mark*Decimal("1.015"),tick,ROUND_CEILING)
        if not stop<mark<target or (mark-stop)*mult>available*Decimal("0.005"):
            return {"state":"TRIAL_STOP_RISK_EXCEEDS_TESTNET_LIMIT","submitted":False}
        notional=mark*mult
        if notional>min(available*Decimal("0.025"),Decimal("25")):
            return {"state":"TRIAL_NOTIONAL_LIMIT","submitted":False}
        intent=TestnetIntent(SIGNAL_ID,CONTRACT,"LONG",str(mark),str(stop),
                            str(target),CLIENT_ID)
        return execute_protected_testnet(intent,gateway,ledger)
    except Exception as exc:
        # Never retry a write or assume it failed after a timeout.
        return {"state":"TRIAL_UNCERTAIN_"+type(exc).__name__.upper(),"submitted":"UNKNOWN"}


def inspect_one_shot_trial_preflight(*, request=None):
    """READ-ONLY account-flat and one-contract budget diagnostic.

    No flags are changed, no journal reservation occurs, and no exchange POST
    is possible through this function. Do not mistake READY for a verified fill.
    """
    from gate_testnet_broker import _request_testnet
    read = request or _request_testnet
    report = {"environment":"GATE_FUTURES_TESTNET", "read_only":True,
              "contract":CONTRACT, "order_submitted":False}
    try:
        acct = read("GET",ROOT+"/accounts")
        spec = read("GET",ROOT+"/contracts/"+CONTRACT)
        pos = read("GET",ROOT+"/positions")
        orders = read("GET",ROOT+"/orders",query="status=open")
        triggers = read("GET",ROOT+"/price_orders",query="status=open")
        if (not isinstance(acct,dict) or not isinstance(spec,dict)
            or spec.get("name")!=CONTRACT or not isinstance(pos,list)
            or not isinstance(orders,list) or not isinstance(triggers,list)
            or not all(isinstance(row,dict) for row in pos+orders+triggers)):
            return {**report,"state":"INVALID_READBACK"}
        if any(_nonzero_position(p) for p in pos) or orders or triggers:
            return {**report,"state":"ACCOUNT_NOT_FLAT"}
        mark=Decimal(str(spec["mark_price"]))
        tick=Decimal(str(spec["order_price_round"]))
        multiplier=Decimal(str(spec["quanto_multiplier"]))
        available=Decimal(str(acct["available"]))
        if not all(v.is_finite() and v>0 for v in (mark,tick,multiplier,available)):
            return {**report,"state":"INVALID_MARKET_METADATA"}
        stop=_rounded(mark*Decimal("0.990"),tick,ROUND_FLOOR)
        target=_rounded(mark*Decimal("1.015"),tick,ROUND_CEILING)
        notional=mark*multiplier
        max_notional=min(available*Decimal("0.025"),Decimal("25"))
        risk=(mark-stop)*multiplier
        max_risk=available*Decimal("0.005")
        summary={**report,"one_contract_notional_usdt":str(notional),
                 "maximum_trial_notional_usdt":str(max_notional),
                 "one_contract_stop_risk_usdt":str(risk),
                 "maximum_trial_risk_usdt":str(max_risk)}
        if not stop<mark<target:
            return {**summary,"state":"INVALID_STOP_TARGET_GEOMETRY"}
        if risk>max_risk:
            return {**summary,"state":"TRIAL_STOP_RISK_EXCEEDS_TESTNET_LIMIT"}
        if notional>max_notional:
            return {**summary,"state":"TRIAL_NOTIONAL_LIMIT"}
        return {**summary,"state":"BUDGET_PASS_NO_ORDER"}
    except Exception as exc:
        return {**report,"state":"READBACK_UNAVAILABLE_"+type(exc).__name__.upper()}
