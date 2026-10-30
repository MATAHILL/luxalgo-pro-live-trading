"""Scanner scheduling regressions: mocked public data; never sends orders."""
import copy
import unittest
from unittest.mock import patch

import app as backend


class ScannerConcurrencyTests(unittest.TestCase):
    def setUp(self):
        with backend._scan_state_lock:
            self.original = copy.deepcopy(backend._scan_state)
            for state in backend._scan_state.values():
                state.update(at=0., running=False, results={}, scanned=0, failed=0, total=0)
        self.client = backend.app.test_client()

    def tearDown(self):
        with backend._scan_state_lock:
            backend._scan_state.clear()
            backend._scan_state.update(self.original)

    def test_initial_scan_starts_once_and_duplicate_refresh_is_throttled(self):
        with patch.object(backend, '_terminal_permitted', return_value=True), \
             patch.object(backend, 'Thread') as thread, \
             patch.object(backend.time, 'time', return_value=1000.0):
            response = self.client.get('/terminal/scanner?tf=5m&refresh=1')
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.get_json()['running'])
            thread.assert_called_once()
            thread.return_value.start.assert_called_once()
            with backend._scan_state_lock:
                backend._scan_state['5m']['running'] = False
                backend._scan_state['5m']['results'] = {'BTC_USDT': {'contract': 'BTC_USDT'}}
            again = self.client.get('/terminal/scanner?tf=5m&refresh=1')
            self.assertEqual(again.status_code, 200)
            self.assertFalse(again.get_json()['running'])
            self.assertEqual(len(again.get_json()['results']), 1)
            thread.assert_called_once()
            self.assertFalse(again.get_json()['orders_enabled'])

    def test_refresh_allowed_after_cooldown_not_before(self):
        with patch.object(backend, '_terminal_permitted', return_value=True), \
             patch.object(backend, 'Thread') as thread, \
             patch.object(backend.time, 'time', return_value=1000.0):
            with backend._scan_state_lock:
                backend._scan_state['5m'].update(at=960., running=False)
            self.client.get('/terminal/scanner?tf=5m&refresh=1')
            thread.assert_not_called()
            with backend._scan_state_lock:
                backend._scan_state['5m'].update(at=954., running=False)
            self.client.get('/terminal/scanner?tf=5m&refresh=1')
            thread.assert_called_once()

    def test_cross_timeframe_scan_exclusivity(self):
        with patch.object(backend, '_terminal_permitted', return_value=True), \
             patch.object(backend, 'Thread') as thread, \
             patch.object(backend.time, 'time', return_value=1000.0):
            with backend._scan_state_lock:
                backend._scan_state['15m']['running'] = True
            blocked = self.client.get('/terminal/scanner?tf=5m&refresh=1')
            self.assertEqual(blocked.status_code, 200)
            self.assertFalse(blocked.get_json()['running'])
            thread.assert_not_called()
            with backend._scan_state_lock:
                backend._scan_state['15m']['running'] = False
            allowed = self.client.get('/terminal/scanner?tf=5m&refresh=1')
            self.assertTrue(allowed.get_json()['running'])
            thread.assert_called_once()

    def test_nonforced_scan_reuses_recent_results(self):
        with patch.object(backend, '_terminal_permitted', return_value=True), \
             patch.object(backend, 'Thread') as thread, \
             patch.object(backend.time, 'time', return_value=1000.0):
            with backend._scan_state_lock:
                backend._scan_state['15m'].update(at=700., running=False)
            fresh = self.client.get('/terminal/scanner?tf=15m')
            self.assertEqual(fresh.status_code, 200)
            thread.assert_not_called()
            with backend._scan_state_lock:
                backend._scan_state['15m'].update(at=399., running=False)
            expired = self.client.get('/terminal/scanner?tf=15m')
            self.assertEqual(expired.status_code, 200)
            thread.assert_called_once()


if __name__ == '__main__':
    unittest.main()
