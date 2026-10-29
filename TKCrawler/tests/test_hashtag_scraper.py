"""Tests for src/scrapers (Agent G: Hashtag page parsing/scraping).

Offline only — every HTTP interaction goes through a mock client. No real
TikTok requests are made.

Coverage:
- parse_view_count unit tests (pure logic, fully verified)
- Fixture-driven parse (fixture built to match ASSUMED hashtag page
  structure — verifies parsing LOGIC, not the real page; see
  docs/reverse/hashtag-api.md and hashtag_selectors.json, all
  "verified": false)
- UNIVERSAL_DATA / SIGI_STATE / CSS-fallback / empty page paths
- Top video ID extraction (JSON candidates + CSS fallback, dedupe, limit)
- scrape_hashtag / get_top_videos request paths via mock client
- Daily quota gate
- 429/403 soft cooldown (state + exception)
"""

import json
import time
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.common import config as common_config
from src.common.exceptions import (
    QuotaExceededException,
    RateLimitException,
    WAFBlockedException,
)
from src.scrapers.hashtag_scraper import HashtagTrendScraper
from src.scrapers.parsers.hashtag_parser import (
    HASHTAG_FIELDS,
    HashtagParser,
    parse_view_count,
)

FIXTURES = Path(__file__).parent / 'fixtures'

EXPECTED_TOP_VIDEOS = [
    '7301111222333444556',
    '7302222333444555667',
    '7303333444555666778',
    '7304444555666777889',
]


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
    return HashtagTrendScraper(
        client=MockClient(),
        min_interval=0,
        max_interval=0,
        cooldown_seconds=60,
    )


# ===========================================================================
# 1. parse_view_count (verified pure logic)
# ===========================================================================

class TestParseViewCount:
    def test_plain_int(self):
        assert parse_view_count(617000000000) == 617000000000

    def test_plain_float(self):
        assert parse_view_count(1234.5) == 1234

    def test_b_suffix(self):
        assert parse_view_count('61.7B') == 61_700_000_000

    def test_b_suffix_lowercase(self):
        assert parse_view_count('617b') == 617_000_000_000

    def test_m_suffix(self):
        assert parse_view_count('120.5M') == 120_500_000

    def test_k_suffix(self):
        assert parse_view_count('3.4k') == 3400

    def test_suffix_with_plus(self):
        assert parse_view_count('10k+') == 10000

    def test_comma_grouped(self):
        assert parse_view_count('1,234,567') == 1234567

    def test_number_with_noise_text(self):
        assert parse_view_count('61.7B views') == 61_700_000_000

    def test_none(self):
        assert parse_view_count(None) == 0

    def test_empty_string(self):
        assert parse_view_count('') == 0

    def test_garbage(self):
        assert parse_view_count('no views here') == 0

    def test_bool_is_zero(self):
        assert parse_view_count(True) == 0


# ===========================================================================
# 2. Fixture-driven parse (logic verification)
# ===========================================================================

class TestHashtagParserFixture:
    @pytest.fixture
    def parsed(self):
        parser = HashtagParser()
        return parser.parse_hashtag(load_fixture('hashtag_page_sample.html'))

    def test_fields_present(self, parsed):
        assert parsed is not None
        assert 'tag_name' in parsed
        assert 'views_total' in parsed

    def test_tag_name(self, parsed):
        assert parsed['tag_name'] == 'tiktokmademebuyit'

    def test_views_total(self, parsed):
        assert parsed['views_total'] == 617_000_000_000

    def test_selector_evidence_discipline(self):
        """Upgraded discipline (Agent P, 2026-10-07, approved change):

        verified:true entries MUST carry a docs/probe/ evidence reference
        in their note (same rule as creator side). verified:false entries
        remain assumptions pending live calibration.
        """
        selectors = HashtagParser().selectors
        for section in ('urls', 'fields', 'embedded_json', 'top_videos'):
            for name, spec in selectors[section].items():
                if spec.get('verified') is True:
                    assert 'docs/probe/' in spec.get('note', ''), (
                        f"{section}.{name}: verified:true without "
                        f"docs/probe/ evidence reference"
                    )

    def test_sigi_state_fallback(self):
        """Legacy SIGI_STATE node + human-suffixed views string."""
        parser = HashtagParser()
        parsed = parser.parse_hashtag(load_fixture('hashtag_sigi_page.html'))
        assert parsed is not None
        assert parsed['tag_name'] == 'tiktokmademebuyit'
        assert parsed['views_total'] == 61_700_000_000  # "61.7B" -> int

    def test_css_only_page_fallback(self):
        """No embedded JSON at all: h1 + strong text fallbacks."""
        parser = HashtagParser()
        parsed = parser.parse_hashtag(load_fixture('hashtag_css_only_page.html'))
        assert parsed is not None
        assert parsed['tag_name'] == 'fashion'          # '#' stripped
        assert parsed['views_total'] == 120_500_000     # "120.5M"

    def test_empty_html_returns_none(self):
        assert HashtagParser().parse_hashtag('') is None

    def test_shell_page_returns_none(self):
        assert HashtagParser().parse_hashtag(
            load_fixture('hashtag_empty_page.html')
        ) is None


