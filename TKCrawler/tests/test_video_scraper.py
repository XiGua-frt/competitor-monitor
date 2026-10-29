"""Tests for Agent E: video stream scraper + X-Bogus signer.

Offline only — every HTTP interaction goes through a mock client backed by
fixtures. No real TikTok requests are made.

Coverage:
- TikTokSigner: determinism, 28-char output over the custom charset,
  cross-implementation match against a verbatim reference port (armxe
  bogus.py), UA binding, sign_func injection, update_config drift patch
- VideoParser: API item field mapping, is_paid candidates, publish_time
  normalization, product-page embedded-JSON path, missing/None tolerance
- VideoStreamScraper: scrape_product_videos via mock client, per-row daily
  quota, 429/403 soft cooldown, env-configured 8-12s intervals, page
  fallback, snapshot engine contract, parse_video_stats
"""

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.common.config import config as common_config
from src.common.exceptions import (
    QuotaExceededException,
    RateLimitException,
    WAFBlockedException,
)
from src.scrapers.signer import (
    DEFAULT_CHARSET,
    TikTokSigner,
    custom_b64_encode,
    rc4_encrypt,
)
from src.scrapers.parsers.video_parser import VIDEO_FIELDS, VideoParser
from src.scrapers.video_scraper import VideoStreamScraper

FIXTURES = Path(__file__).parent / 'fixtures'

FIXED_TS = 1720000000
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
QUERY = 'aid=1988&product_id=123456789&count=10&msToken=abc'


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding='utf-8')


class MockResponse:
    def __init__(self, status_code=200, text=''):
        self.status_code = status_code
        self.text = text


class MockClient:
    """Mock AntiDetectionClient: request(url, method, headers, **kwargs)."""

    def __init__(self, responses=None):
        self.responses = responses or (lambda url: MockResponse(200, ''))
        self.calls = []

    def request(self, url, method='GET', headers=None, **kwargs):
        self.calls.append({'url': url, 'method': method,
                           'headers': headers, 'kwargs': kwargs})
        result = self.responses(url)
        if isinstance(result, Exception):
            raise result
        return result


# ---------------------------------------------------------------------------
# X-Bogus signer
# ---------------------------------------------------------------------------

