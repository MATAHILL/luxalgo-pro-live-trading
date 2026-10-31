"""Offline verification of actual receiver export shapes. No network or secrets."""
import copy
import json
from pathlib import Path

import pytest
from verify_signal_backup import BackupUnverified, verify_backup, load_and_verify


def example():
    rows=[
        dict(id="a"*28,at=1793434600,symbol="BIRBUSDT",side="BUY",
             timeframe="5",bartime="1793434500000",
             event="bullish confirmation signal",status="DRY_RUN"),
        dict(id="b"*28,at=1793434900,symbol="BNBUSDT",side="SELL",
             timeframe="5",bartime="1793434800000",
             event="bearish confirmation signal",status="PAUSED"),
    ]
    manifest={
        "service":"LUX DIRECT BOT","mode":"DRY_RUN",
        "contains_exchange_orders":False,
        "server_total_signal_records":2,"saved_signal_records":2,
        "complete_signal_history_confirmed":True,
        "bnbusdt_record_present":True,
        "birbusdt_record_present":True,
    }
    return rows,manifest


def test_two_genuine_style_records_validate_without_claiming_orders(tmp_path):
    rows,manifest=example()
    assert verify_backup(rows,manifest)=={
        "complete_history_exported":True,
        "saved_records":2,"bnb_present":True,"birb_present":True,
        "contains_exchange_orders":False,"can_authorize_trading":False}
    (tmp_path/"signals.json").write_text(json.dumps(rows),encoding="utf-8")
    (tmp_path/"backup_manifest.json").write_text(json.dumps(manifest),encoding="utf-8")
    assert load_and_verify(tmp_path)["saved_records"]==2


@pytest.mark.parametrize("mutate",[
    lambda rows,m: m.update(complete_signal_history_confirmed=False),
    lambda rows,m: m.update(server_total_signal_records=31),
    lambda rows,m: m.update(saved_signal_records=1),
    lambda rows,m: m.update(contains_exchange_orders=True),
    lambda rows,m: m.update(birbusdt_record_present=False),
    lambda rows,m: rows.append(copy.deepcopy(rows[0])),
    lambda rows,m: rows[0].update(id="NOT_A_HASH"),
    lambda rows,m: rows[1].update(symbol="BNB_USDT"),
    lambda rows,m: rows[1].update(status="FILLED"),
    lambda rows,m: rows[0].update(event="stop loss"),
    lambda rows,m: rows[0].update(side="HOLD"),
    lambda rows,m: rows[1].update(timeframe="1"),
    lambda rows,m: rows[0].update(bartime="{time}"),
    lambda rows,m: rows[0].update(at=-1),
    lambda rows,m: rows[0].update(at=True),
])
def test_incomplete_or_malformed_backup_never_verified(mutate):
    rows,manifest=example()
    mutate(rows,manifest)
    with pytest.raises(BackupUnverified):
        verify_backup(rows,manifest)


def test_single_row_json_export_shape(tmp_path):
    rows,manifest=example()
    rows=rows[:1]
    manifest.update(server_total_signal_records=1,saved_signal_records=1,
                    bnbusdt_record_present=False)
    (tmp_path/"signals.json").write_text(json.dumps(rows[0]),encoding="utf-8")
    (tmp_path/"backup_manifest.json").write_text(json.dumps(manifest),encoding="utf-8")
    result=load_and_verify(tmp_path)
    assert result["saved_records"]==1
    assert result["birb_present"] is True
    assert result["bnb_present"] is False