# ===========================================================================
# 3. Top video IDs
# ===========================================================================

class TestTopVideoIds:
    @pytest.fixture
    def parser(self):
        return HashtagParser()

    def test_from_embedded_json(self, parser):
        ids = parser.get_top_video_ids(load_fixture('hashtag_page_sample.html'))
        # 4 extractable IDs (the object without any id field is skipped),
        # page order kept
        assert ids == EXPECTED_TOP_VIDEOS

    def test_limit(self, parser):
        ids = parser.get_top_video_ids(
            load_fixture('hashtag_page_sample.html'), limit=2
        )
        assert ids == EXPECTED_TOP_VIDEOS[:2]

    def test_default_limit_is_30(self, parser):
        html = load_fixture('hashtag_page_sample.html')
        assert parser.get_top_video_ids(html) == parser.get_top_video_ids(
            html, limit=30
        )

    def test_css_fallback_with_dedupe(self, parser):
        ids = parser.get_top_video_ids(
            load_fixture('hashtag_css_only_page.html')
        )
        # duplicate anchor de-duplicated; /video/ only (the /photo/ link
        # is not matched by the assumed regex)
        assert ids == [
            '7301234500000000001',
            '7301234500000000002',
        ]

    def test_empty_html_returns_empty(self, parser):
        assert parser.get_top_video_ids('') == []

    def test_shell_page_returns_empty(self, parser):
        assert parser.get_top_video_ids(
            load_fixture('hashtag_empty_page.html')
        ) == []


# ===========================================================================
# 4. Request paths via mock client
# ===========================================================================