class TestSigner:
    def test_reference_match(self):
        """Cross-check against a verbatim port of the public armxe
        Web/bogus.py implementation (independent reproduction)."""
        ref = self._reference_x_bogus(QUERY, UA, FIXED_TS)
        signer = TikTokSigner(user_agent=UA)
        mine = signer.generate_x_bogus(
            f"https://www.tiktok.com/api/x?{QUERY}", timestamp=FIXED_TS)
        assert mine == ref

    def _reference_x_bogus(self, params, ua, ts):
        """Verbatim port of armxe/tiktok-api Web/bogus.py (kept byte-faithful
        on purpose: it is the Evidence for signer.py's correctness)."""
        from hashlib import md5

        def md5_2x(s):
            return md5(md5(s.encode()).digest()).digest()

        def rc4(pt, key):
            s = list(range(256)); idx = 0
            for _ in range(256):
                idx = (idx + s[_] + key[_ % len(key)]) % 256
                s[_], s[idx] = s[idx], s[_]
            a = 0; idx = 0; out = ''
            for ch in pt:
                a = (a + 1) % 256; idx = (idx + s[a]) % 256
                s[a], s[idx] = s[idx], s[a]
                out += chr(ord(ch) ^ s[(s[a] + s[idx]) % 256])
            return out

        def b64(string, kt='ABCDEFGHIJKLMNOPQRSTUVWXYZ'
                           'abcdefghijklmnopqrstuvwxyz0123456789+/='):
            ll = []
            for i in range(0, len(string), 3):
                try:
                    n1, n2, n3 = (ord(string[i]), ord(string[i + 1]),
                                  ord(string[i + 2]))
                    a1 = n1 >> 2
                    a2 = (3 & n1) << 4 | (n2 >> 4)
                    a3 = ((15 & n2) << 2) | (n3 >> 6)
                    a4 = 63 & n3
                except IndexError:
                    a1 = n1 >> 2
                    a2 = ((3 & n1) << 4) | 0
                    a3 = 64
                    a4 = 64
                ll += [a1, a2, a3, a4]
            return ''.join(kt[v] for v in ll)

        def checksum(sl):
            c = 64
            _ = [c := c ^ x for x in sl[3:]]
            return c

        md5_data = md5_2x('')
        md5_params = md5_2x(params)
        md5_ua = md5(b64(rc4(ua, [0, 1, 14])).encode()).digest()
        sl = [ts, 536919696, 64, 0, 1, 14,
              md5_params[-2], md5_params[-1],
              md5_data[-2], md5_data[-1],
              md5_ua[-2], md5_ua[-1]]
        sl.extend([(ts >> i) & 0xFF for i in range(24, -1, -8)])
        sl.extend([(sl[1] >> i) & 0xFF for i in range(24, -1, -8)])
        sl.extend([checksum(sl), 255])
        nl = [sl[x - 1] for x in
              [3, 5, 7, 9, 11, 13, 15, 17, 19, 21,
               4, 6, 8, 10, 12, 14, 16, 18, 20]]
        scr = ''.join(chr(_) for _ in
                      [nl[0], nl[10], nl[1], nl[11], nl[2], nl[12],
                       nl[3], nl[13], nl[4], nl[14], nl[5], nl[15],
                       nl[6], nl[16], nl[7], nl[17], nl[8], nl[18], nl[9]])
        rc = rc4(scr, [255])
        return b64('\x02ÿ' + rc,
                   'Dkdpgh4ZKsQB80/Mfvw36XI1R25-WUAlEi7NLboqYTOPuzmFjJnry'
                   'x9HVGcaStCe')

    def test_output_shape_and_determinism(self):
        signer = TikTokSigner(user_agent=UA)
        url = f"https://www.tiktok.com/api/shop/product/videos?{QUERY}"
        a = signer.generate_x_bogus(url, timestamp=FIXED_TS)
        b = signer.generate_x_bogus(url, timestamp=FIXED_TS)
        assert a == b
        assert len(a) == 28
        assert all(c in DEFAULT_CHARSET for c in a)
        # timestamp drift changes the signature (embedded in salt list)
        c = signer.generate_x_bogus(url, timestamp=FIXED_TS + 1)
        assert c != a

    def test_ua_binding(self):
        signer = TikTokSigner(user_agent=UA)
        url = f"https://www.tiktok.com/api/x?{QUERY}"
        base = signer.generate_x_bogus(url, timestamp=FIXED_TS)
        other = signer.generate_x_bogus(url, user_agent=UA + 'x',
                                        timestamp=FIXED_TS)
        assert base != other

    def test_bare_query_string_accepted(self):
        signer = TikTokSigner(user_agent=UA)
        from_url = signer.generate_x_bogus(f"https://t.com/api?{QUERY}",
                                           timestamp=FIXED_TS)
        from_bare = signer.generate_x_bogus(QUERY, timestamp=FIXED_TS)
        from_qmark = signer.generate_x_bogus(f"?{QUERY}", timestamp=FIXED_TS)
        assert from_url == from_bare == from_qmark

    def test_sign_url_appends(self):
        signer = TikTokSigner(user_agent=UA)
        url = f"https://www.tiktok.com/api/x?{QUERY}"
        signed = signer.sign_url(url)
        assert signed.startswith(url)
        assert 'X-Bogus=' in signed
        xb = signed.split('X-Bogus=')[-1]
        assert xb == signer.generate_x_bogus(url)

    def test_sign_func_injection(self):
        """Whole-algorithm replacement: sign_func wins over builtin."""
        calls = []

        def fake_sign(url, ua):
            calls.append((url, ua))
            return 'INJECTED' + '0' * 20

        signer = TikTokSigner(user_agent=UA, sign_func=fake_sign)
        url = f"https://www.tiktok.com/api/x?{QUERY}"
        assert signer.generate_x_bogus(url) == 'INJECTED' + '0' * 20
        assert calls == [(url, UA)]
        # update_config must not resurrect the builtin while injected
        signer.update_config(magic=1)
        assert signer.generate_x_bogus(url) == 'INJECTED' + '0' * 20

    def test_update_config_drift(self):
        """Constant drift (page JS change) changes output via config only."""
        url = f"https://www.tiktok.com/api/x?{QUERY}"
        s1 = TikTokSigner(user_agent=UA)
        s2 = TikTokSigner(user_agent=UA, config={'magic': 536919697})
        assert s1.generate_x_bogus(url, timestamp=FIXED_TS) != \
            s2.generate_x_bogus(url, timestamp=FIXED_TS)
        s1.update_config(charset='A' * 64)
        drifted = s1.generate_x_bogus(url, timestamp=FIXED_TS)
        assert set(drifted) <= {'A'}

    def test_rc4_and_custom_b64_units(self):
        # RC4 sanity: keystream is deterministic, encrypt==decrypt shape
        data = b'hello world'
        key = bytes([0, 1, 14])
        once = rc4_encrypt(data, key)
        assert rc4_encrypt(once, key) == data  # XOR symmetry
        assert once != data
        # custom b64 over 3-byte blocks matches bit-packing expectations
        encoded = custom_b64_encode(b'\x00\x00\x00', DEFAULT_CHARSET)
        assert encoded == DEFAULT_CHARSET[0] * 4

    def test_live_time_used_by_default(self, monkeypatch):
        signer = TikTokSigner(user_agent=UA)
        url = f"https://www.tiktok.com/api/x?{QUERY}"
        monkeypatch.setattr(time, 'time', lambda: FIXED_TS)
        assert signer.generate_x_bogus(url) == \
            signer.generate_x_bogus(url, timestamp=FIXED_TS)


