"""Gate.io one-trade lifecycle controller (development, NOT live-enabled).

An exchange-facing coordinator with durable one-trade state, explicit states for
uncertain order acknowledgments and protective-order verification. Network writes
remain blocked; the adapter is deliberately injected for offline integration.
This is NOT a complete live execution service or an automatic emergency closer.
"""
from __future__ import annotations
from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import sqlite3
from pathlib import Path
from typing import Callable

LIVE_ORDER_SUBMISSION_ENABLED = False

class ExecutionNotReady(RuntimeError): pass

@dataclass(frozen=True)
class TradeRequest:
    event_id: str
    contract: str
    side: str
    entry: str
    stop: str
    take: str

class SingleTradeLedger:
    """Persistent singleton; does not release the lock when a trade closes."""
    def __init__(self, database_path: str):
        self.path = str(Path(database_path))
        with self._connect() as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS one_trade
              (singleton INTEGER PRIMARY KEY CHECK(singleton=1),
               event_id TEXT NOT NULL UNIQUE, state TEXT NOT NULL,
               exchange_order_id TEXT, updated_at TEXT NOT NULL)''')

    @contextmanager
    def _connect(self):
        """Commit/rollback and CLOSE connection before Windows temp-file cleanup."""
        db = sqlite3.connect(self.path, timeout=10)
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def reserve(self, request: TradeRequest):
        if not request.event_id or not request.contract.endswith('_USDT') or request.side not in ('LONG','SHORT'):
            raise ValueError('Invalid signal identity')
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM one_trade WHERE singleton=1').fetchone():
                return 'BLOCKED_ONE_TRADE_ALREADY_USED'
            db.execute('INSERT INTO one_trade VALUES (1,?,?,NULL,?)',
                       (request.event_id,'RESERVED',datetime.now(timezone.utc).isoformat()))
            return 'RESERVED'

    def transition(self, event_id: str, new_state: str, exchange_order_id=None):
        allowed = {
            'RESERVED': {'ACK_UNKNOWN','ACKNOWLEDGED','HALTED'},
            'ACK_UNKNOWN': {'REVIEW_REQUIRED','HALTED'},
            'ACKNOWLEDGED': {'PROTECTION_UNVERIFIED','PROTECTED','HALTED'},
            'PROTECTION_UNVERIFIED': {'PROTECTED','REVIEW_REQUIRED','HALTED'},
            'PROTECTED': {'CLOSED_VERIFIED','REVIEW_REQUIRED','HALTED'},
            'REVIEW_REQUIRED': {'HALTED'},
        }
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT state,event_id,exchange_order_id FROM one_trade WHERE singleton=1').fetchone()
            if not row or row[1]!=event_id or new_state not in allowed.get(row[0],set()):
                raise ExecutionNotReady('Invalid trade lifecycle transition')
            db.execute('UPDATE one_trade SET state=?,exchange_order_id=?,updated_at=? WHERE singleton=1',
                       (new_state, exchange_order_id or row[2], datetime.now(timezone.utc).isoformat()))
            return new_state

    def status(self):
        with self._connect() as db:
            row=db.execute('SELECT event_id,state,exchange_order_id FROM one_trade WHERE singleton=1').fetchone()
        return {'state':row[1] if row else 'NOT_STARTED','event_id':row[0] if row else None,
                'exchange_order_id':row[2] if row else None,
                'second_trade_allowed':False if row else True,
                'live_trading':False}

def submit_exchange_entry(*args, **kwargs):
    """Do not remove this lock: entry+exits are not operational or exchange-tested."""
    raise ExecutionNotReady('Real trading blocked: exchange-side exits and emergency recovery not verified')

def reconcile_entry_ack(ledger: SingleTradeLedger, event_id: str, response: dict | None):
    """An HTTP error/timeout is UNKNOWN, never evidence of no order."""
    if not isinstance(response,dict) or not response.get('id'):
        ledger.transition(event_id,'ACK_UNKNOWN')
        return 'ACK_UNKNOWN_DO_NOT_RETRY'
    ledger.transition(event_id,'ACKNOWLEDGED',str(response['id']))
    return 'ACKNOWLEDGED_PROTECTION_UNVERIFIED'

def self_test():
    from tempfile import TemporaryDirectory
    with TemporaryDirectory() as folder:
        # Windows: SQLite file connections are explicitly closed before cleanup.
        db=SingleTradeLedger(str(Path(folder)/'ledger.sqlite'))
        request=TradeRequest('TEST-LONG','BTC_USDT','LONG','80000','79000','81000')
        assert db.reserve(request)=='RESERVED'
        assert db.reserve(request)=='BLOCKED_ONE_TRADE_ALREADY_USED'
        assert reconcile_entry_ack(db,request.event_id,{'id':123})=='ACKNOWLEDGED_PROTECTION_UNVERIFIED'
        assert db.status()['state']=='ACKNOWLEDGED'
        assert db.transition(request.event_id,'PROTECTION_UNVERIFIED')=='PROTECTION_UNVERIFIED'
        assert db.transition(request.event_id,'REVIEW_REQUIRED')=='REVIEW_REQUIRED'
        try: db.transition(request.event_id,'CLOSED_VERIFIED')
        except ExecutionNotReady: pass
        else: raise AssertionError('Unverified closure incorrectly accepted')
        try: submit_exchange_entry(request)
        except ExecutionNotReady: pass
        else: raise AssertionError('Order dispatch must stay locked')
        with db._connect() as con:
            assert con.execute('SELECT COUNT(*) FROM one_trade').fetchone()[0]==1
    with TemporaryDirectory() as folder:
        db=SingleTradeLedger(str(Path(folder)/'ledger.sqlite'))
        assert db.reserve(TradeRequest('TEST-SHORT','ETH_USDT','SHORT','100','105','95'))=='RESERVED'
        assert reconcile_entry_ack(db,'TEST-SHORT',None)=='ACK_UNKNOWN_DO_NOT_RETRY'
        assert db.status()['state']=='ACK_UNKNOWN'
    return {'persistent_one_trade_lock':'PASSED',
            'acknowledgment_unknown_fail_closed':'PASSED',
            'missing_protection_halts_progress':'PASSED',
            'windows_sqlite_cleanup':'PASSED',
            'real_gate_orders':'BLOCKED',
            'live_ready':False}

if __name__=='__main__':
    print(json.dumps(self_test(),indent=2))
