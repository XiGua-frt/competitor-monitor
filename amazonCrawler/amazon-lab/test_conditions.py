import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from collector.conditions import load_conditions, check_page, require_region


class ConditionTests(unittest.TestCase):
    def setUp(self):
        self.config = load_conditions()
        self.html = '''<html lang="en-US"><div id="glow-ingress-line2">New York 10001</div>
        <input id="currencyOfPreference" value="USD"><input id="ASIN" value="B0DLQ8MVF7">
        <div id="variation_size_name"><select><option selected value="1">Medium</option></select></div>
        <div id="variation_color_name"><span class="selection">Black</span></div></html>'''

    def check(self, html=None, **kwargs):
        return check_page(html or self.html, 'https://www.amazon.com/dp/B0DLQ8MVF7',
                          'B0DLQ8MVF7', self.config, displayed_currency='USD', **kwargs)

    def test_matching_conditions_and_explicit_variant(self):
        result = self.check(expected_variant={'asin':'B0DLQ8MVF7','size':'Medium','color':'Black'})
        self.assertEqual(result['status'], 'matched')
        self.assertTrue(result['comparison_ready'])

    def test_discovery_records_variant_without_claiming_fixed_baseline(self):
        result = self.check()
        self.assertEqual(result['observed']['size'], 'Medium')
        self.assertFalse(result['comparison_ready'])
        self.assertEqual(result['variant_status'], 'not_fixed')

    def test_japan_and_misleading_zip_are_rejected(self):
        for location in ('Japan', 'New York 100010'):
            result = self.check(self.html.replace('New York 10001', location))
            self.assertIn('delivery_postcode_mismatch', result['reasons'])
            with self.assertRaises(ValueError):
                require_region(result)

    def test_missing_currency_evidence_is_not_verified(self):
        result = self.check(self.html.replace('id="currencyOfPreference"', 'id="other"'))
        self.assertFalse(result['region_verified'])

    def test_requested_asin_is_not_observed_asin(self):
        result = self.check(self.html.replace('value="B0DLQ8MVF7"', 'value="B0GWVTMR4Q"'))
        self.assertIn('asin_mismatch', result['reasons'])
        with self.assertRaises(ValueError):
            require_region(result)

    def test_missing_variant_does_not_invent_values(self):
        result = self.check('<html lang="en-US"><div id="glow-ingress-line2">10001</div>'
                            '<input id="currencyOfPreference" value="USD"></html>',
                            expected_variant={'asin':'B0DLQ8MVF7','size':'Medium','color':'Black'})
        self.assertIsNone(result['observed']['asin'])
        self.assertFalse(result['comparison_ready'])

    def test_fixed_size_change_is_detected(self):
        result = self.check(expected_variant={'asin':'B0DLQ8MVF7','size':'Large','color':'Black'})
        self.assertIn('size_mismatch', result['reasons'])
        self.assertFalse(result['comparison_ready'])

    def test_option_for_other_child_does_not_prove_variant(self):
        html = self.html.replace('selected value="1"', 'selected value="0##B0752XJYNL"')
        result = self.check(html, expected_variant={'asin':'B0DLQ8MVF7','size':'Medium','color':'Black'})
        self.assertIn('variant_evidence_conflict', result['reasons'])
        self.assertFalse(result['comparison_ready'])

    def test_foreign_domain_is_rejected(self):
        result = check_page(self.html, 'https://www.amazon.co.jp/dp/B0DLQ8MVF7',
                            'B0DLQ8MVF7', self.config)
        self.assertIn('marketplace_mismatch', result['reasons'])

    def embedded(self, dims, html=None):
        script = '<script>var state = ' + json.dumps({'sortedDimValuesForAllDims':dims}) + ';</script>'
        return (html or self.html).replace('</html>', script + '</html>')

    def test_embedded_selected_color_bound_to_current_asin(self):
        html = self.html.replace('<span class="selection">Black</span>', '')
        html = self.embedded({'color_name':[
            {'dimensionValueState':'AVAILABLE','dimensionValueDisplayText':'Navy','defaultAsin':'B0GWVTMR4Q'},
            {'dimensionValueState':'SELECTED','dimensionValueDisplayText':'Light Pink','defaultAsin':'B0DLQ8MVF7'}]}, html)
        result = self.check(html)
        self.assertEqual(result['observed']['color'], 'Light Pink')
        self.assertEqual(result['observed']['variant_evidence']['color']['source'], 'embedded_selected_dimension')
        self.assertEqual(result['observed']['variant_evidence']['color']['binding'], 'matched')

    def test_embedded_size_without_dom_control(self):
        html = '<html lang="en-US"><input id="ASIN" value="B0DLQ8MVF7"></html>'
        html = self.embedded({'size_name':[{'dimensionValueState':'SELECTED',
            'dimensionValueDisplayText':'Large','defaultAsin':'B0DLQ8MVF7'}]}, html)
        self.assertEqual(self.check(html)['observed']['size'], 'Large')

    def test_custom_composite_identity_keeps_observation_but_binding_unverified(self):
        html = self.html.replace('selected value="1"', 'selected value="0,MC_Custom_1##B0752XJYNL"')
        html = self.embedded({'color_name':[{'dimensionValueState':'SELECTED',
            'dimensionValueDisplayText':'Black','defaultAsin':'MC_Custom_1##B0752XJYNL'}]}, html)
        result = self.check(html, expected_variant={'asin':'B0DLQ8MVF7','size':'Medium','color':'Black'})
        self.assertEqual(result['observed']['color'], 'Black')
        self.assertIn('variant_binding_unverified', result['reasons'])
        self.assertFalse(result['comparison_ready'])

    def test_conflicting_dom_and_json_are_not_silently_confirmed(self):
        html = self.embedded({'color_name':[{'dimensionValueState':'SELECTED',
            'dimensionValueDisplayText':'Pink','defaultAsin':'B0DLQ8MVF7'}]})
        result = self.check(html, expected_variant={'asin':'B0DLQ8MVF7','size':'Medium','color':'Black'})
        self.assertIn('variant_value_conflict', result['reasons'])
        self.assertFalse(result['comparison_ready'])

    def test_malformed_json_and_available_values_do_not_invent_selection(self):
        html = self.html.replace('<span class="selection">Black</span>', '')
        html = html.replace('</html>', '<script>"sortedDimValuesForAllDims":{ broken }</script></html>')
        html = self.embedded({'color_name':[{'dimensionValueState':'AVAILABLE',
            'dimensionValueDisplayText':'Pink','defaultAsin':'B0DLQ8MVF7'}]}, html)
        self.assertIsNone(self.check(html)['observed']['color'])


if __name__ == '__main__':
    unittest.main()
