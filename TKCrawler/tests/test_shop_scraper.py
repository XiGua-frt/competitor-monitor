"""Tests for src/scrapers (Agent D: Shop product page parsing).

Offline only — every HTTP interaction goes through a mock client.
No real TikTok requests are made.

Channels covered:
- VERIFIED shop.tiktok.com PDP channel (__MODERN_ROUTER_DATA__, live
  probed 2026-10-08): fixtures are the REAL probe captures under
  docs/probe/ (docs/probe/shop_retest_2026-10-08.md). Tests assert the
  15-field mapping against shop_product_fields_2026-10-08.json evidence
  values.
- Legacy ProductParser (selectors.json assumption-based): parsing-logic
  fixtures kept unchanged.

Also: URL building (incl. region), login-state requirement, argus
"Security Check" challenge detection, 23002002 region error, daily quota,
429/403 soft cooldown.
"""

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.common import config as common_config
from src.common.exceptions import (
    QuotaExceededException,
    RateLimitException,
    SessionExpiredException,
    WAFBlockedException,
)
from src.scrapers import ShopProductScraper
from src.scrapers.parsers.product_parser import (
    REQUIRED_FIELDS,
    ProductParser,
)
from src.scrapers.parsers.shop_parser import (
    SHOP_FIELDS,
    ShopPageParser,
    is_security_check_page,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROBE_DIR = PROJECT_ROOT / 'docs' / 'probe'
FIXTURES = Path(__file__).parent / 'fixtures'


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding='utf-8')


def load_probe(name: str) -> str:
    return (PROBE_DIR / name).read_text(encoding='utf-8')


# Real probe captures (docs/probe/shop_retest_2026-10-08.md):
# R8 = 200 full-data PDP, R10 = 200 shell with error_code 23002002,
# R9 = argus "Security Check" challenge page (no cookies).
PROBE_PDP_OK = 'shop_pdp_final_body_2026-10-08.html'
PROBE_PDP_REGION_ERROR = 'shop_pdp_product2_body_2026-10-08.html'
PROBE_SECURITY_CHECK = 'shop_pdp_nocookie_body_2026-10-08.html'
PROBE_FIELDS = 'shop_product_fields_2026-10-08.json'


@pytest.fixture(scope='module')
def probe_evidence():
    """Field-level probe evidence JSON (real extracted values)."""
    return json.loads(load_probe(PROBE_FIELDS))


class MockResponse:
    def __init__(self, status_code=200, text=''):
        self.status_code = status_code
        self.text = text


class MockClient:
    """Mock AntiDetectionClient: request(url, method, headers, **kwargs)."""

    def __init__(self, responses=None):
        # responses: callable(url) -> MockResponse or exception instance
        self.responses = responses or (lambda url: MockResponse(200, ''))
        self.calls = []

    def request(self, url, method='GET', headers=None, **kwargs):
        self.calls.append({'url': url, 'method': method, 'headers': headers})
        result = self.responses(url)
        if isinstance(result, Exception):
            raise result
        return result

    def switch_channel(self, channel):
        pass


class StubSessionManager:
    """Test double for SessionManager: canned cookies or expired session."""

    def __init__(self, cookies=None):
        self._cookies = cookies

    def get_cookies(self):
        if not self._cookies:
            raise SessionExpiredException("No valid session found")
        return dict(self._cookies)


@pytest.fixture
def scraper():
    """Scraper with zero wait intervals, 60s cooldown and a logged-in stub."""
    return ShopProductScraper(
        client=MockClient(),
        session_manager=StubSessionManager(
            {'sessionid': 'test-session', 'msToken': 'test-token'}),
        min_interval=0,
        max_interval=0,
        cooldown_seconds=60,
    )


# ===========================================================================
# 1. parse_sales_range (legacy pure logic, unchanged contract)
# ===========================================================================