class TestScrapeRequestPaths:
    def test_scrape_hashtag_success(self, scraper):
        scraper.client = MockClient(
            lambda url: MockResponse(200, load_fixture('hashtag_page_sample.html'))
        )
        result = scraper.scrape_hashtag('#tiktokmademebuyit')

        assert result is not None
        assert set(result.keys()) == set(HASHTAG_FIELDS)
        assert result['tag_name'] == 'tiktokmademebuyit'   # '#' stripped
        assert result['views_total'] == 617_000_000_000
        assert result['snapshot_date'] == date.today().isoformat()
        # URL built from (assumed) hashtag_selectors.json template,
        # without the '#' prefix
        assert scraper.client.calls[0]['url'] == \
            'https://www.tiktok.com/tag/tiktokmademebuyit'
        # exactly one request, quota consumed once
        assert len(scraper.client.calls) == 1
        assert scraper.get_quota_status('hashtags')['used'] == 1

    def test_snapshot_fields_match_contract(self, scraper):
        """Contract for SnapshotDiffEngine.save_snapshot(table=
        'hashtag_snapshots'): {tag_name, snapshot_date, views_total}."""
        scraper.client = MockClient(
            lambda url: MockResponse(200, load_fixture('hashtag_page_sample.html'))
        )
        result = scraper.scrape_hashtag('tiktokmademebuyit')
        assert {'tag_name', 'snapshot_date', 'views_total'} <= set(result.keys())

    def test_url_input_wins_over_page_data(self, scraper):
        """Input tag_name wins over page data (same policy as product_id)."""
        scraper.client = MockClient(
            lambda url: MockResponse(200, load_fixture('hashtag_page_sample.html'))
        )
        result = scraper.scrape_hashtag('TikTokMadeMeBuyIt')
        assert result['tag_name'] == 'TikTokMadeMeBuyIt'

    def test_tag_name_url_encoded(self, scraper):
        scraper.client = MockClient(lambda url: MockResponse(404, ''))
        scraper.scrape_hashtag('made me buy it')
        assert scraper.client.calls[0]['url'] == \
            'https://www.tiktok.com/tag/made%20me%20buy%20it'

    def test_scrape_hashtag_404_returns_none(self, scraper):
        scraper.client = MockClient(lambda url: MockResponse(404, 'Not Found'))
        assert scraper.scrape_hashtag('no_such_tag') is None

    def test_scrape_hashtag_500_returns_none(self, scraper):
        scraper.client = MockClient(lambda url: MockResponse(500, 'oops'))
        assert scraper.scrape_hashtag('some_tag') is None

    def test_scrape_hashtag_empty_page_returns_none(self, scraper):
        scraper.client = MockClient(
            lambda url: MockResponse(200, load_fixture('hashtag_empty_page.html'))
        )
        assert scraper.scrape_hashtag('some_tag') is None

    def test_network_error_returns_none(self, scraper):
        client = MockClient()
        client.request = MagicMock(side_effect=ConnectionError('boom'))
        scraper.client = client
        assert scraper.scrape_hashtag('some_tag') is None

    def test_get_top_videos_success(self, scraper):
        scraper.client = MockClient(
            lambda url: MockResponse(200, load_fixture('hashtag_page_sample.html'))
        )
        ids = scraper.get_top_videos('tiktokmademebuyit')
        assert ids == EXPECTED_TOP_VIDEOS
        assert len(scraper.client.calls) == 1

    def test_get_top_videos_limit(self, scraper):
        scraper.client = MockClient(
            lambda url: MockResponse(200, load_fixture('hashtag_page_sample.html'))
        )
        assert scraper.get_top_videos('tiktokmademebuyit', limit=2) == \
            EXPECTED_TOP_VIDEOS[:2]

    def test_get_top_videos_404_returns_empty(self, scraper):
        scraper.client = MockClient(lambda url: MockResponse(404, ''))
        assert scraper.get_top_videos('no_such_tag') == []

    def test_get_top_videos_no_videos_returns_empty(self, scraper):
        scraper.client = MockClient(
            lambda url: MockResponse(200, load_fixture('hashtag_empty_page.html'))
        )
        assert scraper.get_top_videos('dead_tag') == []

    def test_client_used_not_real_network(self, scraper):
        """The injected mock must be used — BaseScraper never builds a real
        client when one is provided."""
        assert isinstance(scraper.client, MockClient)


# ===========================================================================
# 5. Daily quota
# ===========================================================================

class TestDailyQuota:
    def test_quota_exceeded_raises(self, monkeypatch):
        monkeypatch.setattr(common_config.config, 'DAILY_QUOTA_PRODUCTS', 2)
        scraper = HashtagTrendScraper(
            client=MockClient(lambda url: MockResponse(404, '')),
            min_interval=0, max_interval=0,
        )
        scraper.check_quota('hashtags')  # 1
        scraper.check_quota('hashtags')  # 2
        with pytest.raises(QuotaExceededException):
            scraper.scrape_hashtag('some_tag')

        # and the request was never issued
        assert scraper.client.calls == []


# ===========================================================================
# 6. 429/403 soft cooldown
# ===========================================================================

