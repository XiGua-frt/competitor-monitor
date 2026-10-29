"""Tests for CreatorProfileScraper (Agent F: creator profile parsing).

Offline only — every HTTP interaction goes through a mock client. No real
TikTok requests are made.

Coverage:
- classify_tier boundary semantics (pure logic, fully verified)
- filter_mid_tier (腰部 1-10万) filtering
- 6-field fixture-driven parse (fixture built to match ASSUMED page
  structure — verifies parsing LOGIC, not the real page)
- SIGI_STATE fallback path
- avg_views_30d median estimation (odd/even sample counts)
- Empty page / blocked page handling (returns None, never crashes)
- scrape_profile / get_showcase_products request paths via mock client
- Daily quota gate + 429/403 soft cooldown (state + exception)
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.common import config as common_config
from src.common.exceptions import (
    QuotaExceededException,
    RateLimitException,
    WAFBlockedException,
)
from src.scrapers.creator_scraper import CreatorProfileScraper
from src.scrapers.parsers.creator_parser import CREATOR_FIELDS, CreatorParser

FIXTURES = Path(__file__).parent / 'fixtures'


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding='utf-8')


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


@pytest.fixture
def scraper():
    """Scraper with zero wait intervals and 60s cooldown for fast tests."""
    return CreatorProfileScraper(
        client=MockClient(),
        min_interval=0,
        max_interval=0,
        cooldown_seconds=60,
    )


# ===========================================================================
# 1. classify_tier (verified pure logic)
# ===========================================================================

class TestClassifyTier:
    @pytest.mark.parametrize('count,expected', [
        (0, 'nano'),
        (1, 'nano'),
        (9_999, 'nano'),
        (10_000, 'micro'),      # lower bound inclusive (1万)
        (52_300, 'micro'),
        (99_999, 'micro'),
        (100_000, 'mid'),       # 10万 -> mid
        (499_999, 'mid'),
        (500_000, 'macro'),     # 50万 -> macro
        (5_000_000, 'macro'),
    ])
    def test_boundaries(self, scraper, count, expected):
        assert scraper.classify_tier(count) == expected

    @pytest.mark.parametrize('count', [None, -1, -100_000])
    def test_invalid_counts_are_nano(self, scraper, count):
        assert scraper.classify_tier(count) == 'nano'


# ===========================================================================
# 2. filter_mid_tier (腰部 = 1-10万粉丝 = tier 'micro')
# ===========================================================================

class TestFilterMidTier:
    def test_keeps_only_micro_band(self, scraper):
        profiles = [
            {'creator_id': 'a', 'follower_count': 5_000},      # nano
            {'creator_id': 'b', 'follower_count': 10_000},     # micro (lower edge)
            {'creator_id': 'c', 'follower_count': 99_999},     # micro
            {'creator_id': 'd', 'follower_count': 100_000},    # mid
            {'creator_id': 'e', 'follower_count': 1_000_000},  # macro
        ]
        result = scraper.filter_mid_tier(profiles)
        assert [p['creator_id'] for p in result] == ['b', 'c']

    def test_drops_missing_or_invalid_follower_count(self, scraper):
        profiles = [
            {'creator_id': 'x'},                                   # no key
            {'creator_id': 'y', 'follower_count': None},           # None
            {'creator_id': 'z', 'follower_count': '52.3K'},        # wrong type
            {'creator_id': 'w', 'follower_count': 50_000},         # kept
        ]
        result = scraper.filter_mid_tier(profiles)
        assert [p['creator_id'] for p in result] == ['w']

    def test_order_preserved_and_input_untouched(self, scraper):
        profiles = [
            {'creator_id': 'b', 'follower_count': 20_000},
            {'creator_id': 'a', 'follower_count': 30_000},
        ]
        result = scraper.filter_mid_tier(profiles)
        assert [p['creator_id'] for p in result] == ['b', 'a']
        assert len(profiles) == 2


# ===========================================================================
# 3. Fixture-driven 6-field parse (logic verification, UNIVERSAL_DATA path)
# ===========================================================================

class TestCreatorParserFixture:
    @pytest.fixture
    def parsed(self):
        parser = CreatorParser()
        return parser.parse(load_fixture('creator_profile_sample.html'),
                            'homegadget_pro')

    def test_all_fields_present(self, parsed):
        assert set(CREATOR_FIELDS) <= set(parsed)

    def test_creator_id_from_json_uid(self, parsed):
        # JSON user.id (uid) wins over the input unique_id
        assert parsed['creator_id'] == '7309998888777666'

    def test_nickname(self, parsed):
        assert parsed['nickname'] == 'HomeGadget Pro'

    def test_follower_count(self, parsed):
        assert parsed['follower_count'] == 52_300

    def test_product_count(self, parsed):
        assert parsed['product_count'] == 12

    def test_avg_views_30d_median_odd_sample(self, parsed):
        # playCounts [1200, 3400, 2600, 5000, 1800] -> median 2600
        assert parsed['avg_views_30d'] == 2_600

    def test_category_tags_bio_first_then_videos_dedup(self, parsed):
        # bio: TikTokMadeMeBuyIt, homegadgets; videos add kitchenfinds
        # (homegadgets duplicate dropped)
        assert parsed['category_tags'] == \
            'TikTokMadeMeBuyIt,homegadgets,kitchenfinds'

    def test_max_sample_30(self):
        parser = CreatorParser()
        assert parser.selectors['videos']['max_sample']['value'] == 30


# ===========================================================================
# 4. SIGI_STATE fallback path
# ===========================================================================

class TestCreatorParserSigiFallback:
    @pytest.fixture
    def parsed(self):
        parser = CreatorParser()
        return parser.parse(load_fixture('creator_profile_sigi_sample.html'),
                            'oldstyle_creator')

    def test_user_found_via_user_module(self, parsed):
        assert parsed['creator_id'] == '456777888999000'

    def test_nickname(self, parsed):
        assert parsed['nickname'] == 'OldStyle Creator'

    def test_follower_count_suffix_text(self, parsed):
        # "185.4K" string -> 185400
        assert parsed['follower_count'] == 185_400

    def test_avg_views_median_even_sample(self, parsed):
        # playCounts {100, 300, 200, 400} -> median (200+300)/2 = 250
        assert parsed['avg_views_30d'] == 250

    def test_category_tags(self, parsed):
        # bio '#gadgettok' + video descs ('#gadgettok' dup, '#gear')
        assert parsed['category_tags'] == 'gadgettok,gear'


# ===========================================================================
# 5. CSS fallback + degraded data
# ===========================================================================

class TestCreatorParserDegraded:
    def test_product_count_css_fallback_counts_elements(self):
        html = """
        <html><body>
          <script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">
          {"__DEFAULT_SCOPE__": {"webapp.user-detail": {"userInfo":
            {"user": {"id": "1", "nickname": "N", "signature": "hi #tag"},
             "stats": {"followerCount": 5}}}}}
          </script>
          <div data-e2e="showcase">
            <a data-e2e="product-card" href="/shop/product/111111111111111111"></a>
            <a data-e2e="product-card" href="/shop/product/222222222222222222"></a>
          </div>
        </body></html>
        """
        parser = CreatorParser()
        data = parser.parse(html, 'someone')
        assert data['product_count'] == 2

    def test_missing_videos_yield_none_avg_views(self):
        html = """
        <html><body>
          <script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">
          {"__DEFAULT_SCOPE__": {"webapp.user-detail": {"userInfo":
            {"user": {"id": "1", "nickname": "N"}, "stats": {"followerCount": 5}}}}}
          </script>
        </body></html>
        """
        parser = CreatorParser()
        data = parser.parse(html, 'someone')
        assert data['avg_views_30d'] is None
        assert data['category_tags'] is None
        assert data['creator_id'] == '1'


# ===========================================================================
# 6. Empty / blocked pages
# ===========================================================================

class TestCreatorParserEmptyPages:
    @pytest.mark.parametrize('html', [
        '',
        '   ',
        load_fixture('empty_page_sample.html'),
    ])
    def test_no_embedded_json_returns_none(self, html):
        assert CreatorParser().parse(html, 'someone') is None

    def test_malformed_embedded_json_returns_none(self):
        html = ('<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" '
                'type="application/json">{not json</script>')
        assert CreatorParser().parse(html, 'someone') is None


# ===========================================================================
# 7. scrape_profile request path (mock client)
# ===========================================================================

class TestScrapeProfile:
    def test_success(self, scraper):
        page = load_fixture('creator_profile_sample.html')
        scraper.client = MockClient(lambda url: MockResponse(200, page))
        data = scraper.scrape_profile('homegadget_pro')

        assert data['follower_count'] == 52_300
        assert data['nickname'] == 'HomeGadget Pro'
        assert scraper.client.calls[0]['url'] == \
            'https://www.tiktok.com/@homegadget_pro'

    def test_404_returns_none(self, scraper):
        scraper.client = MockClient(lambda url: MockResponse(404, ''))
        assert scraper.scrape_profile('ghost') is None

    def test_unexpected_status_returns_none(self, scraper):
        scraper.client = MockClient(lambda url: MockResponse(500, 'oops'))
        assert scraper.scrape_profile('someone') is None

    def test_network_error_returns_none(self, scraper):
        scraper.client = MockClient(
            lambda url: ConnectionError('boom'))
        assert scraper.scrape_profile('someone') is None

    def test_blocked_page_returns_none(self, scraper):
        scraper.client = MockClient(
            lambda url: MockResponse(200, load_fixture('empty_page_sample.html')))
        assert scraper.scrape_profile('someone') is None

    def test_429_propagates_and_arms_cooldown(self, scraper):
        scraper.client = MockClient(
            lambda url: RateLimitException('429'))
        with pytest.raises(RateLimitException):
            scraper.scrape_profile('someone')
        assert scraper.in_cooldown
        assert scraper.get_cooldown_remaining() > 0

    def test_403_propagates_and_arms_cooldown(self, scraper):
        scraper.client = MockClient(
            lambda url: WAFBlockedException(url, status_code=403))
        with pytest.raises(WAFBlockedException):
            scraper.scrape_profile('someone')
        assert scraper.in_cooldown

    def test_request_during_cooldown_refused(self, scraper):
        scraper.enter_cooldown(reason='test')
        with pytest.raises(RateLimitException, match='cooldown'):
            scraper.scrape_profile('someone')
        # no request was attempted
        assert scraper.client.calls == []

    def test_quota_gate(self, scraper):
        quota_guard = MagicMock(side_effect=QuotaExceededException('creators', 100))
        scraper.check_quota = quota_guard
        with pytest.raises(QuotaExceededException):
            scraper.scrape_profile('someone')
        quota_guard.assert_called_once()
        assert scraper.client.calls == []


# ===========================================================================
# 8. get_showcase_products request path (mock client)
# ===========================================================================

class TestShowcaseProducts:
    SHOWCASE_URL = 'https://www.tiktok.com/@homegadget_pro/showcase'

    def test_success_with_dedup(self, scraper):
        page = load_fixture('creator_showcase_sample.html')
        scraper.client = MockClient(lambda url: MockResponse(200, page))
        ids = scraper.get_showcase_products('homegadget_pro')

        assert ids == [
            '7301234567890123456',
            '7309876543210987654',
            '7305554443332221110',
        ]
        assert scraper.client.calls[0]['url'] == self.SHOWCASE_URL

    def test_404_returns_empty(self, scraper):
        scraper.client = MockClient(lambda url: MockResponse(404, ''))
        assert scraper.get_showcase_products('ghost') == []

    def test_unexpected_status_returns_empty(self, scraper):
        scraper.client = MockClient(lambda url: MockResponse(503, ''))
        assert scraper.get_showcase_products('someone') == []

    def test_network_error_returns_empty(self, scraper):
        scraper.client = MockClient(lambda url: ConnectionError('boom'))
        assert scraper.get_showcase_products('someone') == []

    def test_no_product_links_returns_empty(self, scraper):
        scraper.client = MockClient(
            lambda url: MockResponse(200, '<html><body>empty</body></html>'))
        assert scraper.get_showcase_products('someone') == []

    def test_429_propagates_and_arms_cooldown(self, scraper):
        scraper.client = MockClient(lambda url: RateLimitException('429'))
        with pytest.raises(RateLimitException):
            scraper.get_showcase_products('someone')
        assert scraper.in_cooldown

    def test_request_during_cooldown_refused(self, scraper):
        scraper.enter_cooldown(reason='test')
        with pytest.raises(RateLimitException, match='cooldown'):
            scraper.get_showcase_products('someone')
        assert scraper.client.calls == []


# ===========================================================================
# 9. Selector discipline: nothing verified
# ===========================================================================

class TestSelectorDiscipline:
    def test_verified_entries_carry_evidence(self):
        """Calibration discipline (upgraded 2026-10-07 after first real probe):

        - an entry may flip verified: true ONLY after a real-page probe;
        - every verified:true entry MUST reference its evidence file
          (docs/probe/...) in its note;
        - entries without real-page evidence must stay verified: false.
        """
        parser = CreatorParser()

        def _walk(node):
            if isinstance(node, dict):
                if 'verified' in node:
                    yield node
                for value in node.values():
                    yield from _walk(value)
            elif isinstance(node, list):
                for item in node:
                    yield from _walk(item)

        entries = list(_walk(parser.selectors))
        assert entries, "no verified-flagged entries found in selectors"
        for entry in entries:
            note = entry.get('note', '')
            if entry['verified'] is True:
                assert 'docs/probe/' in note, \
                    f"verified:true entry lacks evidence reference: {entry.get('value')}"
            # 未验证条目无需断言（保持 false 即可）

    def test_quota_uses_creators_bucket(self, scraper):
        assert scraper.RESOURCE_TYPE == 'creators'
        assert common_config.config.DAILY_QUOTA_CREATORS > 0


# ===========================================================================
# 10. Real-page probe fixture (Agent O, 2026-10-07)
# ===========================================================================

class TestCreatorParserRealProbe:
    """Added by Agent O after the first REAL request calibration.

    Fixture is a sanitized excerpt of the live /@khaby.lame guest-SSR page
    (curl_cffi chrome131 via proxy, status 200, evidence
    docs/probe/20261007T144901Z_creator_khaby_lame_try1_*). REALITY it
    encodes: root __DEFAULT_SCOPE__['webapp.user-detail'].userInfo hits,
    stats.followerCount is a rounded int, and userInfo.itemList == [] in
    guest SSR (no embedded videos -> avg_views_30d degrades to None).
    """

    @pytest.fixture
    def parsed(self):
        parser = CreatorParser()
        return parser.parse(load_fixture('creator_probe_khaby_lame.html'),
                            'khaby.lame')

    def test_parses_without_crash(self, parsed):
        assert parsed is not None

    def test_creator_id_from_real_uid(self, parsed):
        assert parsed['creator_id'] == '127905465618821121'

    def test_nickname_from_real_page(self, parsed):
        assert parsed['nickname'] == 'Khabane lame'

    def test_follower_count_from_real_stats(self, parsed):
        assert parsed['follower_count'] == 163_000_000

    def test_guest_ssr_itemlist_empty_degrades_to_none(self, parsed):
        # REALITY: userInfo.itemList == [] in guest SSR
        assert parsed['avg_views_30d'] is None

    def test_bio_without_hashtags_yields_none_category_tags(self, parsed):
        # real signature has no '#...' tokens and no video descs exist
        assert parsed['category_tags'] is None

    def test_product_count_none_for_non_commerce_account(self, parsed):
        # real page: user.commerceUserInfo = {"commerceUser": false}
        assert parsed['product_count'] is None
