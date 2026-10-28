"""Regression tests for production market-data separation from Testnet/order execution.

Run: python -m unittest discover -s tests -p 'test_live_market_cutover.py'
Tests use mocked exchange data and never submit orders.
"""
import io
import os
import unittest
from unittest.mock import patch
from urllib.error import URLError

import app as backend
from production_cutover_view import production_monitor_page


class ProductionCutoverTests(unittest.TestCase):
    def test_production_page_retains_both_scanners_and_news(self):
        html = production_monitor_page(backend.PAGE)
        for anchor in ('radarBody', 'scannerBody', 'advancedScanBody',
                       'newsStack', 'radarRefresh', 'scannerRefresh', 'advancedRefresh'):
            self.assertIn('id="' + anchor + '"', html)
        self.assertNotIn('id="botTurnOn"', html)
        self.assertNotIn('id="v4Workspace"', html)
        # Historical failure: retired Testnet buttons were removed from HTML,
        # then an unguarded listener stopped the whole market dashboard script.
        self.assertIn('if(!on||!off||!label||!detail)return;', html)

    def test_public_radar_works_with_all_order_credentials_absent(self):
        ticker = {'contract': 'BTC_USDT', 'last': '80000',
                  'change_percentage': '1.2', 'volume_24h_quote': '50000000'}
        with patch.dict(os.environ, {'GATE_API_KEY': '', 'GATE_API_SECRET': '',
                                     'GATE_TESTNET_API_KEY': '', 'GATE_TESTNET_API_SECRET': '',
                                     'LIVE_TRADING_ENABLED': 'false', 'ORDERS_ENABLED': 'false'}):
            with patch.object(backend, '_scan_http_json', return_value=[ticker]):
                with backend._radar_lock:
                    backend._radar_cache.update(data=None, at=0)
                payload = backend._radar_data()
        self.assertEqual(payload['total'], 1)
        self.assertEqual(payload['results'][0]['contract'], 'BTC_USDT')
        self.assertFalse(payload['orders_enabled'])

    def test_market_fallback_uses_only_live_gate_hosts(self):
        urls = []
        def fetch(req, timeout=0):
            urls.append(req.full_url)
            if len(urls) == 1:
                raise URLError('transient outage')
            return io.BytesIO(b'[]')
        with patch.object(backend, 'urlopen', side_effect=fetch):
            self.assertEqual(backend._scan_http_json('/api/v4/futures/usdt/tickers'), [])
        self.assertEqual(len(urls), 2)
        self.assertTrue(urls[0].startswith('https://api.gateio.ws/'))
        self.assertTrue(urls[1].startswith('https://fx-api.gateio.ws/'))
        self.assertTrue(all('testnet' not in url.lower() for url in urls))

    def test_market_feed_denies_order_and_account_paths(self):
        for path in ('/api/v4/futures/usdt/orders',
                     '/api/v4/futures/usdt/accounts',
                     'https://example.com/api/v4/futures/usdt/tickers'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                backend._scan_http_json(path)


if __name__ == '__main__':
    unittest.main()
