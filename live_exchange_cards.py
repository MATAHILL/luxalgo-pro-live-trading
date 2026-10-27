"""Read-only Gate Futures position-card adapter (STAGING ONLY).

Never estimates P&L from candles. P&L and entry rates are sourced from a
fresh authenticated exchange snapshot supplied by the caller. No order writes.
"""
import re
from decimal import Decimal,InvalidOperation

def _number(value,*,positive=False):
    try:
        v=Decimal(str(value))
    except (ValueError,TypeError,InvalidOperation):
        raise ValueError("Invalid exchange numeric readback") from None
    if not v.is_finite() or (positive and v<=0):
        raise ValueError("Invalid exchange numeric readback")
    return str(v)

def exchange_position_cards(positions,ledger_rows,triggers):
    if (not isinstance(positions,list) or not isinstance(ledger_rows,list)
        or not isinstance(triggers,list)
        or not all(isinstance(x,dict) for x in positions+ledger_rows+triggers)):
        raise ValueError("Incomplete exchange/card readback")
    active=[]
    for p in positions:
        qty=Decimal(_number(p.get("size")))
        if qty!=0:
            active.append(p)
    if len(active)>5:
        raise ValueError("More than five exchange positions: quarantine")
    cards=[]
    for p in active:
        contract=p.get("contract")
        mode=p.get("mode")
        if (not isinstance(contract,str) or not re.fullmatch(r"[A-Z0-9]{2,28}_USDT",contract)
            or mode not in ("dual_long","dual_short")):
            raise ValueError("Position mode or contract not verified")
        side="LONG" if mode=="dual_long" else "SHORT"
        matches=[x for x in ledger_rows
                 if x.get("contract")==contract and x.get("side")==side]
        if len(matches)!=1:
            raise ValueError("Live position not uniquely owned by ledger")
        row=matches[0]
        details=row.get("details")
        if not isinstance(details,dict):
            raise ValueError("Missing live protection journal details")
        sid=str(details.get("stop_order_id",""))
        tid=str(details.get("take_order_id",""))
        # No silent assumption that previously checked protection still exists.
        live_ids={str(x.get("id")) for x in triggers
                  if x.get("status")=="open" and
                  (x.get("initial") or {}).get("contract")==contract}
        protected=(row.get("state")=="PROTECTED_VERIFIED" and sid and tid
                   and sid!=tid and sid in live_ids and tid in live_ids)
        cards.append({
            "symbol":contract.replace("_",""),
            "side":side,
            "contracts":_number(p["size"]),
            "entry_price":_number(p["entry_price"],positive=True),
            "mark_price":_number(p["mark_price"],positive=True),
            "unrealised_pnl_usdt":_number(p["unrealised_pnl"]),
            "stop_order_id":sid or None,
            "take_order_id":tid or None,
            "protection_state":"EXCHANGE_VERIFIED" if protected else "UNVERIFIED_HALT",
            "source":"GATE_USDT_FUTURES_EXCHANGE",
            "simulated":False,
        })
    return cards