class TestParseSalesRange:
    def test_range_median(self):
        assert ShopProductScraper.parse_sales_range('500-1k') == 750

    def test_open_ended(self):
        assert ShopProductScraper.parse_sales_range('10k+') == 10000

    def test_plain_suffixed(self):
        assert ShopProductScraper.parse_sales_range('1.2k') == 1200

    def test_plain_number(self):
        assert ShopProductScraper.parse_sales_range('300') == 300

    def test_range_with_suffix_on_both_sides(self):
        assert ShopProductScraper.parse_sales_range('1.5k-2k') == 1750

    def test_uppercase_suffix(self):
        assert ShopProductScraper.parse_sales_range('2K+') == 2000

    def test_empty_string(self):
        assert ShopProductScraper.parse_sales_range('') == 0

    def test_none(self):
        assert ShopProductScraper.parse_sales_range(None) == 0

    def test_garbage(self):
        assert ShopProductScraper.parse_sales_range('sold out') == 0

    def test_whitespace(self):
        assert ShopProductScraper.parse_sales_range('  500-1k  ') == 750


# ===========================================================================
# 2. URL building (verified shop.tiktok.com template, incl. region)
# ===========================================================================

class TestProductUrl:
    def test_default_template_and_region(self, scraper):
        assert scraper._product_url('1729480426738058662') == (
            'https://shop.tiktok.com/view/product/'
            '1729480426738058662?region=MY'
        )

    def test_region_override_in_constructor(self):
        s = ShopProductScraper(
            client=MockClient(),
            session_manager=StubSessionManager({'sessionid': 'x'}),
            region='gb',
            min_interval=0, max_interval=0,
        )
        assert s.region == 'GB'
        assert s._product_url('123') == (
            'https://shop.tiktok.com/view/product/123?region=GB'
        )

    def test_region_env_default(self, monkeypatch):
        monkeypatch.setenv('SHOP_REGION', 'TH')
        s = ShopProductScraper(
            client=MockClient(),
            session_manager=StubSessionManager({'sessionid': 'x'}),
            min_interval=0, max_interval=0,
        )
        assert s.region == 'TH'

    def test_old_www_template_not_used(self, scraper):
        """The old www.tiktok.com/shop/product/{id} template is dead
        (302 -> homepage, probe R3) and must never be built again."""
        url = scraper._product_url('123')
        assert 'www.tiktok.com' not in url
        assert url.startswith('https://shop.tiktok.com/view/product/')

    def test_scrape_requests_hit_verified_entry_url(self, scraper):
        scraper.client = MockClient(
            lambda url: MockResponse(200, load_probe(PROBE_PDP_OK)))
        scraper.scrape_product('1729480426738058662')
        assert scraper.client.calls[0]['url'] == (
            'https://shop.tiktok.com/view/product/'
            '1729480426738058662?region=MY'
        )


# ===========================================================================
# 3. Login state is a hard requirement (probe R9)
# ===========================================================================

class TestLoginStateRequired:
    def test_missing_session_raises_before_any_request(self):
        scraper = ShopProductScraper(
            client=MockClient(),
            session_manager=StubSessionManager(None),
            min_interval=0, max_interval=0,
        )
        with pytest.raises(SessionExpiredException, match='登录态'):
            scraper.scrape_product('123')
        # no request was ever issued
        assert scraper.client.calls == []

    def test_cookies_sent_as_header(self, scraper):
        scraper.client = MockClient(
            lambda url: MockResponse(200, load_probe(PROBE_PDP_OK)))
        scraper.scrape_product('1729480426738058662')
        headers = scraper.client.calls[0]['headers']
        assert 'sessionid=test-session' in headers['Cookie']
        assert 'msToken=test-token' in headers['Cookie']

    def test_navigation_headers_added_per_request(self, scraper):
        """R8 recipe: document-navigation Sec-Fetch-* + www Referer on top
        of the client defaults."""
        scraper.client = MockClient(
            lambda url: MockResponse(200, load_probe(PROBE_PDP_OK)))
        scraper.scrape_product('1729480426738058662')
        headers = scraper.client.calls[0]['headers']
        assert headers['Sec-Fetch-Dest'] == 'document'
        assert headers['Sec-Fetch-Mode'] == 'navigate'
        assert headers['Sec-Fetch-Site'] == 'cross-site'
        assert headers['Referer'] == 'https://www.tiktok.com/'


