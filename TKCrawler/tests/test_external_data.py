"""src/external_data 模块测试。

测试策略 (全部 mock, 不发起真实外网请求):
    - 1688 / Amazon 响应用 tests/fixtures/external_*.json 桩数据,
      monkeypatch requests.get / requests.post;
    - pytrends 用 fake TrendReq 对象替换 TrendsClient._build_trend;
    - 凭据缺失路径: 断言客户端进入"未配置"状态, 返回 None/空结果
      且不发任何网络请求;
    - enrich_product: 合并语义、不覆盖已有键、单源失败降级、入参不被修改。
"""

import json
import sys
from pathlib import Path

import pandas as pd
import pytest
import requests as requests_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.external_data.alibaba_client import AlibabaClient
from src.external_data.amazon_client import AmazonClient
from src.external_data.enricher import ExternalDataEnricher
from src.external_data.trends_client import TRENDS_UNAVAILABLE, TrendsClient

FIXTURES = Path(__file__).resolve().parent / 'fixtures'


def load_fixture(name: str) -> dict:
    with open(FIXTURES / name, encoding='utf-8') as f:
        return json.load(f)


class FakeResponse:
    """requests.Response 的最小替身。"""

    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests_lib.HTTPError(f'status={self.status_code}')

    def json(self):
        return self._payload


# ======================================================================
# AlibabaClient
# ======================================================================
class TestAlibabaClient:
    def test_not_configured_returns_none_without_request(self, monkeypatch):
        monkeypatch.delenv('EXTERNAL_ALIBABA_APP_KEY', raising=False)
        monkeypatch.delenv('EXTERNAL_ALIBABA_APP_SECRET', raising=False)
        called = []

        def fail_get(*args, **kwargs):
            called.append(args)
            raise AssertionError('无凭据时不应发起网络请求')

        monkeypatch.setattr(
            'src.external_data.alibaba_client.requests.get', fail_get)
        client = AlibabaClient()
        assert client.is_configured is False
        assert client.get_supplier_info('LED lights') is None
        assert called == []

    def test_get_supplier_info_success(self, monkeypatch):
        search_payload = load_fixture('external_1688_search.json')
        calls = []

        def fake_get(url, params=None, timeout=None):
            calls.append({'url': url, 'params': params})
            if 'system.oauth2' in url:
                return FakeResponse({'access_token': 'tok-123'})
            return FakeResponse(search_payload)

        monkeypatch.setattr(
            'src.external_data.alibaba_client.requests.get', fake_get)
        client = AlibabaClient(app_key='k1', app_secret='s1', max_retries=1)
        info = client.get_supplier_info('LED Strip Lights')

        assert info == {
            'supplier_name': 'Shenzhen Bright Lighting Co., Ltd.',
            'supplier_price': 3.20,
            'supplier_moq': 50,
            'supplier_lead_time_days': 7,
            'supplier_url': 'https://detail.1688.com/offer/123456789.html',
        }
        # 两次调用: token + 搜索, 均带签名参数
        assert len(calls) == 2
        assert all('_aop_signature' in c['params'] for c in calls)

    def test_request_failure_returns_none_after_retries(self, monkeypatch):
        attempts = []

        def fake_get(url, params=None, timeout=None):
            attempts.append(url)
            if 'system.oauth2' in url:
                return FakeResponse({'access_token': 'tok-123'})
            raise requests_lib.Timeout('gateway timeout')

        monkeypatch.setattr(
            'src.external_data.alibaba_client.requests.get', fake_get)
        client = AlibabaClient(
            app_key='k1', app_secret='s1', max_retries=2, retry_wait=0)
        info = client.get_supplier_info('LED lights')
        assert info is None
        # 搜索请求重试 max_retries 次
        assert len(attempts) == 1 + 2

    def test_search_no_result_returns_empty_dict(self, monkeypatch):
        def fake_get(url, params=None, timeout=None):
            if 'system.oauth2' in url:
                return FakeResponse({'access_token': 'tok-123'})
            return FakeResponse({'result': {'list': []}})

        monkeypatch.setattr(
            'src.external_data.alibaba_client.requests.get', fake_get)
        client = AlibabaClient(app_key='k1', app_secret='s1', max_retries=1)
        assert client.get_supplier_info('未知商品') == {}

    def test_signature_is_deterministic(self):
        client = AlibabaClient(app_key='k1', app_secret='s1')
        params = {'keywords': 'LED', 'pageNo': 1}
        assert client._sign('alibaba.linkplus.offer.search', params) == \
            client._sign('alibaba.linkplus.offer.search', params)


