"""No-network regressions for isolated Binance execution candidate."""
from urllib.parse import urlparse, parse_qs
import pytest

from live_execution_candidate import CandidateBinanceUSDM, AcceptanceBlocked
from order_plan import create_protected_plan

SIGNAL = "genuine-candidate-acceptance-12345"

def plan():
    return create_protected_plan(
        symbol="XRPUSDT", direction="BUY", signal_id=SIGNAL,
        quantity="10", reference_price="2", stop_price="1.95",
        take_profit_price="2.1", quantity_step="0.1", price_tick="0.001",
        minimum_notional="5", maximum_notional="30", maximum_loss="1",
    )

class Ledger:
    def __init__(self):
        self.claims=set()
    def claim(self, signal_id, stage):
        key=(signal_id, stage)
        if key in self.claims:
            raise AcceptanceBlocked("Duplicate durable claim")
        self.claims.add(key)
    def has_claim(self, signal_id, stage):
        return (signal_id, stage) in self.claims

class Response:
    def __init__(self, data):
        self.data=data
    def raise_for_status(self):
        return None
    def json(self):
        return self.data

class FakeHTTP:
    def __init__(self):
        self.calls=[]
        self.positions=[]
        self.open_algos={}
        self.entry=None
        self.emergency=None
        self.fail_stop_ack=False

    def _request(self, method, url, headers):
        parsed=urlparse(url)
        path=parsed.path
        params=parse_qs(parsed.query)
        assert "signature" in params and "timestamp" in params
        assert headers["X-MBX-APIKEY"] == "TEST_KEY"
        self.calls.append((method,path,params))
        if method=="GET":
            if path=="/fapi/v3/positionRisk":
                return Response(self.positions)
            if path=="/fapi/v1/openOrders":
                return Response([])
            if path=="/fapi/v1/openAlgoOrders":
                return Response(list(self.open_algos.values()))
            if path=="/fapi/v1/positionSide/dual":
                return Response({"dualSidePosition": False})
            if path=="/fapi/v1/order":
                cid=params["origClientOrderId"][0]
                row=self.emergency if self.emergency and cid==self.emergency["clientOrderId"] else self.entry
                if row is None or row.get("clientOrderId")!=cid:
                    raise ValueError("Unknown order")
                return Response(row)
            if path=="/fapi/v1/algoOrder":
                cid=params["clientAlgoId"][0]
                row=self.open_algos.get(cid)
                if row is None:
                    raise ValueError("Unknown algo")
                return Response(row)
        if method=="POST" and path=="/fapi/v1/order":
            cid=params["newClientOrderId"][0]
            if params.get("reduceOnly")==["true"]:
                assert self.positions
                self.positions=[]
                self.emergency={"clientOrderId":cid,"status":"FILLED"}
                return Response(self.emergency)
            qty=params["quantity"][0]
            self.positions=[{"symbol":"XRPUSDT","positionAmt":qty}]
            self.entry={"symbol":"XRPUSDT","side":params["side"][0],
                        "positionSide":params["positionSide"][0],
                        "status":"FILLED","executedQty":qty,
                        "clientOrderId":cid}
            return Response(self.entry)
        if method=="POST" and path=="/fapi/v1/algoOrder":
            cid=params["clientAlgoId"][0]
            row={"clientAlgoId":cid, "symbol":"XRPUSDT",
                 "side":params["side"][0],
                 "positionSide":params["positionSide"][0],
                 "orderType":params["type"][0],
                 "triggerPrice":params["triggerPrice"][0],
                 "algoStatus":"NEW", "closePosition":True}
            self.open_algos[cid]=row
            if self.fail_stop_ack and row["orderType"]=="STOP_MARKET":
                raise ValueError("Lost acknowledgment after exchange accepted SL")
            return Response(row)
        if method=="DELETE" and path=="/fapi/v1/algoOrder":
            cid=params["clientAlgoId"][0]
            self.open_algos.pop(cid,None)
            return Response({"code":200})
        raise AssertionError("Unexpected mock exchange request")
    def get(self,url,headers):
        return self._request("GET",url,headers)
    def post(self,url,headers):
        return self._request("POST",url,headers)
    def delete(self,url,headers):
        return self._request("DELETE",url,headers)

def gateway(fake, ledger, allow=False):
    return CandidateBinanceUSDM(
        "TEST_KEY","TEST_SECRET",ledger,client=fake,allow_writes=allow
    )

def test_locked_sender_never_submits_or_claims():
    fake=FakeHTTP()
    ledger=Ledger()
    gw=gateway(fake,ledger)
    with pytest.raises(AcceptanceBlocked,match="locked"):
        gw._write_once(SIGNAL,"ENTRY","POST","/fapi/v1/order",plan().entry)
    assert not fake.calls and not ledger.claims

def test_protected_entry_fixture_and_duplicate_fail_closed():
    fake=FakeHTTP()
    ledger=Ledger()
    gw=gateway(fake,ledger,allow=True)
    spec=plan()
    out=gw.execute_once_with_emergency(SIGNAL,spec)
    assert out["state"]=="PROTECTED_EXCHANGE_READBACK"
    assert len([x for x in fake.calls if x[0]=="POST"])==3
    assert len(fake.open_algos)==2
    # Replay must not close the already protected position.
    with pytest.raises(AcceptanceBlocked,match="Pre-entry blocked"):
        gw.execute_once_with_emergency(SIGNAL,spec)
    assert fake.positions
    assert len([x for x in fake.calls if x[0]=="POST"])==3

def test_stop_ack_unknown_cannot_retry_and_emergency_flattens_once():
    fake=FakeHTTP()
    fake.fail_stop_ack=True
    ledger=Ledger()
    gw=gateway(fake,ledger,allow=True)
    with pytest.raises(AcceptanceBlocked,match="verified flat"):
        gw.execute_once_with_emergency(SIGNAL,plan())
    assert fake.positions==[]
    assert fake.open_algos=={}
    assert len([x for x in fake.calls
                if x[0]=="POST" and x[1]=="/fapi/v1/algoOrder"])==1
    assert len([x for x in fake.calls
                if x[0]=="POST" and x[1]=="/fapi/v1/order"])==2
    assert (SIGNAL,"EMERGENCY") in ledger.claims

def test_existing_manual_position_never_entered_or_closed():
    fake=FakeHTTP()
    fake.positions=[{"symbol":"BTCUSDT","positionAmt":"0.01"}]
    ledger=Ledger()
    gw=gateway(fake,ledger,allow=True)
    with pytest.raises(AcceptanceBlocked,match="Pre-entry blocked"):
        gw.execute_once_with_emergency(SIGNAL,plan())
    assert not ledger.claims
    assert not any(x[0]=="POST" for x in fake.calls)

def test_wrong_stage_path_rejected_without_claim_or_http():
    fake=FakeHTTP()
    ledger=Ledger()
    gw=gateway(fake,ledger,allow=True)
    with pytest.raises(AcceptanceBlocked,match="mismatch"):
        gw._write_once(SIGNAL,"STOP","POST","/fapi/v1/order",plan().entry)
    assert not fake.calls and not ledger.claims