# ===========================================================================
# 4. __MODERN_ROUTER_DATA__ extraction (real probe HTML)
# ===========================================================================

class TestRouterDataExtraction:
    @pytest.fixture
    def parser(self):
        return ShopPageParser()

    def test_extracts_router_data_from_real_probe_html(self, parser):
        data = parser.extract_router_data(load_probe(PROBE_PDP_OK))
        assert data is not None
        assert 'loaderData' in data

    def test_find_pdp_loader_structurally(self, parser):
        data = parser.extract_router_data(load_probe(PROBE_PDP_OK))
        loader = parser.find_pdp_loader(data)
        assert loader is not None
        assert isinstance(loader['page_config']['components_map'], list)

    def test_product_component_found_by_type_not_index(self, parser):
        """Probe: the product component sits at index 3 on the final MY
        page but index 5 on the error page. Locating must rely on
        component_type, never on array position."""
        data = parser.extract_router_data(load_probe(PROBE_PDP_OK))
        loader = parser.find_pdp_loader(data)
        components = loader['page_config']['components_map']
        component_data = parser.find_product_component_data(components)

        assert component_data is not None
        assert 'product_info' in component_data
        # and NOT simply "whatever sits at index 3"
        assert component_data.get('product_info', {}).get('product_id') == \
            '1729480426738058662'

    def test_product_component_found_at_arbitrary_index(self, parser):
        """Synthetic components_map with the product component moved to
        the front and extra components around it — still located."""
        real = parser.extract_router_data(load_probe(PROBE_PDP_OK))
        loader = parser.find_pdp_loader(real)
        components = loader['page_config']['components_map']
        product_component = next(
            c for c in components
            if isinstance(c.get('component_data'), dict)
            and 'product_info' in c['component_data'])

        reordered = [product_component] + [
            c for c in components if c is not product_component]
        component_data = parser.find_product_component_data(reordered)
        assert component_data['product_info']['product_id'] == \
            '1729480426738058662'

    def test_no_router_data_returns_none(self, parser):
        assert parser.extract_router_data(
            '<html><body>no data</body></html>') is None

    def test_parse_empty_html_returns_none(self, parser):
        assert parser.extract_router_data('') is None

    def test_security_check_detector_on_real_probe_page(self, parser):
        html = load_probe(PROBE_SECURITY_CHECK)
        assert is_security_check_page(html) is True
        # sanity: the verified OK page must not be flagged
        assert is_security_check_page(load_probe(PROBE_PDP_OK)) is False

    def test_security_check_detector_title_only(self):
        assert is_security_check_page(
            '<html><head><title>Security Check</title></head>'
            '<body></body></html>') is True
        assert is_security_check_page(
            '<html><head><title>Product</title></head>'
            '<body>security check mentioned in text</body></html>') is False


# ===========================================================================
# 5. 15-field mapping against real probe values
# ===========================================================================

