"""Conservative text relevance rules; missing evidence stays reviewable."""
import json
import re
from pathlib import Path

RULES = json.loads(Path(__file__).with_name('relevance_rules.json').read_text(encoding='utf-8'))
PATTERNS = {name:[re.compile(p, re.I) for p in RULES[name]] for name in (
    'apparel','customization','pet_portrait','pet_wear','non_apparel','customization_unavailable')}


def classify_product(title=None, description=None, stage='search'):
    if stage not in ('search', 'detail'):
        raise ValueError('invalid_relevance_stage')
    title = title.strip() if isinstance(title, str) else ''
    description = description.strip() if isinstance(description, str) else ''
    sources = [('title', title)] + ([('description', description)] if stage == 'detail' else [])
    evidence = []
    hits = {}
    for signal, patterns in PATTERNS.items():
        hits[signal] = []
        for source, text in sources:
            # Pet wear and product type exclusions use the title, not gift examples.
            if signal in ('pet_wear', 'non_apparel') and source != 'title':
                continue
            for pattern in patterns:
                match = pattern.search(text)
                if match:
                    entry = {'signal':signal, 'source':source, 'matched_text':match[0],
                             'excerpt':text[max(0,match.start()-40):match.end()+60]}
                    hits[signal].append(entry)
                    evidence.append(entry)
                    break
    scope = None
    if hits['pet_wear']:
        status, reasons = 'unrelated', ['clothing_for_pets']
    elif hits['customization_unavailable']:
        status, reasons = 'unrelated', ['customization_explicitly_unavailable']
    elif hits['non_apparel'] and not hits['apparel']:
        status, reasons = 'unrelated', ['non_apparel_product']
    elif hits['apparel'] and hits['customization'] and hits['pet_portrait']:
        status, reasons = 'related', ['apparel_customization_and_pet_portrait_evidence']
        scope = ('pet_specific' if any(h['source']=='title' for h in hits['pet_portrait'])
                 else 'general_custom_photo_apparel')
    else:
        status = 'needs_review'
        reasons = ['missing_' + s + '_evidence' for s in ('apparel','customization','pet_portrait') if not hits[s]]
    return {'status':status, 'reasons':reasons, 'product_scope':scope, 'stage':stage,
            'rule_version':RULES['version'], 'target':RULES['target'], 'evidence':evidence,
            'method':'title_and_product_description_rules_not_image_or_order_validation'}


def prepare_candidates(results, keyword, limit=3):
    """Keep every discovery, skip obvious mismatches, deduplicate within this run."""
    if limit < 0:
        raise ValueError('negative_detail_limit')
    records, selected, seen = [], [], set()
    for position, item in enumerate(results, 1):
        asin = item.get('asin')
        relevance = classify_product(item.get('title'))
        if not isinstance(asin, str) or not re.fullmatch(r'[A-Z0-9]{10}', asin):
            decision = 'skip_invalid_asin'
        elif asin in seen:
            decision = 'skip_duplicate'
        else:
            seen.add(asin)
            if relevance['status'] == 'unrelated':
                decision = 'skip_unrelated'
            elif len(selected) >= limit:
                decision = 'defer_budget'
            else:
                decision = 'fetch_detail'
                selected.append({**item, '_search_result_position':position})
        records.append({'asin':asin, 'title':item.get('title'), 'keyword':keyword,
                        'returned_position':position, 'position_scope':'order_returned_by_amzpy_not_verified_page_rank',
                        'relevance':relevance, 'decision':decision})
    return records, selected
