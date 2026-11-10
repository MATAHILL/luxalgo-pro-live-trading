"""HTTP planning while halted; exchange reads mocked, real risk math retained."""
import os
import unittest
from contextlib import ExitStack, nullcontext
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import app as backend
import live_futures_engine as engine
import live_execution_coordinator as coordinator


class HaltedPreviewTests(unittest.TestCase):
    def run_preview(self, *, age=0, equity='72.30675274', authorized=True,
                    csrf=True, event='a'*20):
        row=dict(event_id='a'*20, received_at=datetime.now(timezone.utc)-timedelta(seconds=age),
                 symbol='BTCUSDT', side='LONG', timeframe='5m', levels_status='VALID',
                 entry_price='80000', stop_loss='79000', take_profit='82000', order_type='MARKET')
        con=MagicMock()
        con.cursor.return_value.__enter__.return_value.fetchone.return_value=row
        client=MagicMock()
        client.snapshot.return_value=dict(account=dict(currency='USDT', total=equity,
            available=equity, unrealised_pnl='0', position_mode='dual'), positions=[])
        client.request.return_value=dict(name='BTC_USDT', quanto_multiplier='0.0001',
            order_size_min='1', order_size_max='100000', taker_fee_rate='0.00075')
        with ExitStack() as stack:
            stack.enter_context(patch.object(backend, '_terminal_permitted', return_value=authorized))
            stack.enter_context(patch.object(backend, '_terminal_guard', return_value=({'error':'auth'},401)))
            stack.enter_context(patch.object(backend, '_csrf_valid', return_value=csrf))
            stack.enter_context(patch.object(backend, '_db_connect', return_value=nullcontext(con)))
            stack.enter_context(patch.object(backend, '_ensure_schema'))
            stack.enter_context(patch.object(coordinator, 'install'))
            stack.enter_context(patch.object(coordinator, 'control', return_value={'halted':True}))
            halt=stack.enter_context(patch.object(coordinator, 'halt'))
            stack.enter_context(patch.object(coordinator, 'read_rows', return_value=[]))
            stack.enter_context(patch.object(engine, 'GateLiveClient', return_value=client))
            stack.enter_context(patch.dict(os.environ, {'LUXALGO_LIVE_RISK_PERCENT':'0.5'}))
            response=backend.app.test_client().post('/terminal/live-execution/plan', json={'event_id':event})
        halt.assert_not_called()
        client.submit_entry.assert_not_called()
        client.submit_trigger.assert_not_called()
        for call in client.request.call_args_list:
            self.assertEqual(call.args[0], 'GET')
        return response, client, con

    def test_halt_remains_active_with_nonexecutable_risk_preview(self):
        response, _, _=self.run_preview()
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['status'],'PREVIEW_ONLY_NOT_EXECUTABLE')
        self.assertTrue(response.json['halt_active'])
        self.assertFalse(response.json['orders_enabled'])
        self.assertFalse(response.json['live_trading'])
        self.assertFalse(response.json['plan']['verified_live_order'])
        self.assertEqual(response.json['orders_submitted'],0)
        self.assertEqual(response.json['plan']['contracts'],2)

    def test_stale_signal_still_rejected(self):
        response, _, _=self.run_preview(age=120)
        self.assertEqual(response.status_code,503)
        self.assertFalse(response.json['orders_enabled'])

    def test_empty_equity_still_rejected(self):
        response, _, _=self.run_preview(equity='0')
        self.assertEqual(response.status_code,503)

    def test_auth_csrf_and_event_validation_precede_exchange_reads(self):
        for params, status in [({'authorized':False},401),({'csrf':False},403),({'event':'bad'},422)]:
            with self.subTest(params=params):
                response, client, con=self.run_preview(**params)
                self.assertEqual(response.status_code,status)
                client.snapshot.assert_not_called()
                con.cursor.assert_not_called()


if __name__=='__main__':
    unittest.main()