# ---------------------------------------------------------------------------
# VideoParser
# ---------------------------------------------------------------------------

class TestVideoParser:
    def setup_method(self):
        self.parser = VideoParser(product_id='7234567890123456789')

    def _api_fixture_roots(self):
        data = json.loads(load_fixture('video_api_response.json'))
        return self.parser.find_video_roots(data)

    def test_find_video_roots_api_envelope(self):
        roots = self._api_fixture_roots()
        assert len(roots) == 10
        assert roots[0]['id'] == '7300000000000000001'

    def test_parse_video_item_all_fields(self):
        item = self.parser.parse_video_item(self._api_fixture_roots()[0])
        assert set(item) == set(VIDEO_FIELDS)
        assert item['video_id'] == '7300000000000000001'
        assert item['product_id'] == '7234567890123456789'
        assert item['author_id'] == '6510000000000000001'
        # 1720000000 epoch == 2024-07-03T09:46:40Z
        assert item['publish_time'] == '2024-07-03T09:46:40Z'
        assert item['views'] == 1250000
        assert item['likes'] == 88000
        assert item['comments'] == 1200
        assert item['shares'] == 3400
        assert item['duration'] == 15300
        assert item['is_paid'] is False

    def test_is_paid_candidates(self):
        roots = self._api_fixture_roots()
        # item 2: ad_info.is_ad truthy
        assert self.parser.parse_video_item(roots[1])['is_paid'] is True
        # item 6: commerce_info.is_commerce truthy
        assert self.parser.parse_video_item(roots[5])['is_paid'] is True
        # page fixture item: is_ad_fake truthy
        page_items = self.parser.parse_product_page(
            load_fixture('video_product_page.html'))
        assert page_items[1]['is_paid'] is True

    def test_zero_and_missing_stats(self):
        item = self.parser.parse_video_item(self._api_fixture_roots()[4])
        assert (item['views'], item['likes'], item['comments'],
                item['shares']) == (0, 0, 0, 0)
        assert item['is_paid'] is False

    def test_unusable_items(self):
        assert self.parser.parse_video_item({}) is None  # no video_id
        assert self.parser.parse_video_item(None) is None
        # broken publish_time -> None field, others intact
        item = self.parser.parse_video_item(
            {'id': '1', 'create_time': 'not-a-date'})
        assert item['publish_time'] is None
        assert item['video_id'] == '1'

    def test_page_fallback_parse(self):
        items = self.parser.parse_product_page(
            load_fixture('video_product_page.html'))
        assert len(items) == 2
        assert items[0]['video_id'] == '7300000000000000011'
        assert items[0]['views'] == 555000
        assert items[0]['duration'] == 24500

    def test_page_empty_html(self):
        assert self.parser.parse_product_page('') == []
        assert self.parser.parse_product_page('<html></html>') == []

    def test_alt_api_root_candidates(self):
        parser = VideoParser()
        for envelope in (
            {'data': {'video_list': [{'id': 'a'}]}},
            {'itemList': [{'id': 'b'}]},
            {'aweme_list': [{'id': 'c'}]},
        ):
            assert len(parser.find_video_roots(envelope)) == 1