class TestShopFieldMapping:
    """Each of the 15 SHOP_FIELDS asserted against the verified evidence
    values from docs/probe/shop_product_fields_2026-10-08.json."""

    @pytest.fixture(scope='class')
    def parsed(self):
        parser = ShopPageParser()
        return parser.parse(load_probe(PROBE_PDP_OK))

    def test_exactly_15_fields(self, parsed):
        assert len(SHOP_FIELDS) == 15
        assert set(parsed.keys()) == set(SHOP_FIELDS)

    def test_product_id(self, parsed):
        assert parsed['product_id'] == '1729480426738058662'

    def test_title(self, parsed, probe_evidence):
        assert parsed['title'] == probe_evidence['product_info']['title']
        assert 'HIGH PRESSURE NOZZLE' in parsed['title']

    def test_categories_three_levels(self, parsed, probe_evidence):
        expected = {c['level']: c['category_name']
                    for c in probe_evidence['categories']}
        assert parsed['category_l1'] == expected[1]
        assert parsed['category_l2'] == expected[2]
        assert parsed['category_l3'] == expected[3]
        assert parsed['category_l1'] == 'Automotive & Motorcycle'
        assert parsed['category_l2'] == 'Car Washing & Maintenance'
        assert parsed['category_l3'] == 'Water Guns & Snow Foam Lances'

    def test_price_current_from_min_sku_price(self, parsed, probe_evidence):
        # price.real_price is the "RM19.90" display string; the numeric
        # value comes from price.min_sku_price ("19.90")
        assert probe_evidence['product_info']['price']['real_price'] == 'RM19.90'
        assert parsed['price_current'] == pytest.approx(19.9)

    def test_price_original(self, parsed, probe_evidence):
        assert probe_evidence['product_info']['price']['original_price'] \
            == 'RM69.90'
        assert parsed['price_original'] == pytest.approx(69.9)

    def test_sales_total_global_caliber(self, parsed, probe_evidence):
        """口径: sold_count = "items sold globally since listed"
        (sold_count_note) — must surface as sales_total_global, never as
        a 30-day figure."""
        assert parsed['sales_total_global'] == 1584
        assert int(probe_evidence['product_info']['sold_count']) == 1584
        assert 'globally since' in \
            probe_evidence['product_info']['sold_count_note']['text']
        assert 'sales_30d' not in parsed  # caliber guard

    def test_rating(self, parsed):
        assert parsed['rating'] == pytest.approx(4.7)

    def test_review_count(self, parsed):
        assert parsed['review_count'] == 419

    def test_shop_id_and_name(self, parsed, probe_evidence):
        assert parsed['shop_id'] == \
            probe_evidence['product_info']['seller']['seller_id']
        assert parsed['shop_name'] == 'Autozo Carpro'
        # shop 域卖家名即店铺名 (probe row 8)
        assert parsed['shop_name'] == \
            probe_evidence['product_info']['seller']['name']

    def test_image_urls(self, parsed, probe_evidence):
        urls = parsed['image_urls']
        assert isinstance(urls, list) and len(urls) == 8
        assert urls[0] == \
            probe_evidence['product_info']['images'][0]['url_list'][0]

    def test_sku_count_and_stock_status(self, parsed, probe_evidence):
        # 证据商品已售罄: skus[].stock == 0 + unavailable_info.text
        assert probe_evidence['product_info']['skus'][0]['stock'] == 0
        assert probe_evidence['product_info']['unavailable_info']['text'] == \
            'This item is out of stock'
        assert parsed['sku_count'] == 1
        assert parsed['stock_status'] == 'out_of_stock'

    def test_stock_status_in_stock(self):
        """A product with positive sku stock maps to in_stock."""
        import re
        from bs4 import BeautifulSoup  # noqa: F401  (import sanity only)

        parser = ShopPageParser()
        data = parser.extract_router_data(load_probe(PROBE_PDP_OK))
        loader = parser.find_pdp_loader(data)
        component = next(
            c for c in loader['page_config']['components_map']
            if isinstance(c.get('component_data'), dict)
            and 'product_info' in c['component_data'])
        component['component_data']['product_info']['skus'][0]['stock'] = 5

        # re-render the script tag with mutated JSON
        script = json.dumps(data, ensure_ascii=False)
        mutated = re.sub(
            r'(<script[^>]*id="__MODERN_ROUTER_DATA__"[^>]*>).*?(</script>)',
            lambda m: m.group(1) + script + m.group(2),
            load_probe(PROBE_PDP_OK), count=1, flags=re.S)
        result = parser.parse(mutated)
        assert result['stock_status'] == 'in_stock'


# ===========================================================================
# 5b. Category structures: new 2026-10-08 E2E layout vs probe-period layout
# ===========================================================================

CATEGORY_NEW_FIXTURE = 'shop_category_new_structure_2026-10-08.json'
CATEGORY_OLD_FIXTURE = 'shop_category_old_structure_probe.json'


