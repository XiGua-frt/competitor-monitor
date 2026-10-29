import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('amazon_probe', Path(__file__).with_name('probe.py'))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
preferences_spec = importlib.util.spec_from_file_location('prepare_preferences', Path(__file__).with_name('prepare_preferences.py'))
preferences = importlib.util.module_from_spec(preferences_spec)
preferences_spec.loader.exec_module(preferences)

class PreferenceTests(unittest.TestCase):
    def test_homepage_without_currency_label_accepts_usd_preference_cookie(self):
        self.assertTrue(preferences.preferences_ready('New York 10001',None,'USD'))
        self.assertFalse(preferences.preferences_ready('Japan',None,'USD'))
        self.assertFalse(preferences.preferences_ready('New York 10001',None,'JPY'))
    def test_shipping_restriction_not_mistaken_for_variant_price(self):
        html = '''<div id="glow-ingress-line2">Japan</div>
            <input id="currencyOfPreference" value="JPY">
            <div id="availability">This item cannot be shipped to your selected delivery location.</div>
            <div class="recommendation"><span class="a-price-whole">1,542</span></div>'''
        row = probe.normalize({'asin':'B0GWVTMR4Q'}, {}, html, 1)
        self.assertIsNone(row['commerce']['price'])
        self.assertEqual(row['evidence']['price_missing_reason'], 'delivery_location_unsupported')
        self.assertFalse(row['evidence']['us_preferences_verified'])

    def test_usd_price_requires_us_delivery_evidence(self):
        html = '''<div id="glow-ingress-line2">New York 10001</div>
            <input id="currencyOfPreference" value="USD">
            <span id="apex-pricetopay-accessibility-label">$15.99</span>'''
        row = probe.normalize({'asin':'B0DLQ8MVF7'}, {}, html, 1)
        self.assertEqual(row['commerce']['price'], 15.99)
        self.assertEqual(row['commerce']['currency'], 'USD')
        self.assertTrue(row['evidence']['us_preferences_verified'])

if __name__ == '__main__':
    unittest.main()
