"""Isolated AmzPy probe: one search page, at most three product pages, no retries."""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from types import SimpleNamespace

from bs4 import BeautifulSoup
from amzpy import AmazonScraper

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from collector.conditions import load_conditions, check_page, require_region, session_path
from collector.relevance import classify_product, prepare_candidates

CONDITIONS = load_conditions()
OUT = ROOT.parent / 'test/amazon/amzpy'
QUERY = 'custom pet portrait shirt'

class StopProbe(BaseException):
    pass

def text(soup, selector):
    node = soup.select_one(selector)
    return node.get_text(' ', strip=True) if node else None

def normalize(product, listing, html, position, page_url=None, conditions_config=None, keyword=None):
    config=conditions_config or CONDITIONS
    soup = BeautifulSoup(html, 'lxml')
    bullets = [x.get_text(' ', strip=True) for x in soup.select('#feature-bullets li .a-list-item, #productFactsDesktopExpander li .a-list-item')]
    bullets = [x for x in bullets if x and 'Make sure this fits' not in x]
    description = '\n'.join(bullets) or text(soup, '#productDescription')
    count_text = text(soup, '[data-hook="total-review-count"]') or text(soup, '#acrCustomerReviewText')
    count_match = re.search(r'([\d,]+)\s+(?:global\s+)?ratings?\b', count_text or '', re.I)
    details = {}
    for node in soup.select('#detailBullets_feature_div li, #productDetails_detailBullets_sections1 tr, #productDetails_techSpec_section_1 tr'):
        value = ' '.join(node.get_text(' ', strip=True).replace('\u200e', '').replace('\u200f', '').split())
        for key in ['Date First Available', 'Best Sellers Rank']:
            if key in value:
                details[key] = value.split(key, 1)[1].strip(' :') or None
    seller = soup.select_one('#sellerProfileTriggerId')
    seller_id = parse_qs(urlparse(seller.get('href', '')).query).get('seller', [None])[0] if seller else None
    image = product.get('img_url')
    asin = product['asin']
    price_text = text(soup, '#apex-pricetopay-accessibility-label') or text(soup, '#corePriceDisplay_desktop_feature_div .a-price .a-offscreen, #corePrice_feature_div .a-price .a-offscreen')
    price_match = re.search(r'(USD|JPY|\$|¥)\s*([\d,]+(?:\.\d+)?)', price_text or '')
    raw_price = float(price_match[2].replace(',', '')) if price_match else None
    currency = {'$':'USD','¥':'JPY'}.get(price_match[1], price_match[1]) if price_match else None
    delivery = text(soup, '#glow-ingress-line2')
    currency_node = soup.select_one('#currencyOfPreference')
    preference_currency = currency_node.get('value') if currency_node else None
    availability = text(soup, '#availability')
    us_verified = bool(delivery and re.search(r'(?<!\d)'+re.escape(config['delivery_postcode'])+r'(?!\d)',delivery)
                       and preference_currency == config['currency'])
    missing_reason = None if price_match else ('delivery_location_unsupported'
        if 'cannot be shipped' in (availability or '').lower() else 'main_price_not_displayed')
    conditions = check_page(html, page_url, asin, config, displayed_currency=currency)
    return {
        'platform':'amazon', 'external_id':asin, 'source_url':f'https://www.amazon.com/dp/{asin}',
        'title':product.get('title'), 'description':description, 'author_id':seller_id,
        'published_at':None, 'cover_url':image, 'media_urls':[image] if image else [], 'hashtags':[],
        'stats':dict.fromkeys(['views','likes','comments','shares','collections']),
        'commerce':{'price':raw_price, 'currency':currency, 'brand':product.get('brand'),
            'rating':product.get('rating'), 'rating_count':int(count_match[1].replace(',', '')) if count_match else None,
            'written_review_count':None, 'date_first_available_raw':details.get('Date First Available'),
            'best_sellers_rank_raw':details.get('Best Sellers Rank'),
            'recent_purchase_text':text(soup, '#social-proofing-faceout-title-tk_bought'),
            'availability_text':availability,
            'seller_name':seller.get_text(' ', strip=True) if seller else None},
        'search':{'keyword':keyword or QUERY, 'selected_result_position':listing.get('_search_result_position', position), 'is_sponsored':None,
            'position_scope':'order_returned_by_amzpy_not_verified_page_rank'},
        'relevance':classify_product(product.get('title'), description, stage='detail'),
        'target_status_code':'partial', 'fetched_at':datetime.now(timezone.utc).isoformat(),
        'collection_conditions':conditions,
        'evidence':{'description_source':'feature_bullets' if bullets else 'product_description',
            'price_scope':config['price_scope'],
            'media_urls_complete':False, 'price_source':'main_product_price_container', 'price_raw':price_text,
            'delivery_location':delivery, 'currency_preference':preference_currency,
            'us_preferences_verified':us_verified, 'price_missing_reason':missing_reason,
            'rating_count_raw':count_text}}

