"""Verify a private LUX DIRECT BOT signal export without network or trading.

Usage: python verify_signal_backup.py PATH_TO_BACKUP_FOLDER
Loads signals.json and backup_manifest.json from the owner's local export.
Prints counts only, never identifiers, secrets, or private signal payloads.
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

HASH_RE = re.compile(r"^[a-f0-9]{28}$")
BAR_RE = re.compile(r"^[0-9]{13}$")
SYMBOL_RE = re.compile(r"^[A-Z0-9]{5,20}$")
STATES = frozenset(("DRY_RUN", "PAUSED"))


class BackupUnverified(ValueError):
    pass


def verify_backup(signals, manifest):
    if not isinstance(signals,list) or not isinstance(manifest,dict):
        raise BackupUnverified("Invalid export structure")
    if manifest.get("service")!="LUX DIRECT BOT" or manifest.get("mode")!="DRY_RUN":
        raise BackupUnverified("Not the expected isolated dry-run receiver")
    if manifest.get("contains_exchange_orders") is not False:
        raise BackupUnverified("Export must not claim real exchange orders")
    count=manifest.get("server_total_signal_records")
    saved=manifest.get("saved_signal_records")
    if type(count) is not int or type(saved) is not int or count<0 or saved<0:
        raise BackupUnverified("Invalid source record counts")
    if count!=saved or saved!=len(signals) or saved>30:
        raise BackupUnverified("Export incomplete or inconsistent")
    if manifest.get("complete_signal_history_confirmed") is not True:
        raise BackupUnverified("Receiver did not certify its exposed history complete")
    seen=set()
    symbols=set()
    for row in signals:
        if not isinstance(row,dict):
            raise BackupUnverified("Malformed signal row")
        if not HASH_RE.fullmatch(str(row.get("id",""))):
            raise BackupUnverified("Malformed signal fingerprint")
        if row["id"] in seen:
            raise BackupUnverified("Repeated signal fingerprint")
        seen.add(row["id"])
        symbol=row.get("symbol")
        if not isinstance(symbol,str) or not SYMBOL_RE.fullmatch(symbol):
            raise BackupUnverified("Invalid signal market")
        symbols.add(symbol)
        if row.get("side") not in ("BUY","SELL") or row.get("status") not in STATES:
            raise BackupUnverified("Invalid signal side or recorded state")
        if str(row.get("timeframe"))!="5" or not BAR_RE.fullmatch(str(row.get("bartime",""))):
            raise BackupUnverified("Invalid 5m bar timestamp")
        if type(row.get("at")) is not int or row["at"]<=0:
            raise BackupUnverified("Invalid receipt timestamp")
        if row.get("event") not in ("bullish confirmation signal","bullish confirmation",
                                    "bullish +","bearish confirmation signal",
                                    "bearish confirmation","bearish +"):
            raise BackupUnverified("Nonconfirmation signal in history")
    if manifest.get("bnbusdt_record_present") is not ("BNBUSDT" in symbols):
        raise BackupUnverified("BNB manifest mismatch")
    if manifest.get("birbusdt_record_present") is not ("BIRBUSDT" in symbols):
        raise BackupUnverified("BIRB manifest mismatch")
    return {
        "complete_history_exported":True,
        "saved_records":len(signals),
        "bnb_present":"BNBUSDT" in symbols,
        "birb_present":"BIRBUSDT" in symbols,
        "contains_exchange_orders":False,
        "can_authorize_trading":False,
    }


def load_and_verify(folder):
    path=Path(folder)
    with (path/"signals.json").open("r",encoding="utf-8-sig") as f:
        signal_rows=json.load(f)
    with (path/"backup_manifest.json").open("r",encoding="utf-8-sig") as f:
        manifest=json.load(f)
    # Windows PowerShell ConvertTo-Json may export a single row as an object.
    if isinstance(signal_rows,dict):
        signal_rows=[signal_rows]
    return verify_backup(signal_rows,manifest)


if __name__=="__main__":
    if len(sys.argv)!=2:
        sys.exit("Usage: python verify_signal_backup.py BACKUP_FOLDER")
    try:
        verified=load_and_verify(sys.argv[1])
    except (OSError,ValueError,TypeError) as exc:
        sys.exit("BACKUP_NOT_VERIFIED: "+str(exc))
    print("BACKUP_VERIFIED: "+str(verified["saved_records"])+" signals; "+
          "BNB="+str(verified["bnb_present"])+
          "; BIRB="+str(verified["birb_present"])+
          "; exchange orders=0 (dry-run-only export)")