# ======================================================================
# TrendsClient
# ======================================================================
class FakeTrend:
    """pytrends TrendReq 替身。"""

    def __init__(self, df=None, error=None):
        self._df = df
        self._error = error
        self.payload_calls = []

    def build_payload(self, kw_list, timeframe=None, geo=None):
        self.payload_calls.append({'kw_list': kw_list, 'timeframe': timeframe})
        if self._error:
            raise self._error

    def interest_over_time(self):
        if self._error:
            raise self._error
        return self._df


def patch_trend(monkeypatch, trend):
    monkeypatch.setattr(
        TrendsClient, '_build_trend', lambda self: trend)


class TestTrendsClient:
    def test_score_normalization(self, monkeypatch):
        df = pd.DataFrame({'LED lights': [50.0, 100.0, 67.5]})
        patch_trend(monkeypatch, FakeTrend(df=df))
        score = TrendsClient(max_retries=1).get_trends_score('LED lights')
        assert score == pytest.approx(72.5 / 100)
        assert 0.0 <= score <= 1.0

    def test_score_clipped_to_one(self, monkeypatch):
        df = pd.DataFrame({'LED lights': [150.0, 200.0]})
        patch_trend(monkeypatch, FakeTrend(df=df))
        assert TrendsClient(max_retries=1).get_trends_score('LED lights') == 1.0

    def test_empty_data_returns_unavailable(self, monkeypatch):
        patch_trend(monkeypatch, FakeTrend(df=pd.DataFrame()))
        assert TrendsClient(max_retries=1).get_trends_score('xyz') == \
            TRENDS_UNAVAILABLE

    def test_exception_returns_unavailable(self, monkeypatch):
        patch_trend(monkeypatch, FakeTrend(error=RuntimeError('429 too many')))
        assert TrendsClient(max_retries=1).get_trends_score('LED') == \
            TRENDS_UNAVAILABLE

    def test_retries_before_giving_up(self, monkeypatch):
        trend = FakeTrend(error=requests_lib.ConnectionError('net down'))
        patch_trend(monkeypatch, trend)
        client = TrendsClient(max_retries=3, retry_wait=0)
        assert client.get_trends_score('LED') == TRENDS_UNAVAILABLE


# ======================================================================
# AmazonClient
# ======================================================================
class TestAmazonClient:
    def test_not_configured_returns_none_without_request(self, monkeypatch):
        for var in ('EXTERNAL_AMAZON_ACCESS_KEY', 'EXTERNAL_AMAZON_SECRET_KEY',
                    'EXTERNAL_AMAZON_PARTNER_TAG'):
            monkeypatch.delenv(var, raising=False)

        def fail_post(*args, **kwargs):
            raise AssertionError('无凭据时不应发起网络请求')

        monkeypatch.setattr(
            'src.external_data.amazon_client.requests.post', fail_post)
        client = AmazonClient()
        assert client.is_configured is False
        assert client.get_amazon_bsr('B08N5WRWNW') is None

    def test_get_bsr_success(self, monkeypatch):
        captured = {}

        def fake_post(url, data=None, headers=None, timeout=None):
            captured['url'] = url
            captured['body'] = json.loads(data)
            captured['headers'] = headers
            return FakeResponse(load_fixture('external_amazon_item.json'))

        monkeypatch.setattr(
            'src.external_data.amazon_client.requests.post', fake_post)
        client = AmazonClient(
            access_key='AK', secret_key='SK', partner_tag='tag-0', max_retries=1)
        bsr = client.get_amazon_bsr('B08N5WRWNW')

        assert bsr == {'rank': 142, 'category': 'Electronics', 'asin': 'B08N5WRWNW'}
        assert captured['url'].endswith('/paapi5/getitems')
        assert captured['body']['ItemIds'] == ['B08N5WRWNW']
        assert captured['body']['PartnerTag'] == 'tag-0'
        # SigV4 头
        assert 'AWS4-HMAC-SHA256' in captured['headers']['authorization']
        assert captured['headers']['x-amz-target'].endswith('GetItems')

    def test_api_error_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            'src.external_data.amazon_client.requests.post',
            lambda *a, **k: FakeResponse(
                {'Errors': [{'Code': 'InvalidParameterValue'}]}))
        client = AmazonClient(
            access_key='AK', secret_key='SK', partner_tag='tag-0', max_retries=1)
        assert client.get_amazon_bsr('BADASIN') is None

    def test_no_sales_rank_returns_empty_dict(self, monkeypatch):
        monkeypatch.setattr(
            'src.external_data.amazon_client.requests.post',
            lambda *a, **k: FakeResponse(load_fixture('external_amazon_item.json')))
        # 篡改解析路径: 无 BrowseNodeInfo
        client = AmazonClient(
            access_key='AK', secret_key='SK', partner_tag='tag-0', max_retries=1)
        client._request_with_retry = lambda payload, headers: {
            'ItemsResult': {'Items': [{'ASIN': 'B08N5WRWNW'}]}}
        assert client.get_amazon_bsr('B08N5WRWNW') == {}


