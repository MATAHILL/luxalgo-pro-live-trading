"""Read-only Gate Futures TESTNET position-card feed.

Use only exchange-returned entry/mark/PnL and recheck attached native TP/SL.
Never import production credentials and never submit or cancel orders.
"""
from testnet_protected_coordinator import TestnetIntent

def build_testnet_position_cards(positions,journal_rows,broker):
    if (not isinstance(positions,list) or not isinstance(journal_rows,list)
        or not all(isinstance(v,dict) for v in positions+journal_rows)):
        raise ValueError("Invalid Testnet card snapshot")
    cards=[]
    # Only ask Gate for additional readbacks if at least one journal-owned position exists.
    for pos in positions:
        contract=pos.get("contract")
        side=pos.get("side")
        if not isinstance(contract,str) or side not in ("LONG","SHORT"):
            raise ValueError("Invalid Testnet contract or position side")
        result={
          "symbol":contract.replace("_",""),"contract":contract,"side":side,
          "size_contracts":pos.get("size_contracts"),
          "entry_price":pos.get("entry_price"),
          "mark_price":pos.get("mark_price"),
          "unrealized_pnl":pos.get("unrealized_pnl"),
          "leverage_label":pos.get("leverage_label"),
          "margin_usdt":pos.get("margin_usdt"),
          "roi_percent":pos.get("roi_percent"),
          "liquidation_price":pos.get("liquidation_price"),
          "source":"GATE_FUTURES_TESTNET","simulated":False,
          "protection_state":"UNTRACKED_TESTNET_POSITION",
          "stop_loss":None,"take_profit":None,
          "stop_order_id":None,"take_order_id":None,
          "entry_order_id":None,
          "origin":"TESTNET_POSITION"}
        candidates=[r for r in journal_rows if r.get("contract")==contract and r.get("side")==side]
        if len(candidates)!=1:
            if len(candidates)>1:
                result["protection_state"]="AMBIGUOUS_JOURNAL"
            cards.append(result)
            continue
        row=candidates[0]
        result["signal_id"]=str(row["signal_id"])
        result["origin"]="ONE_SHOT_TESTNET_TRIAL" if str(row.get("signal_id","")).startswith("testnet-trial-") else "TESTNET_SIGNAL"
        details=row.get("details")
        payload=row.get("payload")
        if (row.get("state")!="PROTECTED_VERIFIED" or not isinstance(details,dict)
            or not isinstance(payload,dict)):
            result["protection_state"]="JOURNAL_NOT_VERIFIED"
            cards.append(result)
            continue
        oid=str(details.get("order_id") or "")
        expected=str(row.get("client_id") or "")
        if (not oid.isdigit() or payload.get("text")!=expected
            or payload.get("contract")!=contract
            or payload.get("size")!=(1 if side=="LONG" else -1)):
            result["protection_state"]="JOURNAL_IDENTITY_MISMATCH"
            cards.append(result)
            continue
        try:
            # Broker readbacks are authenticated against Gate Testnet ONLY.
            broker._contract=contract
            order=broker.order(oid)
            raw_positions=broker.positions()
            triggers=broker.native_triggers()
            intent=TestnetIntent(str(row["signal_id"]),contract,side,
                                 str(pos.get("entry_price") or "0"),
                                 str(payload["tpsl_sl_trigger_price"]),
                                 str(payload["tpsl_tp_trigger_price"]),expected)
            if broker.verify_protection(order,raw_positions,triggers,intent,payload) is not True:
                result["protection_state"]="NATIVE_PROTECTION_UNVERIFIED"
            else:
                evidence=getattr(broker,"last_protection_evidence",None)
                ids=evidence.get("native_protection_ids") if isinstance(evidence,dict) else None
                if (not isinstance(evidence,dict) or evidence.get("entry_id")!=oid
                    or evidence.get("entry_finish_as")!="filled"
                    or not isinstance(ids,list) or len(ids)!=2
                    or len({str(v) for v in ids})!=2):
                    result["protection_state"]="NATIVE_PROTECTION_UNVERIFIED"
                else:
                    result.update(protection_state="EXCHANGE_VERIFIED",
                                  stop_loss=str(payload["tpsl_sl_trigger_price"]),
                                  take_profit=str(payload["tpsl_tp_trigger_price"]),
                                  stop_order_id=str(ids[0]),take_order_id=str(ids[1]),
                                  entry_order_id=oid)
        except Exception:
            # Keep the actual position visible; never assume stale protection is valid.
            result["protection_state"]="PROTECTION_READBACK_UNAVAILABLE"
        cards.append(result)
    return cards
