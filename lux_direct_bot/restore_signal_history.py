"""Import one owner-exported LUX DIRECT BOT backup into a mounted durable ledger.

CLI: python restore_signal_history.py OWNER_BACKUP.zip /var/data/lux-direct-bot.sqlite3
Must run on the new service AFTER Render mounts a dedicated disk and AFTER
schema initialization. This restores historical DRY_RUN/PAUSED webhook records
only. It NEVER resends them, changes operator state or calls Binance.

Do not commit ZIPs or signal data to GitHub. Private archival operation only.
"""
from __future__ import annotations
import json
from pathlib import Path
import sys
from zipfile import ZipFile, BadZipFile

from durable_sqlite import open_ledger
from verify_signal_backup import verify_backup

MAX_ARCHIVE_BYTES = 1_000_000
MAX_MEMBER_BYTES = 200_000
REQUIRED_FILES = {"backup_manifest.json", "signals.json"}
LEVEL_STATES = frozenset(("VALID", "INVALID", "NOT_PROVIDED"))
COLUMNS = ("id", "at", "symbol", "side", "timeframe", "bartime",
           "event", "status", "entry_reference", "stop_reference",
           "take_profit_reference", "levels_status")


def read_owner_archive(path):
    archive = Path(path)
    if not archive.is_file() or archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("Missing or oversized private owner backup")
    try:
        with ZipFile(archive, "r") as z:
            selected = {}
            for entry in z.infolist():
                if entry.filename in REQUIRED_FILES:
                    if entry.filename in selected:
                        raise ValueError("Duplicated manifest/signal entry")
                    if entry.file_size > MAX_MEMBER_BYTES:
                        raise ValueError("Oversized manifest/signal entry")
                    selected[entry.filename] = json.loads(z.read(entry).decode("utf-8-sig"))
    except (BadZipFile, UnicodeError, OSError) as exc:
        raise ValueError("Invalid private owner backup archive") from exc
    if set(selected) != REQUIRED_FILES:
        raise ValueError("Backup manifest and signals must both exist at archive root")
    signals = selected["signals.json"]
    if isinstance(signals, dict):
        signals = [signals]
    manifest = selected["backup_manifest.json"]
    verify_backup(signals, manifest)
    return signals


def restore_archive(zip_path, db_path, *, mount_path="/var/data"):
    records = read_owner_archive(zip_path)
    inserted = 0
    already = 0
    with open_ledger(db_path, require_persistence=True, mount_path=mount_path) as db:
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "signals" not in tables or "execution_audit" not in tables:
            raise RuntimeError("Production signal and audit schema must be initialized first")
        before_audit = db.execute("SELECT COUNT(*) FROM execution_audit").fetchone()[0]
        sql = ("INSERT OR IGNORE INTO signals ("+ ",".join(COLUMNS)+") VALUES ("+
               ",".join("?" for _ in COLUMNS)+")")
        for row in records:
            if row.get("levels_status", "NOT_PROVIDED") not in LEVEL_STATES:
                raise ValueError("Invalid signal reference level status")
            vals = tuple(row.get(k) if k != "levels_status" else row.get(k, "NOT_PROVIDED")
                         for k in COLUMNS)
            result = db.execute(sql, vals)
            if result.rowcount == 1:
                inserted += 1
                continue
            # Never silently overwrite a different record with the same fingerprint.
            previous = db.execute(
                "SELECT "+",".join(COLUMNS)+" FROM signals WHERE id=?",
                (row["id"],)).fetchone()
            if previous is None:
                raise RuntimeError("Signal insertion refused unexpectedly")
            if any(str(previous[k]) != str(v) for k,v in zip(COLUMNS, vals)):
                raise RuntimeError("Conflicting historical signal ID — fail closed")
            already += 1
        after_audit = db.execute("SELECT COUNT(*) FROM execution_audit").fetchone()[0]
        if after_audit != before_audit:
            raise RuntimeError("Historical restore modified execution audit")
    return {"verified_export": True, "restored": inserted,
            "already_present": already, "non_executable_history_only": True}


def main(args):
    if len(args) != 3:
        raise SystemExit("Usage: python restore_signal_history.py OWNER_BACKUP.zip /var/data/lux-direct-bot.sqlite3")
    try:
        result = restore_archive(args[1], args[2])
    except (ValueError, RuntimeError, OSError, TypeError) as exc:
        raise SystemExit("HISTORY_RESTORE_BLOCKED: "+str(exc)) from exc
    print("HISTORY_VERIFIED: "+str(result["restored"])+" restored; "+
          str(result["already_present"])+" already present; NO TRADES")


if __name__ == "__main__":
    main(sys.argv)
