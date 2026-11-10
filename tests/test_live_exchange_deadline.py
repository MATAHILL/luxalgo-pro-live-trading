"""Offline wire-level checks for Gate's server-enforced entry expiry."""
import io
import json
import os
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import live_futures_engine as engine

NOW = 1800000000
PLAN = dict(contract='BTC_USDT', contracts=1, side='LONG',
            client_text='t-offline-expiry', received_epoch=NOW-10)
FLAGS = dict(LIVE_TRADING_ENABLED='true', ORDERS_ENABLED='true',
             LIVE_ORDERS_SEPARATELY_AUTHORIZED='true')


class ExchangeDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        def transport(request, **kwargs):
            self.calls.append(request)
            return io.BytesIO(json.dumps({'id': '123'}).encode())
        self.client = engine.GateLiveClient('fake-key', 'fake-secret', transport)
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(engine, 'EXCHANGE_WRITE_RELEASE', True))
        stack.enter_context(patch.object(engine.time, 'time', return_value=NOW))
        stack.enter_context(patch.dict(os.environ, FLAGS))

    def header(self):
        return dict((k.lower(), v) for k, v in self.calls[-1].header_items())

    def test_entry_sends_original_signal_deadline_on_wire(self):
        self.client.submit_entry(PLAN)
        self.assertEqual(self.header()['x-gate-exptime'], str((NOW+50)*1000))
        self.assertEqual(json.loads(self.calls[0].data)['size'], 1)

    def test_review_deadline_can_shorten_but_never_extend_signal(self):
        for review, expected in ((NOW+8, NOW+8), (NOW+100, NOW+50)):
            self.client.submit_entry(dict(PLAN, submission_expires_epoch=review))
            self.assertEqual(self.header()['x-gate-exptime'], str(expected*1000))

    def test_invalid_or_elapsed_entry_never_reaches_transport(self):
        for extra in ({'received_epoch': NOW-60}, {'received_epoch': NOW+1},
                      {'received_epoch': True}, {'received_epoch': None},
                      {'submission_expires_epoch': NOW},
                      {'submission_expires_epoch': '1800000001'}):
            with self.subTest(extra=extra), self.assertRaises(engine.LiveSafetyError):
                self.client.submit_entry(dict(PLAN, **extra))
        self.assertEqual(self.calls, [])

    def test_transport_rechecks_deadline_after_order_construction(self):
        with patch.object(engine.time, 'time', side_effect=[NOW, NOW+51]):
            with self.assertRaisesRegex(engine.LiveSafetyError, 'deadline'):
                self.client.submit_entry(PLAN)
        self.assertEqual(self.calls, [])

    def test_deadline_rejected_on_get_trigger_or_invalid_value(self):
        for method, path, value in (
            ('GET', '/accounts', (NOW+1)*1000),
            ('POST', '/price_orders', (NOW+1)*1000),
            ('POST', '/orders', True),
            ('POST', '/orders', str((NOW+1)*1000)),
            ('POST', '/orders', (NOW+61)*1000)):
            with self.subTest(method=method, path=path, value=value):
                with self.assertRaises(engine.LiveSafetyError):
                    self.client.request(method, engine.PREFIX+path,
                        payload={} if method == 'POST' else None, expires_at_ms=value)
        self.assertEqual(self.calls, [])

    def test_recovery_post_has_no_inherited_entry_expiry(self):
        self.client.request('POST', engine.PREFIX+'/orders', payload={
            'contract': 'BTC_USDT', 'size': -1, 'reduce_only': True})
        self.assertNotIn('x-gate-exptime', self.header())

    def test_source_and_environment_locks_remain_effective(self):
        with patch.object(engine, 'EXCHANGE_WRITE_RELEASE', False):
            with self.assertRaisesRegex(engine.LiveSafetyError, 'SOURCE_LOCKED'):
                self.client.submit_entry(PLAN)
        with patch.dict(os.environ, {'ORDERS_ENABLED': 'false'}):
            with self.assertRaisesRegex(engine.LiveSafetyError, 'authorization'):
                self.client.submit_entry(PLAN)
        self.assertEqual(self.calls, [])

    def test_unknown_ack_is_not_retried(self):
        def lost(request, **kwargs):
            self.calls.append(request)
            raise TimeoutError('offline injected timeout')
        self.client.transport = lost
        with self.assertRaises(engine.AckUnknown):
            self.client.submit_entry(PLAN)
        self.assertEqual(len(self.calls), 1)


if __name__ == '__main__':
    unittest.main()
