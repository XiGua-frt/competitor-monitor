"""Scraper 层单测 — mock client 注入，无真实网络。

覆盖：
- PdpScraper.scrape_product 产物字段完整（可直接喂 save_snapshot）
- 503 响应触发软冷却，冷却期内第二次调用不发请求直接抛 BotBlockedException
- SearchScraper / OfferScraper 的 URL 构造与产物结构
"""

from decimal import Decimal
from pathlib import Path

import pytest

from src.scrapers import (
    BaseScraper,
    BotBlockedException,
    OfferScraper,
    PdpScraper,
    SearchScraper,
)

FIXTURES = Path(__file__).parent / 'fixtures' / 'amazon'


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding='utf-8')


class MockResponse:
    def __init__(self, status_code=200, text=''):
        self.status_code = status_code
        self.text = text


class MockClient:
    """带 request(url, method, headers, **kw) 的 mock client。"""

    def __init__(self, responses):
        # responses: list of MockResponse，按调用顺序弹出
        self._responses = list(responses)
        self.calls = []

    def request(self, url, method='GET', headers=None, **kw):
        self.calls.append({'url': url, 'method': method, 'headers': headers})
        if not self._responses:
            raise AssertionError('unexpected extra request')
        return self._responses.pop(0)


@pytest.fixture
def fast_scraper_kwargs():
    # 测试里把限速区间压到 0，避免真实 sleep
    return {'min_interval': 0, 'max_interval': 0, 'cooldown_seconds': 3600}


class TestPdpScraper:
    def test_scrape_product_full_pipeline(self, fast_scraper_kwargs):
        html = load('B0DLQ8MVF7.html')
        client = MockClient([MockResponse(200, html)])
        scraper = PdpScraper(client=client, **fast_scraper_kwargs)
        data = scraper.scrape_product('B0DLQ8MVF7')

        # snapshot 字段完整性（asin_snapshots 列口径）
        assert data['asin'] == 'B0DLQ8MVF7'
        assert data['marketplace'] == 'US'
        assert data['snapshot_date'] is not None
        assert data['title'].startswith('Custom Best Friend')
        assert data['price_current'] == Decimal('14.99')
        assert data['rating'] == Decimal('4.6')
        assert data['rating_count'] == 104
        assert data['bsr_ranks'][0]['rank'] == 729415
        assert data['date_first_available'] == '2024-11-01'
        assert data['variant_asin'] == 'B0752XJYNL'
        assert data['variant_status'] == 'fixed'
        assert data['price_scope'] == 'display_page_price'
        assert data['currency'] == 'USD'
        assert data['comparison_ready'] is True
        assert data['region_verified'] is True

        # URL 正确
        assert client.calls[0]['url'] == 'https://www.amazon.com/dp/B0DLQ8MVF7'

    def test_503_triggers_cooldown_blocks_next_call(self,
                                                    fast_scraper_kwargs):
        client = MockClient([
            MockResponse(503, '<html><body>Service Unavailable</body></html>'),
        ])
        scraper = PdpScraper(client=client, **fast_scraper_kwargs)

        with pytest.raises(BotBlockedException):
            scraper.scrape_product('B0DLQ8MVF7')

        # 已进入软冷却
        assert scraper.in_cooldown is True
        assert scraper.get_cooldown_remaining() > 0

        # 第二次调用：不发请求，直接抛 BotBlockedException
        with pytest.raises(BotBlockedException, match='Soft cooldown'):
            scraper.scrape_product('B0DLQ8MVF7')
        assert len(client.calls) == 1  # 只有一次真实请求

    def test_bot_challenge_body_triggers_cooldown(self,
                                                  fast_scraper_kwargs):
        captcha_html = ('<html><head><title>Robot Check</title></head>'
                        '<body>Enter the characters you see below'
                        '<img src="captcha-image"></body></html>')
        client = MockClient([MockResponse(200, captcha_html)])
        scraper = PdpScraper(client=client, **fast_scraper_kwargs)
        with pytest.raises(BotBlockedException):
            scraper.scrape_product('B0DLQ8MVF7')
        assert scraper.in_cooldown is True

    def test_normal_page_containing_captcha_word_not_blocked(
            self, fast_scraper_kwargs):
        # "captcha" 出现在正常页资源名里不误报（四强特征设计）
        html = load('B0DLQ8MVF7.html') + '<img src="/captcha/asset.png">'
        client = MockClient([MockResponse(200, html)])
        scraper = PdpScraper(client=client, **fast_scraper_kwargs)
        data = scraper.scrape_product('B0DLQ8MVF7')
        assert data['title'].startswith('Custom Best Friend')
        assert scraper.in_cooldown is False

    def test_marketplace_url(self, fast_scraper_kwargs):
        client = MockClient([MockResponse(200, load('B0GWVTMR4Q.html'))])
        scraper = PdpScraper(client=client, **fast_scraper_kwargs)
        scraper.scrape_product('B0GWVTMR4Q', marketplace='UK')
        assert client.calls[0]['url'] == \
            'https://www.amazon.co.uk/dp/B0GWVTMR4Q'
        # marketplace 写回快照
        # (重取返回值)
        client2 = MockClient([MockResponse(200, load('B0GWVTMR4Q.html'))])
        scraper2 = PdpScraper(client=client2, **fast_scraper_kwargs)
        data = scraper2.scrape_product('B0GWVTMR4Q', marketplace='UK')
        assert data['marketplace'] == 'UK'


