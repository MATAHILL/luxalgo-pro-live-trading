"""SQLite durability guard for LUX DIRECT BOT's dedicated Render disk.

A paid Render web service is not enough: DB_PATH must be beneath an actual
mounted persistent disk and explicitly gated before any live certification.
This module never sends orders or handles exchange credentials.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3

def storage_health(db_path, *, require_persistence=False, mount_path="/var/data"):
    """Report storage readiness without revealing local paths or secrets."""
    if not isinstance(db_path, str) or not db_path or db_path == ":memory:":
        return {"durable":False,"reason":"NO_FILESYSTEM_LEDGER"}
    if not isinstance(mount_path,str) or not mount_path.startswith("/") or mount_path == "/":
        return {"durable":False,"reason":"INVALID_DISK_MOUNT"}
    path=Path(db_path)
    root=Path(mount_path)
    inside=path.is_absolute() and root in path.parents
    mounted=os.path.ismount(str(root))
    if not inside:
        return {"durable":False,"reason":"DB_NOT_ON_DEDICATED_RENDER_DISK"}
    if not mounted:
        return {"durable":False,"reason":"PERSISTENT_DISK_NOT_MOUNTED"}
    return {"durable":True,"reason":"DEDICATED_DISK_MOUNT_VERIFIED"}

@contextmanager
def open_ledger(db_path, *, require_persistence=False, mount_path="/var/data"):
    state=storage_health(db_path,require_persistence=require_persistence,mount_path=mount_path)
    if require_persistence and not state["durable"]:
        raise RuntimeError("Persistent trading ledger required: "+state["reason"])
    # Keep original SQLite development mode when no persistent storage is requested.
    if not db_path or db_path==":memory:":
        raise RuntimeError("Dedicated ledger file required")
    db=sqlite3.connect(db_path,timeout=20,isolation_level="DEFERRED")
    try:
        db.row_factory=sqlite3.Row
        db.execute("PRAGMA busy_timeout=20000")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def initialize_audit(db):
    """Never autorun entries on restart; store only distinct non-executable audit IDs."""
    db.execute("""CREATE TABLE IF NOT EXISTS execution_audit (
        id TEXT PRIMARY KEY,
        signal_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN (
            'BLOCKED','REQUESTED','ACK_UNKNOWN','PROTECTION_PENDING',
            'PROTECTED','EMERGENCY_HALT','CLOSED_VERIFIED')),
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL)""")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_audit_signal
        ON execution_audit(signal_id)""")