def _router_html(router_data: dict) -> str:
    """Wrap router data into a __MODERN_ROUTER_DATA__ script node."""
    return (
        '<!DOCTYPE html><html><head>'
        '<script type="application/json" id="__MODERN_ROUTER_DATA__">'
        + json.dumps(router_data, ensure_ascii=False)
        + '</script></head><body></body></html>'
    )


class TestCategoryStructures:
    """Live-page structure moved on 2026-10-08 (e2e_shop_2026-10-08.md):
    categories now live in
    product component_data.category_info.recommended_categories[]
    (no `level` keys, array order = the 3-level chain). The probe-period
    `categories[]`-with-level layout must keep working as fallback."""

    @pytest.fixture
    def parser(self):
        return ShopPageParser()

    def test_new_structure_categories_by_array_order(self, parser):
        """Real E2E capture (e2e_shop_response_2026-10-08.json, trimmed):
        recommended_categories[] mapped to l1/l2/l3 by array order."""
        data = json.loads(load_fixture(CATEGORY_NEW_FIXTURE))
        result = parser.parse(_router_html(data))

        assert result is not None
        assert result['category_l1'] == 'Automotive & Motorcycle'
        assert result['category_l2'] == 'Car Washing & Maintenance'
        assert result['category_l3'] == 'Water Guns & Snow Foam Lances'

    def test_new_structure_product_component_found_not_by_index(self, parser):
        """On the new page the product component sits at index 2 of this
        trimmed capture (probe page had it at 3) — located structurally."""
        data = json.loads(load_fixture(CATEGORY_NEW_FIXTURE))
        components = data['loaderData'][
            '(product_name_slug$)/(product_id)/page'][
            'page_config']['components_map']
        component_data = parser.find_product_component_data(components)
        assert component_data['product_info']['product_id'] == \
            '1729480426738058662'

    def test_new_structure_full_parse_fields(self, parser):
        """Beyond categories: the new page layout parses the full 15-field
        dict with the same E2E evidence values."""
        data = json.loads(load_fixture(CATEGORY_NEW_FIXTURE))
        result = parser.parse(_router_html(data))

        assert set(result.keys()) == set(SHOP_FIELDS)
        assert result['product_id'] == '1729480426738058662'
        assert result['price_current'] == pytest.approx(19.9)
        assert result['sales_total_global'] == 1584
        assert result['review_count'] == 419
        assert result['shop_name'] == 'Autozo Carpro'
        assert result['sku_count'] == 1
        assert result['stock_status'] == 'out_of_stock'

    def test_precedence_new_structure_wins_when_both_present(self, parser):
        """When a component carries BOTH layouts, the new structure wins
        (the live page renders the old one empty). Fixture: old
        categories[] carries the probe chain, new recommended_categories[]
        carries a sentinel — the sentinel must surface."""
        component_data = json.loads(load_fixture(CATEGORY_OLD_FIXTURE))
        cats = parser._extract_categories(component_data)
        assert cats['category_l1'] == 'NEW SHOULD WIN'
        assert cats['category_l2'] is None
        assert cats['category_l3'] is None

    def test_old_structure_fallback_when_new_missing(self, parser):
        """Probe-period layout (categories[] with `level` keys) still
        maps when category_info is absent."""
        component_data = json.loads(load_fixture(CATEGORY_OLD_FIXTURE))
        del component_data['category_info']
        cats = parser._extract_categories(component_data)
        assert cats['category_l1'] == 'Automotive & Motorcycle'
        assert cats['category_l2'] == 'Car Washing & Maintenance'
        assert cats['category_l3'] == 'Water Guns & Snow Foam Lances'

    def test_short_chain_maps_missing_levels_to_none(self, parser):
        """Fewer than 3 recommended entries: remaining levels stay None
        instead of raising / bleeding values across levels."""
        component_data = {
            'category_info': {'recommended_categories': [
                {'category_id': '605196',
                 'category_name': 'Automotive & Motorcycle'},
                {'category_id': '940808',
                 'category_name': 'Car Washing & Maintenance'},
            ]},
        }
        cats = parser._extract_categories(component_data)
        assert cats == {
            'category_l1': 'Automotive & Motorcycle',
            'category_l2': 'Car Washing & Maintenance',
            'category_l3': None,
        }

    def test_no_categories_yields_all_none(self, parser):
        component_data = {
            'category_info': {'recommended_categories': []},
            'categories': [],
        }
        cats = parser._extract_categories(component_data)
        assert cats == {
            'category_l1': None, 'category_l2': None, 'category_l3': None,
        }


