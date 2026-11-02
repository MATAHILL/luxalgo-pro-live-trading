"""Binance USD-M account readiness: signed GET ONLY; never places/cancels orders.

Live readiness stays BLOCKED until genuine TradingView identity, durable
ledger, protection and recovery exchange proofs, limited acceptance trade,
and separate final financial release are independently certified.
"""
from decimal import Decimal, InvalidOperation
import os
import httpx
from binance_transport import BinanceReadOnly, BinanceTransportBlocked

def _nonnegative(v,label):
    if v is None or isinstance(v,bool):
        raise ValueError("Missing "+label)
    try:
        x=Decimal(str(v))
    except (TypeError,ValueError,InvalidOperation) as e:
        raise ValueError("Malformed "+label) from e
    if not x.is_finite() or x<0:
        raise ValueError("Invalid "+label)
    return x

def snapshot_read_only(client=None):
    """Every inventory from authenticated Binance GET; unknown => blocked.

    No signal ids, secret values or exchange order identifiers are returned.
    Never infer empty inventory from API errors or absent fields.
    """
    safe={"mode":"DRY_RUN","exchange":"BINANCE_USDT_M",
          "orders_enabled":False,"can_trade":False,
          "acceptance_certified":False}
    key=os.environ.get("BINANCE_API_KEY","")
    secret=os.environ.get("BINANCE_API_SECRET","")
    if not key or not secret:
        return dict(safe,status="NOT_CONFIGURED",
                    reason="BINANCE_FUTURES_API_CREDENTIALS_REQUIRED")
    c=client if client is not None else BinanceReadOnly(key,secret)
    try:
        balance=c.get("/fapi/v3/balance")
        account=c.get("/fapi/v3/account")
        positions=c.get("/fapi/v3/positionRisk")
        open_orders=c.get("/fapi/v1/openOrders")
        open_algos=c.get("/fapi/v1/openAlgoOrders")
        mode=c.get("/fapi/v1/positionSide/dual")
        if not isinstance(account,dict) or not all(isinstance(x,list) for x in (balance,positions,open_orders,open_algos)):
            raise ValueError("INCOMPLETE_ACCOUNT_INVENTORY")
        if len(positions)>1000 or len(open_orders)>=1000 or len(open_algos)>=1000:
            raise ValueError("PAGINATION_OR_INVENTORY_LIMIT_UNVERIFIED")
        if not isinstance(mode,dict) or type(mode.get("dualSidePosition")) is not bool:
            raise ValueError("POSITION_MODE_UNVERIFIED")
        balances=[x for x in balance if isinstance(x,dict) and x.get("asset")=="USDT"]
        if len(balances)!=1:
            raise ValueError("USDT_BALANCE_UNVERIFIED")
        available=_nonnegative(balances[0].get("availableBalance"),"available")
        wallet=_nonnegative(balances[0].get("balance"),"wallet")
        # The wallet/deposit figure is NOT Futures equity. Verify signed
        # marginBalance, which includes unrealized P&L, against the balance GET.
        assets=account.get("assets")
        if not isinstance(assets,list):
            raise ValueError("SIGNED_ACCOUNT_ASSETS_UNVERIFIED")
        rows=[x for x in assets if isinstance(x,dict) and x.get("asset")=="USDT"]
        if len(rows)!=1:
            raise ValueError("SIGNED_USDT_EQUITY_UNVERIFIED")
        equity=_nonnegative(rows[0].get("marginBalance"),"futures equity")
        signed_wallet=_nonnegative(rows[0].get("walletBalance"),"account wallet")
        signed_available=_nonnegative(rows[0].get("availableBalance"),"account available")
        tolerance=Decimal("0.00000001")
        if (abs(signed_wallet-wallet)>tolerance
                or abs(signed_available-available)>tolerance
                or available>equity):
            raise ValueError("SIGNED_FUTURES_BALANCES_DISAGREE")
        active_positions=0
        for p in positions:
            if not isinstance(p,dict) or not isinstance(p.get("symbol"),str):
                raise ValueError("INVALID_POSITION_INVENTORY")
            if p.get("positionAmt") is None:
                raise ValueError("UNKNOWN_POSITION_AMOUNT")
            amount=_nonnegative(abs(Decimal(str(p["positionAmt"]))),"position amount")
            active_positions+=int(amount!=0)
        for group in (open_orders,open_algos):
            if not all(isinstance(o,dict) and isinstance(o.get("symbol"),str)
                       for o in group):
                raise ValueError("UNVERIFIED_OPEN_ORDER_INVENTORY")
        flat=active_positions==0 and not open_orders and not open_algos
        return dict(safe,status="CONNECTED_READ_ONLY",
            position_mode="HEDGE" if mode["dualSidePosition"] else "ONE_WAY",
            account_snapshot_complete=True,exchange_flat=flat,
            wallet_usdt=str(wallet),available_usdt=str(available),
            futures_equity_usdt=str(equity),
            equity_basis="SIGNED_USDT_FUTURES_MARGIN_BALANCE",
            open_positions=active_positions,
            open_orders=len(open_orders),open_conditional_orders=len(open_algos),
            reason="ACCEPTANCE_NOT_CERTIFIED_NO_LIVE_ORDERS")
    except (httpx.HTTPError,BinanceTransportBlocked,TypeError,ValueError,
            KeyError,OverflowError,ArithmeticError) as exc:
        return dict(safe,status="READBACK_UNVERIFIED",
                    reason=type(exc).__name__.upper()+"_FAIL_CLOSED")
