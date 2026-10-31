import pytest
from decimal import Decimal
from execution_runner import simulate_protected_execution, EmergencyHalt
from order_plan import create_protected_plan

def plan(side="BUY"):
    return create_protected_plan(
        symbol="XRPUSDT", direction=side, signal_id="confirmation-bar-abc123",
        quantity="10", reference_price="2.0",
        stop_price="1.95" if side=="BUY" else "2.05",
        take_profit_price="2.10" if side=="BUY" else "1.90",
        quantity_step=".1", price_tick=".001",
        minimum_notional="5", maximum_notional="30", maximum_loss="1",
    )

class FakeExchange:
    def __init__(self, *, fail=None):
        self.fail=fail
        self.qty=Decimal("0")
        self.entry={}
        self.protections={}
        self.closes=[]
        self.open_order_ids=[]
        self.position_side="BOTH"

    def snapshot(self):
        return {"qty":str(self.qty), "open_order_ids":self.open_order_ids,
                "position_side":self.position_side,
                "protection_ids":list(self.protections)}

    def enter(self, spec):
        signed=Decimal(spec["quantity"]) * (1 if spec["side"]=="BUY" else -1)
        self.qty=signed
        self.entry={"symbol":spec["symbol"],"side":spec["side"],"status":"FILLED",
                    "clientOrderId":spec["newClientOrderId"], "executedQty":spec["quantity"]}
        if self.fail=="entry_uncertain":
            raise TimeoutError("Ack lost, entry may have filled")

    def read_entry(self, client_id):
        if self.fail=="partial":
            return self.entry | {"status":"PARTIALLY_FILLED","executedQty":"5"}
        return dict(self.entry)

    def create_algo(self, spec):
        self.protections[spec["clientAlgoId"]]=dict(spec)
        if self.fail=="algo_timeout" and spec["type"]=="STOP_MARKET":
            raise TimeoutError("Ack lost but algo may exist")

    def read_algo(self, client_id):
        spec=self.protections.get(client_id)
        if not spec:return {}
        row={"symbol":spec["symbol"],"side":spec["side"],
             "positionSide":spec["positionSide"],"clientAlgoId":client_id,
             "orderType":spec["type"], "triggerPrice":spec["triggerPrice"],
             "algoStatus":"NEW","closePosition":True}
        if self.fail=="bad_tp" and spec["type"]=="TAKE_PROFIT_MARKET":
            row["algoStatus"]="REJECTED"
        return row

    def emergency_reduce_only_close(self, qty, side):
        if self.fail=="close_fails":
            raise RuntimeError("No confirmation on close")
        self.closes.append((qty,side))
        self.qty=Decimal("0")

    def cancel_algo(self, cid):
        if self.fail=="orphan_cancel":
            raise RuntimeError("Could not cancel orphan")
        self.protections.pop(cid,None)


@pytest.mark.parametrize("side",["BUY","SELL"])
def test_successful_mock_exchange_sequence(side):
    ex=FakeExchange()
    result=simulate_protected_execution(plan(side),ex)
    assert result["state"]=="SIMULATION_ACCEPTED"
    assert result["orders_sent_to_real_exchange"]==0
    assert len(ex.protections)==2
    assert ex.closes==[]
    assert (ex.qty>0) == (side=="BUY")

@pytest.mark.parametrize("failure",["entry_uncertain","partial","bad_tp","algo_timeout"])
def test_fail_closed_and_flattened(failure):
    ex=FakeExchange(fail=failure)
    with pytest.raises(EmergencyHalt,match="verified flat"):
        simulate_protected_execution(plan(),ex)
    assert ex.qty==0
    assert not ex.protections
    assert ex.closes

@pytest.mark.parametrize("failure",["close_fails","orphan_cancel"])
def test_unrecoverable_failure_halts(failure):
    ex=FakeExchange(fail="bad_tp")
    if failure=="close_fails":
        def cannot_close(quantity, side):
            raise RuntimeError("simulated emergency close timeout")
        ex.emergency_reduce_only_close=cannot_close
    if failure=="orphan_cancel":
        def cannot_cancel(_):
            raise RuntimeError("simulated orphan cancellation failure")
        ex.cancel_algo=cannot_cancel
    with pytest.raises(EmergencyHalt,match="Emergency recovery uncertain|Orphan protection cannot"):
        simulate_protected_execution(plan(),ex)

def test_reject_existing_position_and_orders_without_any_write():
    for attr,val in [("qty",Decimal("10")),("open_order_ids",["manual-order"]),
                     ("position_side","LONG")]:
        ex=FakeExchange()
        setattr(ex,attr,val)
        with pytest.raises(EmergencyHalt):
            simulate_protected_execution(plan(),ex)
        assert ex.entry=={}
        assert ex.closes==[]