# ===========================================================================
# 6. Scraper-level: Security Check / 23002002 / shells / network errors
# ===========================================================================

class TestScrapeProductResponses:
    def test_security_check_page_raises_waf_blocked(self, scraper):
        """argus challenge page comes back with status 200 (probe R9) and
        must surface as WAFBlockedException."""
        scraper.client = MockClient(
            lambda url: MockResponse(200, load_probe(PROBE_SECURITY_CHECK)))
        with pytest.raises(WAFBlockedException):
            scraper.scrape_product('123')

    def test_region_error_23002002_returns_none(self, scraper, caplog):
        """Probe R10: US product viewed with an hk-account -> 200 shell
        with structured error_code 23002002 (region restriction, not a
        routing failure). Batch jobs must keep running: return None."""
        scraper.client = MockClient(
            lambda url: MockResponse(
                200, load_probe(PROBE_PDP_REGION_ERROR)))
        assert scraper.scrape_product('1729450858130215273') is None

    def test_region_error_message_hint(self, scraper, monkeypatch):
        """caplog cannot capture loguru output — spy on the parser module
        logger instead."""
        import src.scrapers.parsers.shop_parser as shop_parser_module

        class LogSpy:
            def __init__(self):
                self.messages = []

            def _rec(self, msg, *a, **k):
                self.messages.append(str(msg))

            warning = _rec
            error = _rec
            info = _rec
            debug = _rec

        spy = LogSpy()
        monkeypatch.setattr(shop_parser_module, 'logger', spy)

        scraper.client = MockClient(
            lambda url: MockResponse(
                200, load_probe(PROBE_PDP_REGION_ERROR)))
        scraper.scrape_product('1729450858130215273')
        assert any('23002002' in m for m in spy.messages)

    def test_shell_page_without_router_data_returns_none(self, scraper):
        scraper.client = MockClient(
            lambda url: MockResponse(
                200, '<html><body>empty shell</body></html>'))
        assert scraper.scrape_product('123') is None

    def test_scrape_product_success_end_to_end(self, scraper):
        scraper.client = MockClient(
            lambda url: MockResponse(200, load_probe(PROBE_PDP_OK)))
        result = scraper.scrape_product('1729480426738058662')

        assert result is not None
        assert set(result.keys()) == set(SHOP_FIELDS)
        assert result['product_id'] == '1729480426738058662'
        assert result['sales_total_global'] == 1584
        # exactly one request, quota consumed once
        assert len(scraper.client.calls) == 1
        assert scraper.get_quota_status('products')['used'] == 1

    def test_scrape_product_404_returns_none(self, scraper):
        scraper.client = MockClient(lambda url: MockResponse(404, 'Not Found'))
        assert scraper.scrape_product('123') is None

    def test_scrape_product_500_returns_none(self, scraper):
        scraper.client = MockClient(lambda url: MockResponse(500, 'oops'))
        assert scraper.scrape_product('123') is None

    def test_network_error_returns_none(self, scraper):
        client = MockClient()
        client.request = MagicMock(side_effect=ConnectionError('boom'))
        scraper.client = client
        assert scraper.scrape_product('123') is None

    def test_client_used_not_real_network(self, scraper):
        """The injected mock must be used — BaseScraper never builds a real
        client when one is provided."""
        assert isinstance(scraper.client, MockClient)


# ===========================================================================
# 7. Legacy ProductParser (selectors.json assumption-based) — logic kept
# ===========================================================================

