"""Expected conditions and independently observed page evidence."""
import json
import re
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup

CONFIG_PATH = Path(__file__).resolve().parent.parent / 'collection_conditions.json'


def load_conditions(path=CONFIG_PATH):
    config = json.loads(Path(path).read_text(encoding='utf-8'))
    if (config['schema_version'] != 1 or config['marketplace'] != 'www.amazon.com'
            or config['country_code'] != 'com' or config['currency'] != 'USD'
            or config['language'] != 'en-US'
            or not re.fullmatch(r'\d{5}', config['delivery_postcode'])
            or config['variant_policy'] != 'record_selected_then_fix_per_product'):
        raise ValueError('unsupported_collection_conditions')
    for asin, variant in config['tracked_variants'].items():
        if not re.fullmatch(r'[A-Z0-9]{10}', asin) or variant.get('asin') != asin:
            raise ValueError('invalid_tracked_variant')
    return config


def session_path(config):
    return (CONFIG_PATH.parent / config['session_file']).resolve()


def selected_dimensions(soup, asin):
    """Decode JSON values only; never execute page scripts or infer from titles."""
    candidates = {'size':[], 'color':[], 'fit_type':[]}
    keys = {'size_name':'size', 'color_name':'color', 'fit_type':'fit_type'}
    decoder = json.JSONDecoder()
    for script in soup.find_all('script'):
        script_text = script.string or script.get_text()
        for match in re.finditer(r'"sortedDimValuesForAllDims"\s*:', script_text):
            try:
                dims = decoder.raw_decode(script_text[match.end():].lstrip())[0]
            except (ValueError, TypeError):
                continue
            if not isinstance(dims, dict):
                continue
            for dimension, field in keys.items():
                values = dims.get(dimension, [])
                if not isinstance(values, list):
                    continue
                for item in values:
                    if not isinstance(item, dict) or item.get('dimensionValueState') != 'SELECTED':
                        continue
                    value, identity = item.get('dimensionValueDisplayText'), item.get('defaultAsin')
                    if not isinstance(value, str) or not value.strip():
                        continue
                    binding = ('matched' if identity and identity == asin else
                               'unverified' if not identity or str(identity).startswith('MC_Custom_') else 'mismatch')
                    evidence = {'value':value.strip(), 'source':'embedded_selected_dimension',
                                'associated_identity':identity, 'binding':binding}
                    if evidence not in candidates[field]:
                        candidates[field].append(evidence)
    return candidates


def check_page(html, page_url, requested_asin, config, displayed_currency=None,
               expected_variant=None):
    soup = BeautifulSoup(html, 'lxml')
    def content(selector):
        node = soup.select_one(selector)
        return node.get_text(' ', strip=True) or None if node else None

    asin_node = soup.select_one('input#ASIN, input[name="ASIN"]')
    asin = asin_node.get('value') if asin_node else None
    currency_node = soup.select_one('#currencyOfPreference')
    currency = currency_node.get('value') if currency_node else None
    language_node = soup.select_one('html[lang]')
    language = language_node.get('lang') if language_node else None
    delivery = content('#glow-ingress-line2')
    option = soup.select_one('#variation_size_name option[selected]')
    size = content('#variation_size_name .selection')
    if size is None and option and option.get('value') not in ('-1', ''):
        size = option.get_text(' ', strip=True) or None
    color = content('#variation_color_name .selection')
    variant_evidence = {}
    variant_reasons = []
    values = {'size':size, 'color':color, 'fit_type':content('#variation_fit_type .selection')}
    for field, candidates in selected_dimensions(soup, asin).items():
        dom_value = values[field]
        if candidates:
            different_values = {c['value'] for c in candidates}
            if len(different_values) > 1 or (dom_value and dom_value not in different_values):
                variant_reasons.append('variant_value_conflict')
                variant_evidence[field] = {'source':'conflicting_sources', 'dom_value':dom_value, 'candidates':candidates}
            else:
                values[field] = candidates[0]['value']
                variant_evidence[field] = dict(candidates[0])
            if any(c['binding'] == 'mismatch' for c in candidates):
                variant_reasons.append('variant_evidence_conflict')
            if any(c['binding'] == 'unverified' for c in candidates):
                variant_reasons.append('variant_binding_unverified')
        elif dom_value:
            variant_evidence[field] = {'value':dom_value, 'source':'selected_page_control'}
        else:
            variant_evidence[field] = {'value':None, 'source':None, 'reason':'selected_value_not_found'}
    observed = {'marketplace':urlparse(page_url or '').hostname, 'page_url':page_url,
                'delivery_location':delivery, 'currency_preference':currency,
                'displayed_currency':displayed_currency, 'language':language,
                'asin':asin, **values, 'variant_evidence':variant_evidence}
    reasons = []
    if observed['marketplace'] != config['marketplace']:
        reasons.append('marketplace_mismatch')
    if not re.search(r'(?<!\d)' + re.escape(config['delivery_postcode']) + r'(?!\d)', delivery or ''):
        reasons.append('delivery_postcode_mismatch')
    if currency != config['currency']:
        reasons.append('currency_preference_unverified')
    if displayed_currency and displayed_currency != config['currency']:
        reasons.append('displayed_currency_mismatch')
    region_verified = not reasons
    if language and language.lower() != config['language'].lower():
        reasons.append('language_mismatch')
    elif not language:
        reasons.append('language_unverified')
    if asin and asin != requested_asin:
        reasons.append('asin_mismatch')
    elif not asin:
        reasons.append('asin_unverified')
    # A selected option may refer to a different child than the buy-box ASIN.
    option_asin = re.search(r'\bB[A-Z0-9]{9}\b', option.get('value', '')) if option else None
    if option_asin and option_asin[0] != asin:
        observed['size_option_asin'] = option_asin[0]
        variant_reasons.append('variant_binding_unverified' if 'MC_Custom_' in option.get('value','')
                               else 'variant_evidence_conflict')
    reasons.extend(dict.fromkeys(variant_reasons))
    expected_variant = (expected_variant if expected_variant is not None
                        else config['tracked_variants'].get(requested_asin))
    if expected_variant:
        for key in ('asin', 'size', 'color'):
            if not expected_variant.get(key) or observed.get(key) is None:
                reasons.append(key + '_unverified' if key + '_unverified' not in reasons else 'variant_unverified')
            elif observed[key] != expected_variant[key] and key + '_mismatch' not in reasons:
                reasons.append(key + '_mismatch')
        variant_status = 'matched' if not any(r.startswith(('asin_', 'size_', 'color_', 'variant_')) for r in reasons) else 'unconfirmed'
    else:
        variant_status = 'not_fixed'
    return {'expected':{k:config[k] for k in ('marketplace','delivery_postcode','currency','language','price_scope','variant_policy')},
            'expected_variant':expected_variant, 'requested_asin':requested_asin,
            'observed':observed, 'region_verified':region_verified,
            'variant_status':variant_status, 'status':'matched' if not reasons else 'unconfirmed',
            'comparison_ready':bool(expected_variant and not reasons), 'reasons':reasons}


def require_region(result):
    blocking = [r for r in result['reasons'] if r in (
        'marketplace_mismatch','delivery_postcode_mismatch','currency_preference_unverified',
        'displayed_currency_mismatch','language_mismatch','language_unverified','asin_mismatch')]
    if blocking:
        raise ValueError('collection_conditions_failed:' + ','.join(blocking))
