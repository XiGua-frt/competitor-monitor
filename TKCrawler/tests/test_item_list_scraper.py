"""Tests for src/scrapers/item_list_scraper.py (ItemListScraper).

Offline only — every HTTP interaction goes through a mock client and the
session manager / signer are injected mocks. No real TikTok requests,
no browser. Live verification of the request shape was done by the probe
(docs/probe/itemlist_retest_2026-10-08.md, verified: true); these tests
verify the scraper faithfully reproduces that shape.

Coverage:
- URL construction: verified baseline params, secUid/count/cursor,
  real msToken injection, Referer embedding
- Signing: signer receives the unsigned URL, request carries X-Bogus
  in query AND headers, full login cookie jar in headers
- Response parsing: probe-fixture-driven (trimmed real response),
  item field mapping, empty itemList
- Error paths: statusCode != 0 (both casings), HTTP 404/500,
  malformed JSON, network error, quota gate
- Session: expired session / missing msToken -> SessionExpiredException
- Pagination (untested: pagination — logic tests only): hasMore/cursor
  loop, max_pages cap, stale-cursor guard, partial-data preservation
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.common.exceptions import (
    QuotaExceededException,
    RateLimitException,
    SessionExpiredException,
    WAFBlockedException,
)
from src.scrapers.item_list_scraper import (
    ITEM_LIST_FIELDS,
    ItemListScraper,
)

FIXTURES = Path(__file__).parent / 'fixtures'

SEC_UID = 'MS4wLjABAAAAnrn369W7f04wKrl5_r3jZ7pn-M-OQH04eBPxpruRceppgs7T2mWZvEhMF6VBpK0x'
UNIQUE_ID = 'dalil3185'
MOCK_MS_TOKEN = 'IGKiGsX_C0HUk3FwgVnY5Mb_GYNp_jTEUVhtUV0dx-WMAs_lN'

LOGIN_COOKIES = {
    'sessionid': 'sessionid-value',
    'sid_guard': 'sid_guard-value',
    'ttwid': 'ttwid-value',
    'msToken': MOCK_MS_TOKEN,
}


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding='utf-8')


def fixture_dict() -> dict:
    return json.loads(load_fixture('itemlist_response_sample.json'))


def page_payload(items=None, has_more=False, cursor='-1', status_code=0):
    """Build a response payload mirroring the verified probe structure."""
    data = fixture_dict()
    if items is not None:
        data['itemList'] = items
    data['hasMore'] = has_more
    data['cursor'] = cursor
    data['statusCode'] = status_code
    data['status_code'] = status_code
    return data


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


class MockSigner:
    """Records signing calls; appends a fixed X-Bogus to the query."""

    def __init__(self):
        self.sign_calls = []
        self.headers_calls = []

    def sign_url(self, url):
        self.sign_calls.append(url)
        return f'{url}&X-Bogus=fakebogusvalue'

    def headers_for(self, url):
        self.headers_calls.append(url)
        return {'User-Agent': 'mock-ua', 'X-Bogus': 'fakebogusvalue'}


class MockSessionManager:
    def __init__(self, cookies=None, expired=False):
        self._cookies = (LOGIN_COOKIES if cookies is None else cookies)
        self._expired = expired

    def get_cookies(self):
        if self._expired:
            raise SessionExpiredException("Session expired")
        return self._cookies


def make_scraper(responses=None, session=None):
    """ItemListScraper with zero waits and fully mocked dependencies."""
    return ItemListScraper(
        client=MockClient(responses),
        signer=MockSigner(),
        session_manager=session if session is not None
        else MockSessionManager(),
        min_interval=0,
        max_interval=0,
        cooldown_seconds=60,
    )


def ok_page(**payload_kwargs) -> MockResponse:
    return MockResponse(200, json.dumps(page_payload(**payload_kwargs)))


# ===========================================================================
# 1. URL construction (verified probe shape)
# ===========================================================================

class TestBuildUrl:
    def test_contains_verified_baseline_params(self):
        scraper = make_scraper()
        url = scraper._build_url(SEC_UID, count=15, cursor=0,
                                 ms_token=MOCK_MS_TOKEN)
        assert url.startswith('https://www.tiktok.com/api/post/item_list/?')
        assert 'aid=1988' in url
        assert 'from_page=user' in url
        assert 'sourceType=8' in url
        assert 'coverFormat=2' in url
        assert 'device_platform=web_pc' in url
        assert 'needPinnedItemIds=false' in url
        assert 'withQABis=true' in url

    def test_sec_uid_count_cursor_injected(self):
        scraper = make_scraper()
        url = scraper._build_url(SEC_UID, count=20, cursor=30,
                                 ms_token=MOCK_MS_TOKEN)
        assert f'secUid={SEC_UID}' in url
        assert 'count=20' in url
        assert 'cursor=30' in url

    def test_real_ms_token_injected(self):
        scraper = make_scraper()
        url = scraper._build_url(SEC_UID, ms_token=MOCK_MS_TOKEN)
        assert f'msToken={MOCK_MS_TOKEN}' in url

    def test_referer_with_unique_id(self):
        scraper = make_scraper()
        url = scraper._build_url(SEC_UID, unique_id=UNIQUE_ID,
                                 ms_token=MOCK_MS_TOKEN)
        assert f'referer=https%3A%2F%2Fwww.tiktok.com%2F%40{UNIQUE_ID}' in url

    def test_referer_fallback_without_unique_id(self):
        scraper = make_scraper()
        url = scraper._build_url(SEC_UID, ms_token=MOCK_MS_TOKEN)
        assert 'referer=https%3A%2F%2Fwww.tiktok.com%2F' in url


# ===========================================================================
# 2. Request shape: signing + cookies + msToken (verified probe contract)
# ===========================================================================

class TestFetchRequestShape:
    def test_signed_url_and_headers(self):
        client = MockClient(lambda url: ok_page())
        scraper = make_scraper(responses=lambda url: ok_page())
        scraper.client = client
        scraper.fetch_page(SEC_UID, unique_id=UNIQUE_ID)

        call = client.calls[0]
        # X-Bogus appended to the query by the signer
        assert call['url'].endswith('&X-Bogus=fakebogusvalue')
        # Same-value header copy
        assert call['headers']['X-Bogus'] == 'fakebogusvalue'
        assert call['headers']['User-Agent'] == 'mock-ua'

    def test_signer_receives_unsigned_url_with_ms_token(self):
        signer = MockSigner()
        scraper = make_scraper()
        scraper.signer = signer
        scraper.fetch_page(SEC_UID, cursor=0, count=15)

        signed_input = signer.sign_calls[0]
        assert 'X-Bogus' not in signed_input  # signer adds it, not us
        assert f'secUid={SEC_UID}' in signed_input
        assert 'cursor=0' in signed_input
        assert 'count=15' in signed_input
        assert f'msToken={MOCK_MS_TOKEN}' in signed_input
        # headers_for gets the same (pre-signing) URL so the value matches
        assert signer.headers_calls == [signed_input]

    def test_full_login_cookie_jar_sent(self):
        client = MockClient(lambda url: ok_page())
        scraper = make_scraper()
        scraper.client = client
        scraper.fetch_page(SEC_UID)

        cookie_header = client.calls[0]['headers']['Cookie']
        for key, value in LOGIN_COOKIES.items():
            assert f'{key}={value}' in cookie_header
        assert '; ' in cookie_header  # full jar, not a single cookie

    def test_ms_token_injected_from_session_cookies(self):
        client = MockClient(lambda url: ok_page())
        scraper = make_scraper()
        scraper.client = client
        scraper.fetch_page(SEC_UID)

        url = client.calls[0]['url']
        assert f'msToken={MOCK_MS_TOKEN}' in url

    def test_missing_ms_token_raises_session_expired(self):
        scraper = make_scraper(
            session=MockSessionManager(
                cookies={'sessionid': 'x'}  # no msToken
            )
        )
        with pytest.raises(SessionExpiredException):
            scraper.fetch_page(SEC_UID)

    def test_expired_session_propagates(self):
        scraper = make_scraper(
            session=MockSessionManager(expired=True)
        )
        with pytest.raises(SessionExpiredException):
            scraper.fetch_page(SEC_UID)


# ===========================================================================
# 3. Response parsing (probe-fixture driven)
# ===========================================================================

class TestParsePage:
    def test_parses_probe_fixture(self):
        scraper = make_scraper(lambda url: ok_page())
        result = scraper.fetch_page(SEC_UID, unique_id=UNIQUE_ID)

        assert result is not None
        assert len(result['videos']) == 1
        video = result['videos'][0]
        # Values straight from the verified probe response
        assert video['video_id'] == '7636064756910034197'
        assert video['author_id'] == '7595450576105374741'
        assert video['sec_uid'] == SEC_UID
        assert video['unique_id'] == UNIQUE_ID
        assert video['desc'] == ('Welcome to Zhangjiajie China '
                                 '#Zhangjiajie #chinatravel')
        assert video['create_time'] == 1777909876
        assert video['play_count'] == 578
        assert video['digg_count'] == 13
        assert video['collect_count'] == 1
        assert video['comment_count'] == 0
        assert video['share_count'] == 0

    def test_has_more_false_cursor_minus_one(self):
        scraper = make_scraper(lambda url: ok_page())
        result = scraper.fetch_page(SEC_UID)
        assert result['has_more'] is False
        assert result['cursor'] == -1

    def test_output_fields_contract(self):
        scraper = make_scraper(lambda url: ok_page())
        result = scraper.fetch_page(SEC_UID)
        assert set(result['videos'][0].keys()) == set(ITEM_LIST_FIELDS)

    def test_empty_item_list(self):
        scraper = make_scraper(lambda url: ok_page(items=[]))
        result = scraper.fetch_page(SEC_UID)
        assert result == {'videos': [], 'has_more': False, 'cursor': -1}

    def test_missing_stats_and_author_default(self):
        item = {'id': '123', 'desc': 'bare'}
        scraper = make_scraper(lambda url: ok_page(items=[item]))
        result = scraper.fetch_page(SEC_UID)
        video = result['videos'][0]
        assert video['author_id'] is None
        assert video['sec_uid'] is None
        assert video['unique_id'] is None
        assert video['create_time'] is None
        assert video['play_count'] == 0
        assert video['digg_count'] == 0
        assert video['collect_count'] == 0
        assert video['comment_count'] == 0
        assert video['share_count'] == 0

    def test_author_id_coerced_to_string(self):
        # author.id must land as a string form (numeric-tolerant); the
        # DB column video_snapshots.author_id is varchar(50).
        item = {'id': '456', 'author': {'id': 7595450576105374741},
                'desc': 'numeric author id'}
        scraper = make_scraper(lambda url: ok_page(items=[item]))
        video = scraper.fetch_page(SEC_UID)['videos'][0]
        assert video['author_id'] == '7595450576105374741'
        assert isinstance(video['author_id'], str)

    def test_author_id_none_when_author_id_key_missing(self):
        # author present but no id key -> None, other fields still parsed
        item = {'id': '789', 'author': {'secUid': SEC_UID,
                                        'uniqueId': UNIQUE_ID}}
        scraper = make_scraper(lambda url: ok_page(items=[item]))
        video = scraper.fetch_page(SEC_UID)['videos'][0]
        assert video['author_id'] is None
        assert video['sec_uid'] == SEC_UID
        assert video['unique_id'] == UNIQUE_ID

    def test_cursor_coerced_to_int(self):
        scraper = make_scraper(
            lambda url: ok_page(has_more=True, cursor='15')
        )
        result = scraper.fetch_page(SEC_UID)
        assert result['cursor'] == 15


# ===========================================================================
# 4. Error paths
# ===========================================================================

class TestErrorPaths:
    def test_status_code_camel_case_nonzero(self):
        payload = page_payload(status_code=10204)
        payload.pop('status_code')  # only camelCase present
        scraper = make_scraper(
            lambda url: MockResponse(200, json.dumps(payload))
        )
        assert scraper.fetch_page(SEC_UID) is None

    def test_status_code_snake_case_nonzero(self):
        payload = page_payload(status_code=5)
        payload.pop('statusCode')  # only snake_case present
        scraper = make_scraper(
            lambda url: MockResponse(200, json.dumps(payload))
        )
        assert scraper.fetch_page(SEC_UID) is None

    def test_http_404(self):
        scraper = make_scraper(lambda url: MockResponse(404, 'not found'))
        assert scraper.fetch_page(SEC_UID) is None

    def test_http_500(self):
        scraper = make_scraper(lambda url: MockResponse(500, 'oops'))
        assert scraper.fetch_page(SEC_UID) is None

    def test_malformed_json(self):
        scraper = make_scraper(lambda url: MockResponse(200, '<html>blocked'))
        assert scraper.fetch_page(SEC_UID) is None

    def test_network_error_returns_none(self):
        scraper = make_scraper(
            lambda url: ConnectionError('reset by peer')
        )
        assert scraper.fetch_page(SEC_UID) is None

    def test_waf_block_propagates(self):
        scraper = make_scraper(
            lambda url: WAFBlockedException(url, 403)
        )
        with pytest.raises(WAFBlockedException):
            scraper.fetch_page(SEC_UID)

    def test_rate_limit_propagates(self):
        scraper = make_scraper(lambda url: RateLimitException('429'))
        with pytest.raises(RateLimitException):
            scraper.fetch_page(SEC_UID)

    def test_quota_exceeded_propagates(self, monkeypatch):
        from src.common.config import config

        scraper = make_scraper(lambda url: ok_page())
        monkeypatch.setattr(config, 'DAILY_QUOTA_VIDEOS', 0)
        with pytest.raises(QuotaExceededException):
            scraper.fetch_page(SEC_UID)


# ===========================================================================
# 5. Pagination (untested: pagination — logic-only coverage)
# ===========================================================================

class TestPagination:
    def test_stops_on_has_more_false(self):
        client = MockClient(lambda url: ok_page(has_more=False, cursor='-1'))
        scraper = make_scraper()
        scraper.client = client

        videos = scraper.scrape_user_videos(SEC_UID, max_pages=5)
        assert len(client.calls) == 1
        assert len(videos) == 1

    def test_paginates_with_server_cursor(self):
        page_two = [dict(fixture_dict()['itemList'][0],
                         id='7636064756910034198')]
        responses = [
            ok_page(has_more=True, cursor='15'),
            ok_page(items=page_two, has_more=False, cursor='-1'),
        ]
        client = MockClient(lambda url: responses.pop(0))
        scraper = make_scraper()
        scraper.client = client

        videos = scraper.scrape_user_videos(SEC_UID, max_pages=5)
        assert len(client.calls) == 2
        assert 'cursor=0' in client.calls[0]['url']
        assert 'cursor=15' in client.calls[1]['url']
        assert [v['video_id'] for v in videos] == [
            '7636064756910034197', '7636064756910034198'
        ]

    def test_max_pages_cap(self):
        responses = [ok_page(has_more=True, cursor='15'),
                     ok_page(has_more=True, cursor='30'),
                     ok_page(has_more=True, cursor='45')]
        client = MockClient(lambda url: responses.pop(0))
        scraper = make_scraper()
        scraper.client = client

        scraper.scrape_user_videos(SEC_UID, max_pages=2)
        assert len(client.calls) == 2

    def test_stale_cursor_guard(self):
        # hasMore stays true but the cursor never advances -> stop, don't spin
        responses = [ok_page(has_more=True, cursor='0'),
                     ok_page(has_more=True, cursor='0')]
        client = MockClient(lambda url: responses.pop(0))
        scraper = make_scraper()
        scraper.client = client

        scraper.scrape_user_videos(SEC_UID, max_pages=5)
        assert len(client.calls) == 1

    def test_decreasing_cursor_continues(self):
        # Verified 2026-10-08 E2E (@khaby.lame): item_list cursor is
        # createTime-based and DECREASES across pages (newest first).
        # The old `next_cursor <= cursor` guard wrongly stopped here.
        responses = [ok_page(has_more=True, cursor='1778776278000'),
                     ok_page(has_more=True, cursor='1755177953000'),
                     ok_page(has_more=False, cursor='-1')]
        client = MockClient(lambda url: responses.pop(0))
        scraper = make_scraper()
        scraper.client = client

        videos = scraper.scrape_user_videos(SEC_UID, max_pages=5)
        assert len(client.calls) == 3
        assert 'cursor=1778776278000' in client.calls[1]['url']
        assert 'cursor=1755177953000' in client.calls[2]['url']
        assert len(videos) == 3  # 1 + 1 + 1 (fixture default single item)

    def test_garbled_cursor_falls_back_to_offset(self):
        responses = [ok_page(has_more=True, cursor='not-a-number'),
                     ok_page(has_more=False, cursor='-1')]
        client = MockClient(lambda url: responses.pop(0))
        scraper = make_scraper()
        scraper.client = client

        scraper.scrape_user_videos(SEC_UID, max_pages=3, count=15)
        assert 'cursor=15' in client.calls[1]['url']

    def test_first_page_failure_returns_none(self):
        scraper = make_scraper(lambda url: MockResponse(404, ''))
        assert scraper.scrape_user_videos(SEC_UID, max_pages=3) is None

    def test_later_page_failure_keeps_partial_data(self):
        responses = [ok_page(has_more=True, cursor='15'),
                     MockResponse(500, 'oops')]
        client = MockClient(lambda url: responses.pop(0))
        scraper = make_scraper()
        scraper.client = client

        videos = scraper.scrape_user_videos(SEC_UID, max_pages=3)
        assert len(videos) == 1  # rows from the first page survive

    def test_quota_exhausted_mid_loop_keeps_partial(self, monkeypatch):
        from src.common.config import config

        responses = [ok_page(has_more=True, cursor='15'),
                     ok_page(has_more=False, cursor='-1')]
        client = MockClient(lambda url: responses.pop(0))
        scraper = make_scraper()
        scraper.client = client
        # Daily bucket allows exactly one page request
        monkeypatch.setattr(config, 'DAILY_QUOTA_VIDEOS', 1)

        videos = scraper.scrape_user_videos(SEC_UID, max_pages=3)
        assert len(videos) == 1
        assert len(client.calls) == 1


# ===========================================================================
# 6. Package export + integration with the real signer
# ===========================================================================

class TestExportAndRealSigner:
    def test_package_export(self):
        import src.scrapers as scrapers_pkg
        assert scrapers_pkg.ItemListScraper is ItemListScraper
        assert 'ItemListScraper' in scrapers_pkg.__all__

    def test_real_signer_produces_x_bogus(self):
        """The real TikTokSigner must accept the built URL (the query is
        what gets signed; the probe verified its output server-side)."""
        from src.scrapers.signer import TikTokSigner

        scraper = make_scraper()
        scraper.signer = TikTokSigner()
        client = MockClient(lambda url: ok_page())
        scraper.client = client

        result = scraper.fetch_page(SEC_UID)
        assert result is not None
        url = client.calls[0]['url']
        assert 'X-Bogus=' in url
        xb_value = url.split('X-Bogus=')[1].split('&')[0]
        assert len(xb_value) == 28