# ---------------------------------------------------------------------------
# VideoStreamScraper
# ---------------------------------------------------------------------------

def make_scraper(client, **kwargs):
    defaults = dict(min_interval=0, max_interval=0)  # no sleeping in tests
    defaults.update(kwargs)
    return VideoStreamScraper(client=client, **defaults)


class TestVideoStreamScraper:
    def _api_response(self):
        return load_fixture('video_api_response.json')

    def test_scrape_product_videos_happy_path(self):
        client = MockClient(lambda url: MockResponse(200, self._api_response()))
        scraper = make_scraper(client)
        videos = scraper.scrape_product_videos('7234567890123456789')
        assert len(videos) == 10
        assert all(set(v) == set(VIDEO_FIELDS) for v in videos)
        assert videos[0]['product_id'] == '7234567890123456789'
        # URL carried the signed query
        call = client.calls[0]
        assert 'product_id=7234567890123456789' in call['url']
        assert 'X-Bogus=' in call['url']
        assert call['headers']['X-Bogus'] == \
            call['url'].split('X-Bogus=')[-1]
        # UA passed in request headers matches signer UA (X-Bogus is UA-bound)
        assert call['headers']['User-Agent'] == scraper.signer.user_agent

    def test_limit_caps_rows(self):
        client = MockClient(lambda url: MockResponse(200, self._api_response()))
        scraper = make_scraper(client)
        videos = scraper.scrape_product_videos('7234567890123456789', limit=3)
        assert len(videos) == 3

    def test_signer_injection_into_scraper(self):
        """Custom signer propagates its UA + signature into the request."""
        signer = TikTokSigner(user_agent='CustomUA/1.0')
        client = MockClient(lambda url: MockResponse(200, self._api_response()))
        scraper = make_scraper(client, signer=signer)
        scraper.scrape_product_videos('7234567890123456789', limit=1)
        assert client.calls[0]['headers']['User-Agent'] == 'CustomUA/1.0'

    def test_404_and_bad_status(self):
        client = MockClient(lambda url: MockResponse(404, ''))
        assert make_scraper(client).scrape_product_videos('1') == []
        client = MockClient(lambda url: MockResponse(502, 'oops'))
        assert make_scraper(client).scrape_product_videos('1') == []

    def test_malformed_body(self):
        client = MockClient(lambda url: MockResponse(200, 'not-json{'))
        assert make_scraper(client).scrape_product_videos('1') == []
        client = MockClient(lambda url: MockResponse(200, '[]'))
        assert make_scraper(client).scrape_product_videos('1') == []

    def test_network_error_returns_empty(self):
        client = MockClient(lambda url: ConnectionError('boom'))
        assert make_scraper(client).scrape_product_videos('1') == []

    def test_daily_quota_gate(self, monkeypatch):
        monkeypatch.setattr(common_config, 'DAILY_QUOTA_VIDEOS', 2)
        client = MockClient(lambda url: MockResponse(200, self._api_response()))
        scraper = make_scraper(client)
        # First call: 1 gate + 2 rows == 3 > 2 -> partial after gate+1 row?
        # Contract: gate consumes 1, each row consumes 1; quota=2 -> gate ok
        # (count 1), row1 ok (count 2), row2 raises -> partial return of 1.
        videos = scraper.scrape_product_videos('1')
        assert len(videos) == 1
        # Next call: gate itself exceeds -> QuotaExceededException
        with pytest.raises(QuotaExceededException):
            scraper.scrape_product_videos('1')

    def test_soft_cooldown_on_429(self):
        client = MockClient(lambda url: RateLimitException('429'))
        scraper = make_scraper(client)
        with pytest.raises(RateLimitException):
            scraper.scrape_product_videos('1')
        assert scraper.in_cooldown

    def test_soft_cooldown_on_403(self):
        client = MockClient(lambda url: WAFBlockedException('403', 403))
        scraper = make_scraper(client)
        with pytest.raises(WAFBlockedException):
            scraper.scrape_product_videos('1')
        assert scraper.in_cooldown
        # subsequent call refused while cooling down
        with pytest.raises(RateLimitException):
            scraper.scrape_product_videos('1')

    def test_default_intervals_env_config(self, monkeypatch):
        monkeypatch.setenv('VIDEO_REQUEST_MIN_INTERVAL', '8')
        monkeypatch.setenv('VIDEO_REQUEST_MAX_INTERVAL', '12')
        scraper = VideoStreamScraper(client=MockClient())
        assert scraper.limiter.min_interval == 8
        assert scraper.limiter.max_interval == 12
        # inverted env values are normalized
        monkeypatch.setenv('VIDEO_REQUEST_MIN_INTERVAL', '12')
        monkeypatch.setenv('VIDEO_REQUEST_MAX_INTERVAL', '8')
        scraper = VideoStreamScraper(client=MockClient())
        assert scraper.limiter.min_interval == 8
        assert scraper.limiter.max_interval == 12
        # explicit kwargs override env
        scraper = VideoStreamScraper(client=MockClient(),
                                     min_interval=2, max_interval=3)
        assert scraper.limiter.min_interval == 2
        assert scraper.limiter.max_interval == 3

    def test_snapshot_engine_contract(self):
        engine = MagicMock()
        client = MockClient(lambda url: MockResponse(200, self._api_response()))
        scraper = make_scraper(client, snapshot_engine=engine)
        scraper.scrape_product_videos('7234567890123456789')
        assert engine.save_snapshot.call_count == 10
        first_args = engine.save_snapshot.call_args_list[0]
        assert first_args.args[1] == 'video_snapshots'
        assert 'video_id' in first_args.args[0]

    def test_parse_video_stats(self):
        scraper = make_scraper(MockClient())
        raw = json.loads(self._api_response())['data']['videos'][2]
        stats = scraper.parse_video_stats(raw)
        assert stats['video_id'] == '7300000000000000003'
        assert stats['views'] == 432
        assert set(stats) == set(VIDEO_FIELDS)

    def test_page_fallback_path(self):
        html = load_fixture('video_product_page.html')
        client = MockClient(lambda url: MockResponse(200, html))
        scraper = make_scraper(client)
        videos = scraper.scrape_product_videos_from_page('998877')
        assert len(videos) == 2
        assert videos[0]['product_id'] == '998877'
        assert 'product/998877' in client.calls[0]['url']
