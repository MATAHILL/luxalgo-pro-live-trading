"""Owner signal history restore: only historical DRY_RUN records, never executions."""
import json
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import pytest

from durable_sqlite import initialize_audit, open_ledger
from restore_signal_history import read_owner_archive, restore_archive


def archive_fixture(path, rows=None, manifest_updates=None):
    rows = list(rows) if rows is not None else [
        {"id":"a"*28,"at":1793375400,"symbol":"BNBUSDT","side":"BUY",
         "timeframe":"5","bartime":"1793375400000",
         "event":"bullish confirmation signal","status":"DRY_RUN",
         "entry_reference":None,"stop_reference":None,
         "take_profit_reference":None,"levels_status":"NOT_PROVIDED"},
        {"id":"b"*28,"at":1793375700,"symbol":"BIRBUSDT","side":"SELL",
         "timeframe":"5","bartime":"1793375700000",
         "event":"bearish confirmation signal","status":"PAUSED",
         "entry_reference":None,"stop_reference":None,
         "take_profit_reference":None,"levels_status":"NOT_PROVIDED"},
    ]
    manifest={
        "service":"LUX DIRECT BOT","mode":"DRY_RUN",
        "contains_exchange_orders":False,
        "server_total_signal_records":len(rows),
        "saved_signal_records":len(rows),
        "complete_signal_history_confirmed":True,
        "bnbusdt_record_present":any(x["symbol"]=="BNBUSDT" for x in rows),
        "birbusdt_record_present":any(x["symbol"]=="BIRBUSDT" for x in rows),
    }
    if manifest_updates:
        manifest.update(manifest_updates)
    with ZipFile(path,"w") as z:
        z.writestr("signals.json",json.dumps(rows))
        z.writestr("backup_manifest.json",json.dumps(manifest))
    return rows


def setup_db(disk):
    dbpath=str(disk/"ledger.sqlite3")
    with open_ledger(dbpath,require_persistence=True,mount_path=str(disk)) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS signals (
            id TEXT PRIMARY KEY,at INTEGER,symbol TEXT,side TEXT,timeframe TEXT,
            bartime TEXT,event TEXT,status TEXT,entry_reference TEXT,stop_reference TEXT,
            take_profit_reference TEXT,levels_status TEXT)""")
        initialize_audit(db)
    return dbpath


def test_unchanged_owner_history_import_is_idempotent_and_nonexecuting(tmp_path):
    disk=tmp_path/"disk"
    disk.mkdir()
    archive=tmp_path/"owner.zip"
    archive_fixture(archive)
    with patch("durable_sqlite.os.path.ismount",return_value=True):
        dbpath=setup_db(disk)
        first=restore_archive(archive,dbpath,mount_path=str(disk))
        assert first=={"verified_export":True,"restored":2,
                       "already_present":0,"non_executable_history_only":True}
        second=restore_archive(archive,dbpath,mount_path=str(disk))
        assert second["restored"]==0 and second["already_present"]==2
        with open_ledger(dbpath,require_persistence=True,mount_path=str(disk)) as db:
            rows=db.execute("SELECT status FROM signals ORDER BY at").fetchall()
            assert [x[0] for x in rows]==["DRY_RUN","PAUSED"]
            assert db.execute("SELECT COUNT(*) FROM execution_audit").fetchone()[0]==0


def test_restore_requires_actual_mounted_disk(tmp_path):
    disk=tmp_path/"not-mounted"
    disk.mkdir()
    archive=tmp_path/"owner.zip"
    archive_fixture(archive)
    dbpath=str(disk/"ledger.sqlite3")
    with pytest.raises(RuntimeError,match="PERSISTENT_DISK_NOT_MOUNTED"):
        restore_archive(archive,dbpath,mount_path=str(disk))
    assert not (disk/"ledger.sqlite3").exists()


def test_incomplete_backup_rejected_before_database_access(tmp_path):
    archive=tmp_path/"owner.zip"
    archive_fixture(archive,manifest_updates={"complete_signal_history_confirmed":False})
    with pytest.raises(ValueError,match="Receiver did not certify"):
        restore_archive(archive,str(tmp_path/"never-open.sqlite"))


def test_same_id_conflicting_record_blocks_entire_transaction(tmp_path):
    disk=tmp_path/"disk"
    disk.mkdir()
    archive=tmp_path/"owner.zip"
    archive_fixture(archive)
    with patch("durable_sqlite.os.path.ismount",return_value=True):
        dbpath=setup_db(disk)
        with open_ledger(dbpath,require_persistence=True,mount_path=str(disk)) as db:
            db.execute("INSERT INTO signals(id,at,symbol,status) VALUES(?,?,?,?)",
                       ("b"*28,1234,"BIRBUSDT","PAUSED"))
        with pytest.raises(RuntimeError,match="Conflicting historical signal"):
            restore_archive(archive,dbpath,mount_path=str(disk))
        with open_ledger(dbpath,require_persistence=True,mount_path=str(disk)) as db:
            assert db.execute("SELECT COUNT(*) FROM signals").fetchone()[0]==1
            assert db.execute("SELECT COUNT(*) FROM execution_audit").fetchone()[0]==0


def test_zero_or_single_row_archive_must_not_fabricate_bnb_birb(tmp_path):
    archive=tmp_path/"owner.zip"
    archive_fixture(archive,rows=[
        {"id":"a"*28,"at":1793375400,"symbol":"XRPUSDT","side":"BUY",
         "timeframe":"5","bartime":"1793375400000",
         "event":"bullish confirmation","status":"DRY_RUN"},
    ])
    entries=read_owner_archive(archive)
    assert len(entries)==1 and entries[0]["symbol"]=="XRPUSDT"