def run():
    OUT.mkdir(parents=True, exist_ok=True)
    if (OUT/'report.json').exists() and not (OUT/'report-before-region-fix.json').exists():
        (OUT/'report-before-region-fix.json').write_bytes((OUT/'report.json').read_bytes())
    report = {'repository':'theonlyanil/amzpy', 'commit':'4c0ff9c', 'keyword':QUERY,
        'marketplace':'amazon.com', 'max_details':3, 'max_requests':4, 'requests':[],
        'search_mode':'saved_listing' if '--same-products' in sys.argv else 'live_search',
        'status':'started', 'products_saved':0, 'search_results':0}
    rows, raw, discoveries = [], [], []
    html_by_asin = {}
    url_by_asin = {}
    try:
        state_path = session_path(CONDITIONS)
        report['preferences_state_loaded'] = state_path.exists()
        if not state_path.exists():
            raise StopProbe('preferences_session_missing')
        scraper = AmazonScraper(country_code=CONDITIONS['country_code'], MAX_RETRIES=0, REQUEST_TIMEOUT=25,
            DELAY_BETWEEN_REQUESTS=(15,15))
        transport = scraper.session.session
        if state_path.exists():
            state = json.loads(state_path.read_text(encoding='utf-8'))
            for cookie in state.get('cookies', []):
                if cookie.get('domain','').lstrip('.') == 'amazon.com':
                    transport.cookies.set(cookie['name'],cookie['value'],domain=cookie['domain'],path=cookie.get('path','/'))
        transport.cookies.set('i18n-prefs',CONDITIONS['currency'],domain='.amazon.com',path='/')
        transport.cookies.set('lc-main',CONDITIONS['language'].replace('-','_'),domain='.amazon.com',path='/')
        scraper.session.ua_generator = SimpleNamespace(random=transport.headers['User-Agent'])
        actual_get = transport.get
        def guarded_get(url, **kwargs):
            # Amazon's marketplace choice parameter; keep requests on the requested site.
            url += ('&' if '?' in url else '?') + 'mr_donotredirect'
            parsed = urlparse(url)
            if parsed.hostname != CONDITIONS['marketplace'] or len(report['requests']) >= 4:
                raise StopProbe('request_budget_or_domain')
            entry = {'path':parsed.path, 'status':None}
            report['requests'].append(entry)
            kwargs['allow_redirects'] = False
            response = actual_get(url, **kwargs)
            entry['status'] = response.status_code
            entry['final_host'] = urlparse(response.url).hostname
            entry['bytes'] = len(response.content)
            html = response.text
            is_detail = re.search(r'/dp/([A-Z0-9]{10})', parsed.path)
            name = is_detail[1] if is_detail else 'search'
            (OUT / (name+'.html')).write_text(html, encoding='utf-8')
            if is_detail:
                html_by_asin[is_detail[1]] = html
                url_by_asin[is_detail[1]] = response.url
            page = BeautifulSoup(html, 'lxml')
            if response.status_code != 200:
                raise StopProbe('http_' + str(response.status_code))
            if urlparse(response.url).hostname != 'www.amazon.com':
                raise StopProbe('unexpected_redirect')
            if page.select_one('form[action*="validateCaptcha"], input#captchacharacters') or 'api-services-support@amazon.com' in html:
                raise StopProbe('verification_required')
            if '/ap/signin' in urlparse(response.url).path:
                raise StopProbe('login_required')
            conditions = check_page(html, response.url, is_detail[1] if is_detail else None, CONDITIONS)
            entry['collection_conditions'] = conditions
            try:
                require_region(conditions)
            except ValueError as error:
                raise StopProbe(str(error))
            return response
        transport.get = guarded_get
        results = scraper.search_products(query=QUERY, max_pages=1)
        report['search_results'] = len(results)
        (OUT/'search-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
        discoveries, selected = prepare_candidates(results, QUERY, limit=3)
        report['relevance_counts'] = {status:sum(r['relevance']['status']==status for r in discoveries)
                                      for status in ('related','unrelated','needs_review')}
        report['selected_for_details'] = len(selected)
        if not results:
            raise StopProbe('no_products_parsed')
        for i, item in enumerate(selected, 1):
            product = scraper.get_product_details('https://www.amazon.com/dp/' + item['asin'])
            if not product or not product.get('title'):
                raise StopProbe('detail_not_parsed')
            raw.append(product)
            row = normalize(product,item,html_by_asin[item['asin']],i,url_by_asin[item['asin']])
            try:
                require_region(row['collection_conditions'])
            except ValueError as error:
                report['condition_failure'] = row['collection_conditions']
                raise StopProbe(str(error))
            rows.append(row)
        report['status'] = 'partial' if rows else 'no_eligible_candidates'
    except StopProbe as error:
        report['status'], report['stop_reason'] = 'stopped', str(error)
    except Exception as error:
        report['status'], report['exception'] = 'error', type(error).__name__
    finally:
        report['products_saved'] = len(rows)
        report['fetched_at'] = datetime.now(timezone.utc).isoformat()
        for filename, data in [('posts.json',rows), ('upstream-details.json',raw), ('report.json',report),
                               ('discoveries.json', discoveries)]:
            (OUT/filename).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=True,indent=2), flush=True)
    return 0 if report['status'] in ('partial','no_eligible_candidates') else 1