class TestLegacyProductParser:
    @pytest.fixture
    def parsed(self):
        parser = ProductParser()
        return parser.parse(load_fixture('product_page_sample.html'))

    def test_all_15_fields_present(self, parsed):
        assert parsed is not None
        assert set(parsed.keys()) >= set(REQUIRED_FIELDS)
        missing = [f for f in REQUIRED_FIELDS if parsed.get(f) is None]
        assert missing == [], f"fixture should yield 100% field coverage, missing: {missing}"

    def test_field_values(self, parsed):
        assert parsed['product_id'] == '7301234567890123456'
        assert parsed['title'] == 'Portable Mini Blender 400ml USB Rechargeable Juicer Cup'
        assert parsed['category_l1'] == 'Home & Kitchen'
        assert parsed['category_l2'] == 'Kitchen Appliances'
        assert parsed['category_l3'] == 'Blenders'
        assert parsed['price_current'] == 19.99
        assert parsed['price_original'] == 29.99
        assert parsed['sales_30d'] == 750          # "500-1k" median
        assert parsed['listed_date'] == '2025-11-20'
        assert parsed['shop_id'] == '7300988776655443'
        assert parsed['shop_name'] == 'BlissHome Official Store'
        assert parsed['creator_count'] == 1200     # "1.2k"
        assert parsed['video_count_7d'] == 86
        assert parsed['review_count'] == 234
        assert parsed['rating'] == 4.7

    def test_gmv_computed(self, parsed):
        # gmv_30d_est = price_current * sales_30d = 19.99 * 750
        assert parsed['gmv_30d_est'] == pytest.approx(14992.5)

    def test_selectors_all_marked_unverified(self):
        selectors = ProductParser().selectors
        for url_spec in selectors['urls'].values():
            assert url_spec['verified'] is False
        for field_spec in selectors['fields'].values():
            assert field_spec['verified'] is False

    def test_json_path_walker(self):
        from src.scrapers.parsers.product_parser import _walk_json_path
        data = {'a': {'b': [{'c': 1}]}}

        assert _walk_json_path(data, "a.b[0].c") == 1
        assert _walk_json_path(data, "a['b'][0].c") == 1

        # missing path returns a sentinel that is never a real value
        sentinel_missing = _walk_json_path(data, "no.such.path")
        assert sentinel_missing is _walk_json_path(data, "also.missing")
        assert sentinel_missing != 1


# ===========================================================================
# 8. Category page (legacy unverified channel, interface kept)
# ===========================================================================

class TestScrapeCategory:
    def test_scrape_category_success(self, scraper):
        scraper.client = MockClient(
            lambda url: MockResponse(
                200, load_fixture('category_page_sample.html')))
        ids = scraper.scrape_category('https://www.tiktok.com/shop/category/blenders')

        # 3 unique IDs (duplicate card de-duplicated, non-product link ignored)
        assert ids == [
            '7301234567890123456',
            '7301234567890123457',
            '7301234567890123458',
        ]
        # category pages do not consume the product-page quota
        assert scraper.get_quota_status('products')['used'] == 0

    def test_scrape_category_404_returns_empty(self, scraper):
        scraper.client = MockClient(lambda url: MockResponse(404, 'Not Found'))
        assert scraper.scrape_category('https://x/category') == []

    def test_scrape_category_no_links_returns_empty(self, scraper):
        scraper.client = MockClient(
            lambda url: MockResponse(200, '<html><body></body></html>'))
        assert scraper.scrape_category('https://x/category') == []

    def test_scrape_category_security_check_raises(self, scraper):
        scraper.client = MockClient(
            lambda url: MockResponse(200, load_probe(PROBE_SECURITY_CHECK)))
        with pytest.raises(WAFBlockedException):
            scraper.scrape_category('https://x/category')


# ===========================================================================
# 9. Daily quota
# ===========================================================================