class TestSoftCooldown:
    def test_403_triggers_cooldown_and_reraises(self, scraper):
        scraper.client = MockClient(
            lambda url: WAFBlockedException(url, 403)
        )
        with pytest.raises(WAFBlockedException):
            scraper.scrape_hashtag('some_tag')

        assert scraper.in_cooldown is True
        assert 0 < scraper.get_cooldown_remaining() <= 60

    def test_429_triggers_cooldown_and_reraises(self, scraper):
        scraper.client = MockClient(
            lambda url: RateLimitException('429')
        )
        with pytest.raises(RateLimitException):
            scraper.scrape_hashtag('some_tag')
        assert scraper.in_cooldown is True

    def test_blocked_requests_during_cooldown(self, scraper):
        scraper.client = MockClient(
            lambda url: WAFBlockedException(url, 403)
        )
        with pytest.raises(WAFBlockedException):
            scraper.scrape_hashtag('some_tag')

        calls_after_block = len(scraper.client.calls)
        # During cooldown, scrape_hashtag refuses WITHOUT touching the client
        with pytest.raises(RateLimitException, match='Soft cooldown'):
            scraper.scrape_hashtag('another_tag')
        assert len(scraper.client.calls) == calls_after_block

    def test_cooldown_expires(self):
        scraper = HashtagTrendScraper(
            client=MockClient(
                lambda url: MockResponse(200, load_fixture('hashtag_page_sample.html'))
            ),
            min_interval=0, max_interval=0, cooldown_seconds=0.05,
        )
        scraper.enter_cooldown('test')
        assert scraper.in_cooldown is True
        time.sleep(0.1)
        assert scraper.in_cooldown is False
        # requests resume after expiry
        assert scraper.scrape_hashtag('tiktokmademebuyit') is not None

    def test_reset_cooldown(self, scraper):
        scraper.enter_cooldown('test')
        assert scraper.in_cooldown is True
        scraper.reset_cooldown()
        assert scraper.in_cooldown is False


# ===========================================================================
# 7. Scraper inherits BaseScraper contract
# ===========================================================================

class TestBaseContract:
    def test_inherits_base_scraper(self):
        from src.scrapers.base_scraper import BaseScraper
        assert issubclass(HashtagTrendScraper, BaseScraper)

    def test_scraper_uses_interval_params(self):
        scraper = HashtagTrendScraper(min_interval=3, max_interval=4)
        assert scraper.limiter.min_interval == 3
        assert scraper.limiter.max_interval == 4


# ===========================================================================
# 8. Real-page probe fixture (Agent O, 2026-10-07)
# ===========================================================================

class TestHashtagParserRealProbe:
    """Added by Agent O after the first REAL request calibration.

    Fixture is a sanitized excerpt of the live /tag/webdev guest-SSR page
    (curl_cffi chrome131 via proxy, status 200, evidence
    docs/probe/20261007T144849Z_tag_webdev_try1_*). REALITY it encodes:
    __UNIVERSAL_DATA_FOR_REHYDRATION__ exists and parses, but carries only
    5 non-challenge scope keys (no webapp.challenge-detail / challengeInfo),
    and the DOM is an empty shell (no data-e2e, no /video/ anchors). The
    parser must degrade gracefully: parse -> None, top videos -> [].
    """

    @pytest.fixture
    def shell_html(self):
        return load_fixture('hashtag_probe_webdev_shell.html')

    def test_embedded_json_node_exists_and_parses(self, shell_html):
        data = HashtagParser().extract_embedded_json(shell_html)
        assert data is not None
        assert set(data['__DEFAULT_SCOPE__'].keys()) == {
            'webapp.app-context', 'webapp.biz-context',
            'webapp.i18n-translation', 'seo.abtest', 'webapp.a-b',
        }

    def test_no_challenge_root_returns_none(self, shell_html):
        assert HashtagParser().parse_hashtag(shell_html) is None

    def test_no_video_anchors_returns_empty(self, shell_html):
        assert HashtagParser().get_top_video_ids(shell_html) == []


# ===========================================================================
# 9. Signed-API channel (Agent P, live-verified 2026-10-07)
# ===========================================================================

