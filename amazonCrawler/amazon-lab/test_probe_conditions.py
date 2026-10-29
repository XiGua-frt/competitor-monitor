import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('amazon_probe', Path(__file__).with_name('probe.py'))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class StopConditionsTests(unittest.TestCase):
    def run_saved_listings(self, listings):
        calls = []
        def get(url, **kwargs):
            calls.append(url)
            asin = url.split('/dp/')[1].split('?')[0]
            html = ('<html lang="en-US"><div id="glow-ingress-line2">New York 10001</div>'
                    '<input id="currencyOfPreference" value="USD"><input id="ASIN" value="'+asin+'">'
                    '<div id="feature-bullets"><li><span class="a-list-item">Upload a pet portrait photo for your shirt.</span></li></div></html>')
            return SimpleNamespace(status_code=200, url=url, content=html.encode(), text=html)
        transport = SimpleNamespace(get=get, headers={'User-Agent':'test'},
                                    cookies=SimpleNamespace(set=lambda *a, **k:None))
        scraper = SimpleNamespace(session=SimpleNamespace(session=transport), search_products=lambda **kwargs:listings)
        def details(url):
            transport.get(url)
            return {'asin':url.split('/dp/')[1], 'title':'Custom Photo T Shirt'}
        scraper.get_product_details = details
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            state = folder/'state.json'
            state.write_text('{"cookies":[]}', encoding='utf-8')
            with patch.object(probe, 'OUT', folder/'output'), patch.object(probe, 'session_path', return_value=state), \
                    patch.object(probe, 'AmazonScraper', return_value=scraper):
                code = probe.run()
            data = {name:json.loads((folder/'output'/name).read_text(encoding='utf-8'))
                    for name in ('report.json','posts.json','discoveries.json')}
        return code, calls, data

    def test_unrelated_listing_not_requested_and_detail_rechecks_pending(self):
        code, calls, data = self.run_saved_listings([
            {'asin':'B000000001','title':'Custom Pet Photo Mug'},
            {'asin':'B000000002','title':'Custom Photo T Shirt'},
            {'asin':'B000000003','title':'Custom Pet Portrait T Shirt'}])
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        self.assertFalse(any('B000000001' in url for url in calls))
        self.assertEqual([r['search']['selected_result_position'] for r in data['posts.json']], [2,3])
        self.assertTrue(all(r['relevance']['status']=='related' for r in data['posts.json']))
        self.assertEqual(data['discoveries.json'][0]['decision'], 'skip_unrelated')

    def test_all_unrelated_is_valid_empty_result(self):
        code, calls, data = self.run_saved_listings([{'asin':'B000000001','title':'Custom Pet Photo Mug'}])
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])
        self.assertEqual(data['posts.json'], [])
        self.assertEqual(data['report.json']['status'], 'no_eligible_candidates')

    def test_region_failure_stops_before_second_request(self):
        calls = []
        def get(url, **kwargs):
            calls.append(url)
            return SimpleNamespace(status_code=200, url=url, content=b'test',
                text='<html lang="en-US"><div id="glow-ingress-line2">Japan</div>'
                     '<input id="currencyOfPreference" value="USD"></html>')
        transport = SimpleNamespace(get=get, headers={'User-Agent':'test'},
                                    cookies=SimpleNamespace(set=lambda *a, **k:None))
        scraper = SimpleNamespace(session=SimpleNamespace(session=transport))
        def search(**kwargs):
            transport.get('https://www.amazon.com/s?k=test')
            transport.get('https://www.amazon.com/dp/B0DLQ8MVF7')
            return []
        scraper.search_products = search
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            state = folder/'state.json'
            state.write_text('{"cookies":[]}', encoding='utf-8')
            with patch.object(probe, 'OUT', folder/'output'), patch.object(probe, 'session_path', return_value=state), \
                    patch.object(probe, 'AmazonScraper', return_value=scraper):
                self.assertEqual(probe.run(), 1)
            report = json.loads((folder/'output/report.json').read_text(encoding='utf-8'))
            self.assertEqual(report['status'], 'stopped')
            self.assertIn('delivery_postcode_mismatch', report['stop_reason'])
            self.assertEqual(len(calls), 1)

    def test_missing_session_stops_without_initializing_scraper(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            with patch.object(probe, 'OUT', folder/'output'), \
                    patch.object(probe, 'session_path', return_value=folder/'missing.json'), \
                    patch.object(probe, 'AmazonScraper') as scraper:
                self.assertEqual(probe.run(), 1)
                scraper.assert_not_called()


if __name__ == '__main__':
    unittest.main()