class TestDailyQuota:
    def test_quota_exceeded_raises(self, monkeypatch):
        monkeypatch.setattr(common_config.config, 'DAILY_QUOTA_PRODUCTS', 2)
        scraper = ShopProductScraper(
            client=MockClient(lambda url: MockResponse(404, '')),
            session_manager=StubSessionManager({'sessionid': 'x'}),
            min_interval=0, max_interval=0,
        )
        scraper.check_quota('products')  # 1
        scraper.check_quota('products')  # 2
        with pytest.raises(QuotaExceededException):
            scraper.scrape_product('123')

        # and the request was never issued
        assert scraper.client.calls == []


# ===========================================================================
# 10. 429/403 soft cooldown
# ===========================================================================

class TestSoftCooldown:
    def test_403_triggers_cooldown_and_reraises(self, scraper):
        scraper.client = MockClient(
            lambda url: WAFBlockedException(url, 403)
        )
        with pytest.raises(WAFBlockedException):
            scraper.fetch('https://x/product/1')

        assert scraper.in_cooldown is True
        assert 0 < scraper.get_cooldown_remaining() <= 60

    def test_429_triggers_cooldown_and_reraises(self, scraper):
        scraper.client = MockClient(
            lambda url: RateLimitException('429')
        )
        with pytest.raises(RateLimitException):
            scraper.fetch('https://x/product/1')
        assert scraper.in_cooldown is True

    def test_blocked_requests_during_cooldown(self, scraper):
        scraper.client = MockClient(
            lambda url: WAFBlockedException(url, 403)
        )
        with pytest.raises(WAFBlockedException):
            scraper.fetch('https://x/product/1')

        calls_after_block = len(scraper.client.calls)
        # During cooldown, fetch refuses WITHOUT touching the client
        with pytest.raises(RateLimitException, match='Soft cooldown'):
            scraper.fetch('https://x/product/2')
        assert len(scraper.client.calls) == calls_after_block

    def test_cooldown_expires(self):
        scraper = ShopProductScraper(
            client=MockClient(lambda url: MockResponse(200, '')),
            session_manager=StubSessionManager({'sessionid': 'x'}),
            min_interval=0, max_interval=0, cooldown_seconds=0.05,
        )
        scraper.enter_cooldown('test')
        assert scraper.in_cooldown is True
        time.sleep(0.1)
        assert scraper.in_cooldown is False
        # requests resume after expiry
        assert scraper.fetch('https://x/1').status_code == 200

    def test_reset_cooldown(self, scraper):
        scraper.enter_cooldown('test')
        assert scraper.in_cooldown is True
        scraper.reset_cooldown()
        assert scraper.in_cooldown is False

    def test_scrape_product_propagates_cooldown_exception(self, scraper):
        scraper.client = MockClient(
            lambda url: WAFBlockedException(url, 403)
        )
        with pytest.raises(WAFBlockedException):
            scraper.scrape_product('123')
        # second call blocked by cooldown state
        with pytest.raises(RateLimitException, match='Soft cooldown'):
            scraper.scrape_product('456')


# ===========================================================================
# 11. Risk-control parameters come from config, not hardcoded
# ===========================================================================

class TestRiskParamsFromConfig:
    def test_interval_defaults_are_seven_to_ten(self):
        from src.scrapers.base_scraper import RISK_DEFAULTS
        assert RISK_DEFAULTS['REQUEST_MIN_INTERVAL'] == 7
        assert RISK_DEFAULTS['REQUEST_MAX_INTERVAL'] == 10

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv('SCRAPER_REQUEST_MIN_INTERVAL', '8')
        monkeypatch.setenv('SCRAPER_REQUEST_MAX_INTERVAL', '12')
        import importlib
        import src.scrapers.base_scraper as bs
        importlib.reload(bs)
        assert bs.RISK_DEFAULTS['REQUEST_MIN_INTERVAL'] == 8
        assert bs.RISK_DEFAULTS['REQUEST_MAX_INTERVAL'] == 12
        importlib.reload(bs)  # restore for other tests

    def test_scraper_uses_interval_params(self):
        scraper = ShopProductScraper(
            session_manager=StubSessionManager({'sessionid': 'x'}),
            min_interval=3, max_interval=4)
        assert scraper.limiter.min_interval == 3
        assert scraper.limiter.max_interval == 4