class TestHashtagParserApi:
    """Agent P's LIVE-verified challenge/detail API channel.

    Fixture is the sanitized real response of
    /api/challenge/detail/?challengeName=webdev (TikTokSigner X-Bogus +
    guest cookies, status 200, statusCode 0, evidence
    docs/probe/20261007T154904Z_api_challenge_detail_body.json).
    REALITY it encodes: tag name field is challenge.title (NOT
    challengeName), viewCount exists at challengeInfo.challenge.stats
    (int, rounded) and challengeInfo.statsV2 (string, precise).
    """

    @pytest.fixture
    def payload_text(self):
        return load_fixture('hashtag_api_probe_webdev_detail.json')

    def test_real_payload_parses(self, payload_text):
        parsed = HashtagParser().parse_hashtag_api(payload_text)
        assert parsed == {'tag_name': 'webdev', 'views_total': 739500000}

    def test_dict_payload_accepted(self, payload_text):
        payload = json.loads(payload_text)
        parsed = HashtagParser().parse_hashtag_api(payload)
        assert parsed['tag_name'] == 'webdev'
        assert parsed['views_total'] == 739500000

    def test_primary_stats_path_wins(self):
        """challengeInfo.challenge.stats.viewCount (verified primary path)
        is preferred; the precise statsV2 string stays documented in the
        selector note for future precision upgrades."""
        payload = {
            'statusCode': 0,
            'challengeInfo': {
                'challenge': {'title': 'foo', 'stats': {'viewCount': 100}},
                'statsV2': {'viewCount': '739512730'},
            },
        }
        parsed = HashtagParser().parse_hashtag_api(payload)
        assert parsed == {'tag_name': 'foo', 'views_total': 100}

    def test_nonzero_status_code_returns_none(self, payload_text):
        payload = json.loads(payload_text)
        payload['statusCode'] = 10216
        assert HashtagParser().parse_hashtag_api(payload) is None

    def test_missing_challenge_info_returns_none(self):
        assert HashtagParser().parse_hashtag_api({'statusCode': 0}) is None

    def test_invalid_json_returns_none(self):
        assert HashtagParser().parse_hashtag_api('<html>blocked</html>') is None

    def test_empty_body_returns_none(self):
        assert HashtagParser().parse_hashtag_api('') is None

    def test_tag_name_hash_stripped(self):
        payload = {'statusCode': 0, 'challengeInfo': {
            'challenge': {'title': '#webdev'}}}
        parsed = HashtagParser().parse_hashtag_api(payload)
        assert parsed['tag_name'] == 'webdev'
        assert parsed['views_total'] == 0


class TestScrapeHashtagApi:
    """Scraper-level API channel: URL building, signing, headers, contract."""

    def test_api_success(self, scraper):
        scraper.client = MockClient(
            lambda url: MockResponse(
                200, load_fixture('hashtag_api_probe_webdev_detail.json'))
        )
        result = scraper.scrape_hashtag_api('webdev')

        assert result is not None
        assert set(result.keys()) == set(HASHTAG_FIELDS)
        assert result['tag_name'] == 'webdev'
        assert result['views_total'] == 739500000
        assert result['snapshot_date'] == date.today().isoformat()
        assert scraper.get_quota_status('hashtags')['used'] == 1

    def test_request_is_signed_and_shaped_like_the_probe(self, scraper):
        scraper.client = MockClient(
            lambda url: MockResponse(
                200, load_fixture('hashtag_api_probe_webdev_detail.json'))
        )
        scraper.scrape_hashtag_api('#webdev')

        call = scraper.client.calls[0]
        assert call['method'] == 'GET'
        # endpoint + challengeName from the (verified) selector template
        assert '/api/challenge/detail/' in call['url']
        assert 'challengeName=webdev' in call['url']
        # X-Bogus appended to the query (canonical verified placement)
        assert 'X-Bogus=' in call['url']
        # guest dummy msToken in query AND Cookie, matching the probe
        assert 'msToken=' in call['url']
        assert call['headers']['Cookie'].startswith('msToken=')
        # UA bound into the signature
        assert 'Chrome/131' in call['headers']['User-Agent']
        # baseline params that made the live request accepted
        assert 'aid=1988' in call['url']
        assert 'device_platform=web_pc' in call['url']

    def test_empty_body_gated_response_returns_none(self, scraper):
        """The verified failure mode of the gated list endpoints: 200 +
        empty body must degrade to None (never crash)."""
        scraper.client = MockClient(lambda url: MockResponse(200, ''))
        assert scraper.scrape_hashtag_api('webdev') is None

    def test_404_returns_none(self, scraper):
        scraper.client = MockClient(lambda url: MockResponse(404, ''))
        assert scraper.scrape_hashtag_api('no_such_tag') is None

    def test_403_propagates_waf_exception(self, scraper):
        scraper.client = MockClient(lambda url: WAFBlockedException(url, 403))
        with pytest.raises(WAFBlockedException):
            scraper.scrape_hashtag_api('webdev')
        assert scraper.in_cooldown is True

    def test_network_error_returns_none(self, scraper):
        client = MockClient()
        client.request = MagicMock(side_effect=ConnectionError('boom'))
        scraper.client = client
        assert scraper.scrape_hashtag_api('webdev') is None