if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    if '--normalize-fixed' in sys.argv:
        OUT = OUT / 'us-preferences-fixed'
    if '--normalize-saved' in sys.argv or '--normalize-fixed' in sys.argv:
        products = json.loads((OUT/'upstream-details.json').read_text(encoding='utf-8'))
        results = json.loads((OUT/'search-results.json').read_text(encoding='utf-8'))
        by_asin = {r['asin']:r for r in results}
        saved_report = json.loads((OUT/'report.json').read_text(encoding='utf-8'))
        old_rows = {r['external_id']:r for r in json.loads((OUT/'posts.json').read_text(encoding='utf-8'))} if (OUT/'posts.json').exists() else {}
        saved_requests = {r['path']:r for r in saved_report['requests']}
        rows = []
        for i, p in enumerate(products, 1):
            host = saved_requests.get('/dp/' + p['asin'], {}).get('final_host')
            page_url = 'https://' + host + '/dp/' + p['asin'] if host else None
            rows.append(normalize(p,by_asin[p['asin']],(OUT/(p['asin']+'.html')).read_text(encoding='utf-8'),i,page_url))
        for row in rows:
            row['fetched_at'] = old_rows.get(row['external_id'], {}).get('fetched_at', saved_report['fetched_at'])
        (OUT/'posts.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps([{'asin':r['external_id'], **r['commerce'], 'description_present':bool(r['description']),
            'cover_present':bool(r['cover_url'])} for r in rows],ensure_ascii=True,indent=2))
    elif '--same-products' in sys.argv or '--check-redirect' in sys.argv:
        print('旧联网诊断入口已停用，请通过 collect.py once 使用持久请求控制。')
        raise SystemExit(2)
    else:
        from collect import main
        raise SystemExit(main(['once']))
