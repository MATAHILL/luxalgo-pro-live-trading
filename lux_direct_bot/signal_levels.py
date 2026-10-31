"""Parse optional original LuxAlgo entry/SL1/TP1 reference prices, never fills.

Source price values do not prove a Binance execution price. No indicator
calculations or market-derived replacements are performed.
"""
from decimal import Decimal, InvalidOperation


def parse_luxalgo_reference_levels(payload,side):
    """Return (entry, stop, target, status). Missing/invalid never fabricated."""
    fields=("entry","sl","tp1")
    values=[payload.get(x) for x in fields]
    if any(x is None or str(x).strip()=="" for x in values):
        return (None,None,None,"NOT_PROVIDED")
    try:
        if any(isinstance(x,bool) for x in values):
            raise ValueError("Boolean is not a price")
        nums=[Decimal(str(x)) for x in values]
        if any(not x.is_finite() or x<=0 for x in nums):
            raise ValueError("Invalid reference price")
        entry,stop,target=nums
        if not ((stop<entry<target) if side=="BUY" else (target<entry<stop)):
            raise ValueError("Invalid LuxAlgo price geometry")
        return (str(entry),str(stop),str(target),"VALID")
    except (InvalidOperation,ValueError,TypeError):
        return (None,None,None,"INVALID")