# ======================================================================
# ExternalDataEnricher
# ======================================================================
def make_enricher(supplier=None, trends_score=TRENDS_UNAVAILABLE, bsr=None):
    """构造带打桩子客户端的富化器。"""
    alibaba = AlibabaClient(app_key='k', app_secret='s')
    alibaba.get_supplier_info = lambda title: supplier

    trends = TrendsClient(max_retries=1)
    trends.get_trends_score = lambda kw: trends_score

    amazon = AmazonClient(access_key='a', secret_key='b', partner_tag='c')
    amazon.get_amazon_bsr = lambda asin: bsr
    return ExternalDataEnricher(alibaba, trends, amazon)


class TestExternalDataEnricher:
    PRODUCT = {
        'product_id': 'p-001',
        'title': 'LED Strip Lights RGB',
        'keyword': 'led strip lights',
        'asin': 'B08N5WRWNW',
        'gmv_30d_est': 50000.0,
    }

    def test_enrich_merges_all_sources(self):
        enricher = make_enricher(
            supplier={
                'supplier_name': 'Shenzhen Bright Lighting Co., Ltd.',
                'supplier_price': 3.20,
                'supplier_moq': 50,
                'supplier_lead_time_days': 7,
                'supplier_url': 'https://detail.1688.com/offer/123.html',
            },
            trends_score=0.725,
            bsr={'rank': 142, 'category': 'Electronics', 'asin': 'B08N5WRWNW'},
        )
        result = enricher.enrich_product(self.PRODUCT)

        assert result['supplier_name'] == 'Shenzhen Bright Lighting Co., Ltd.'
        assert result['supplier_price'] == 3.20
        assert result['supplier_moq'] == 50
        assert result['supplier_lead_time_days'] == 7
        assert result['trends_score'] == pytest.approx(0.725)
        assert result['amazon_rank'] == 142
        assert result['amazon_category'] == 'Electronics'
        assert result['amazon_asin'] == 'B08N5WRWNW'
        # 原有键保留
        assert result['product_id'] == 'p-001'
        assert result['gmv_30d_est'] == 50000.0

    def test_enrich_does_not_overwrite_existing_keys(self):
        enricher = make_enricher(
            supplier={'supplier_price': 3.20, 'supplier_moq': 50},
            trends_score=0.5,
            bsr={'rank': 142, 'category': 'Electronics', 'asin': 'X'},
        )
        product = {**self.PRODUCT, 'supplier_price': 9.99, 'trends_score': 0.1}
        result = enricher.enrich_product(product)

        assert result['supplier_price'] == 9.99   # 原值保留
        assert result['supplier_moq'] == 50        # 新值加入
        assert result['trends_score'] == 0.1
        assert 'amazon_rank' in result

    def test_enrich_degrades_when_all_sources_fail(self):
        enricher = make_enricher(
            supplier=None,               # 1688 未配置
            trends_score=TRENDS_UNAVAILABLE,  # Trends 失败
            bsr=None,                    # Amazon 未配置
        )
        result = enricher.enrich_product(self.PRODUCT)
        assert result == self.PRODUCT    # 仅返回原数据, 不抛异常

    def test_enrich_partial_success(self):
        enricher = make_enricher(
            supplier={'supplier_price': 3.20, 'supplier_moq': 50},
            trends_score=TRENDS_UNAVAILABLE,   # Trends 挂了
            bsr=None,                          # Amazon 未配置
        )
        result = enricher.enrich_product(self.PRODUCT)
        assert result['supplier_price'] == 3.20
        assert 'trends_score' not in result
        assert 'amazon_rank' not in result

    def test_enrich_does_not_mutate_input(self):
        enricher = make_enricher(
            supplier={'supplier_price': 3.20}, trends_score=0.5,
            bsr={'rank': 142, 'category': 'Electronics', 'asin': 'X'})
        original = dict(self.PRODUCT)
        enricher.enrich_product(self.PRODUCT)
        assert self.PRODUCT == original

    def test_enrich_without_asin_skips_amazon(self):
        enricher = make_enricher(
            supplier={}, trends_score=0.5, bsr={'rank': 1, 'category': 'X', 'asin': 'Y'})
        product = {'title': 'LED', 'keyword': 'led'}
        result = enricher.enrich_product(product)
        assert result['trends_score'] == 0.5
        assert 'amazon_rank' not in result

    def test_enrich_without_title_skips_supplier(self):
        enricher = make_enricher(
            supplier={'supplier_price': 3.20}, trends_score=TRENDS_UNAVAILABLE,
            bsr=None)
        result = enricher.enrich_product({'product_id': 'p-2'})
        assert 'supplier_price' not in result
        assert result == {'product_id': 'p-2'}