class TestSearchScraper:
    def test_search_pipeline(self, fast_scraper_kwargs):
        client = MockClient([
            MockResponse(200, load('search_sample.html')),
        ])
        scraper = SearchScraper(client=client, **fast_scraper_kwargs)
        items = scraper.search('custom dog shirt', page=2)

        assert len(items) == 3
        assert items[0]['asin'] == 'B0AAAAAAAA'
        assert items[0]['natural_rank'] == 1
        assert items[0]['keyword'] == 'custom dog shirt'
        assert items[0]['page'] == 2
        assert items[0]['snapshot_date'] is not None
        # ad_rank 缺省补 None（keyword_rank_snapshots 口径）
        assert items[0]['ad_rank'] is None
        ad = items[1]
        assert ad['ad_rank'] == 1
        assert ad['natural_rank'] is None

        # URL 构造
        assert client.calls[0]['url'] == (
            'https://www.amazon.com/s?k=custom+dog+shirt&page=2')

    def test_search_503_cooldown(self, fast_scraper_kwargs):
        client = MockClient([MockResponse(503, 'Service Unavailable')])
        scraper = SearchScraper(client=client, **fast_scraper_kwargs)
        with pytest.raises(BotBlockedException):
            scraper.search('shirts')
        with pytest.raises(BotBlockedException):
            scraper.search('shirts')
        assert len(client.calls) == 1


class TestOfferScraper:
    def test_scrape_offers_pipeline(self, fast_scraper_kwargs):
        client = MockClient([MockResponse(200, load('offer_sample.html'))])
        scraper = OfferScraper(client=client, **fast_scraper_kwargs)
        offers = scraper.scrape_offers('B0DLQ8MVF7')

        assert len(offers) == 3
        assert offers[0]['asin'] == 'B0DLQ8MVF7'
        assert offers[0]['is_buybox'] is True
        assert offers[0]['seller_id'] == 'A21T9VMSLBQ6TP'
        assert offers[0]['price'] == Decimal('14.99')
        assert offers[0]['snapshot_date'] is not None
        assert client.calls[0]['url'] == (
            'https://www.amazon.com/gp/offer-listing/B0DLQ8MVF7'
            '?condition=new&fulfillment=all')


class TestBaseScraper:
    def test_cooldown_expiry_manual_reset(self, fast_scraper_kwargs):
        client = MockClient([MockResponse(200, 'ok')])
        scraper = BaseScraper(client=client, **fast_scraper_kwargs)
        scraper.enter_cooldown('test')
        assert scraper.in_cooldown is True
        scraper.reset_cooldown()
        assert scraper.in_cooldown is False
        # reset 后可以正常请求
        resp = scraper.fetch('https://www.amazon.com/x')
        assert resp.status_code == 200

    def test_fetch_success(self, fast_scraper_kwargs):
        client = MockClient([MockResponse(200, 'hello')])
        scraper = BaseScraper(client=client, **fast_scraper_kwargs)
        resp = scraper.fetch('https://www.amazon.com/x',
                             headers={'X-Test': '1'})
        assert resp.status_code == 200
        assert client.calls[0]['headers'] == {'X-Test': '1'}
