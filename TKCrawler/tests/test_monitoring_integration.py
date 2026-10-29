"""Integration tests: BaseScraper ↔ src/monitoring metrics (Agent M).

离线测试：mock client 驱动 BaseScraper 子类发起请求，验证
scraper_requests_total / scraper_request_duration_seconds /
scraper_waf_blocks_total / data_quality_missing_fields_ratio 四条指标在
success / WAF / 限流 / 异常四条路径上正确变化，以及指标层不可用时
（prometheus_client 缺失、记录函数抛错、metrics server 未启动）主流程
零影响（零开销降级）。

不真实请求 TikTok。
"""

import itertools
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from prometheus_client import REGISTRY

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import src.scrapers.base_scraper as base_scraper_module  # noqa: E402
from src.common.exceptions import (  # noqa: E402
    RateLimitException,
    WAFBlockedException,
)
from src.monitoring import metrics  # noqa: E402
from src.scrapers.base_scraper import BaseScraper  # noqa: E402

_uid_counter = itertools.count()


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
        self.calls.append({'url': url, 'method': method, 'headers': headers})
        result = self.responses(url)
        if isinstance(result, Exception):
            raise result
        return result


def make_scraper(name, responses=None, **kwargs):
    """动态创建名为 ``name`` 的 BaseScraper 子类实例。

    类名小写即指标 scraper 标签；每个用例用唯一名字隔离全局
    REGISTRY 中的计数器状态。
    """
    cls = type(name, (BaseScraper,), {})
    kwargs.setdefault('min_interval', 0)
    kwargs.setdefault('max_interval', 0)
    return cls(client=MockClient(responses), **kwargs)


def get_count(scraper_name, status):
    return metrics.get_request_count(scraper_name, status)


def get_duration_count(scraper_name):
    value = REGISTRY.get_sample_value(
        metrics.DURATION_SECONDS + '_count', {'scraper': scraper_name}
    )
    return value or 0.0


def get_duration_sum(scraper_name):
    value = REGISTRY.get_sample_value(
        metrics.DURATION_SECONDS + '_sum', {'scraper': scraper_name}
    )
    return value or 0.0


# ===========================================================================
# 1. success 路径
# ===========================================================================

class TestSuccessPath:
    def test_success_increments_counter_and_histogram(self):
        name = f'successscraper{next(_uid_counter)}'
        scraper = make_scraper(name)

        resp = scraper.fetch('https://x/1')
        assert resp.status_code == 200

        assert get_count(name, 'success') == 1.0
        assert get_count(name, 'waf') == 0.0
        assert get_count(name, 'rate_limited') == 0.0
        assert get_count(name, 'error') == 0.0
        assert get_duration_count(name) == 1.0
        assert get_duration_sum(name) >= 0.0

    def test_success_repeated(self):
        name = f'successscraper{next(_uid_counter)}'
        scraper = make_scraper(name)
        for i in range(3):
            scraper.fetch(f'https://x/{i}')

        assert get_count(name, 'success') == 3.0
        assert get_duration_count(name) == 3.0

    def test_scraper_label_is_lowercased_class_name(self):
        name = f'LabelCheck{next(_uid_counter)}'
        scraper = make_scraper(name)
        scraper.fetch('https://x/1')
        # 标签必须等于类名小写，而不是 'basescraper' 等固定值
        assert get_count(name.lower(), 'success') == 1.0

    def test_no_waf_block_on_success(self):
        name = f'successscraper{next(_uid_counter)}'
        scraper = make_scraper(name)
        scraper.fetch('https://x/1')
        assert metrics.get_waf_block_count(name) == 0.0


# ===========================================================================
# 2. WAF 路径
# ===========================================================================

class TestWafPath:
    def test_waf_increments_request_and_block_counters(self):
        name = f'wafscraper{next(_uid_counter)}'
        scraper = make_scraper(
            name, responses=lambda url: WAFBlockedException(url, 403),
            cooldown_seconds=0.05,
        )

        with pytest.raises(WAFBlockedException):
            scraper.fetch('https://x/1')

        assert get_count(name, 'waf') == 1.0
        assert get_count(name, 'success') == 0.0
        assert metrics.get_waf_block_count(name) == 1.0
        assert get_duration_count(name) == 1.0

    def test_waf_repeated_blocks(self):
        name = f'wafscraper{next(_uid_counter)}'
        scraper = make_scraper(
            name, responses=lambda url: WAFBlockedException(url, 403),
            cooldown_seconds=0.05,
        )

        for _ in range(2):
            with pytest.raises(WAFBlockedException):
                scraper.fetch('https://x/1')
            import time
            time.sleep(0.06)  # 让冷却过期，制造第二次真实请求

        assert get_count(name, 'waf') == 2.0
        assert metrics.get_waf_block_count(name) == 2.0

    def test_waf_still_enters_cooldown(self):
        name = f'wafscraper{next(_uid_counter)}'
        scraper = make_scraper(
            name, responses=lambda url: WAFBlockedException(url, 403),
        )
        with pytest.raises(WAFBlockedException):
            scraper.fetch('https://x/1')
        assert scraper.in_cooldown is True


# ===========================================================================
# 3. 限流路径
# ===========================================================================

class TestRateLimitedPath:
    def test_429_recorded_as_rate_limited(self):
        name = f'ratelimitedscraper{next(_uid_counter)}'
        scraper = make_scraper(
            name, responses=lambda url: RateLimitException('429'),
        )

        with pytest.raises(RateLimitException):
            scraper.fetch('https://x/1')

        assert get_count(name, 'rate_limited') == 1.0
        assert get_count(name, 'waf') == 0.0
        assert get_count(name, 'success') == 0.0
        assert get_duration_count(name) == 1.0

    def test_429_still_enters_cooldown(self):
        name = f'ratelimitedscraper{next(_uid_counter)}'
        scraper = make_scraper(
            name, responses=lambda url: RateLimitException('429'),
        )
        with pytest.raises(RateLimitException):
            scraper.fetch('https://x/1')
        assert scraper.in_cooldown is True

    def test_cooldown_refusal_not_counted_as_request(self):
        """冷却期拒绝发生在真实请求之前，不计入请求指标。"""
        name = f'ratelimitedscraper{next(_uid_counter)}'
        scraper = make_scraper(
            name, responses=lambda url: RateLimitException('429'),
        )
        with pytest.raises(RateLimitException):
            scraper.fetch('https://x/1')

        before = get_count(name, 'rate_limited')
        client_calls = len(scraper.client.calls)
        with pytest.raises(RateLimitException, match='Soft cooldown'):
            scraper.fetch('https://x/2')

        assert len(scraper.client.calls) == client_calls
        assert get_count(name, 'rate_limited') == before


# ===========================================================================
# 4. 其他异常路径
# ===========================================================================

class TestErrorPath:
    def test_network_error_recorded_as_error(self):
        name = f'errorscraper{next(_uid_counter)}'
        scraper = make_scraper(name)
        scraper.client.request = MagicMock(side_effect=ConnectionError('boom'))

        with pytest.raises(ConnectionError):
            scraper.fetch('https://x/1')

        assert get_count(name, 'error') == 1.0
        assert get_count(name, 'success') == 0.0
        assert get_count(name, 'waf') == 0.0
        assert get_duration_count(name) == 1.0

    def test_network_error_does_not_enter_cooldown(self):
        name = f'errorscraper{next(_uid_counter)}'
        scraper = make_scraper(name)
        scraper.client.request = MagicMock(side_effect=ConnectionError('boom'))
        with pytest.raises(ConnectionError):
            scraper.fetch('https://x/1')
        assert scraper.in_cooldown is False

    def test_scrape_level_error_isolated_from_success(self):
        name = f'errorscraper{next(_uid_counter)}'
        scraper = make_scraper(name)

        def flaky(url):
            if url.endswith('bad'):
                raise ConnectionError('boom')
            return MockResponse(200, '')

        scraper.client = MockClient(flaky)
        scraper.fetch('https://x/good')
        with pytest.raises(ConnectionError):
            scraper.fetch('https://x/bad')

        assert get_count(name, 'success') == 1.0
        assert get_count(name, 'error') == 1.0


# ===========================================================================
# 5. 零开销降级
# ===========================================================================

class TestGracefulDegradation:
    def test_recorder_raising_does_not_break_fetch(self, monkeypatch):
        """埋点函数抛错时，fetch 行为完全不受影响。"""
        name = f'degradedscraper{next(_uid_counter)}'
        scraper = make_scraper(name)

        def broken_observe(*args, **kwargs):
            raise RuntimeError('metrics backend exploded')

        monkeypatch.setattr(base_scraper_module, 'observe_request',
                            broken_observe)
        resp = scraper.fetch('https://x/1')
        assert resp.status_code == 200

    def test_recorder_raising_does_not_mask_exceptions(self, monkeypatch):
        """埋点抛错不会吞掉/替换业务异常。"""
        name = f'degradedscraper{next(_uid_counter)}'
        scraper = make_scraper(
            name, responses=lambda url: WAFBlockedException(url, 403),
        )

        def broken_observe(*args, **kwargs):
            raise RuntimeError('metrics backend exploded')

        monkeypatch.setattr(base_scraper_module, 'observe_request',
                            broken_observe)
        monkeypatch.setattr(base_scraper_module, 'inc_waf_block',
                            broken_observe)
        with pytest.raises(WAFBlockedException):
            scraper.fetch('https://x/1')
        assert scraper.in_cooldown is True  # 冷却逻辑照常生效

    def test_metrics_disabled_noop(self, monkeypatch):
        """模拟 prometheus_client 缺失（记录函数为 None）：no-op 不抛错。"""
        name = f'disabledscraper{next(_uid_counter)}'
        scraper = make_scraper(name)

        monkeypatch.setattr(base_scraper_module, 'observe_request', None)
        monkeypatch.setattr(base_scraper_module, 'inc_waf_block', None)
        monkeypatch.setattr(base_scraper_module, 'set_missing_ratio', None)

        resp = scraper.fetch('https://x/1')
        assert resp.status_code == 200
        assert scraper.record_data_quality('t', {'a': None, 'b': 1}) == 0.5

    def test_metrics_server_not_started_is_fine(self):
        """未调用 start_metrics_server 时，埋点照常累加且不抛错。"""
        name = f'serverscraper{next(_uid_counter)}'
        scraper = make_scraper(name)
        scraper.fetch('https://x/1')
        assert get_count(name, 'success') == 1.0


# ===========================================================================
# 6. 数据质量助手 record_data_quality
# ===========================================================================

class TestDataQualityHelper:
    def test_ratio_computed_and_reported(self):
        name = f'dqscraper{next(_uid_counter)}'
        scraper = make_scraper(name)
        table = f'product_snapshots_{next(_uid_counter)}'

        ratio = scraper.record_data_quality(
            table, {'a': 1, 'b': None, 'c': 2, 'd': None})
        assert ratio == pytest.approx(0.5)
        assert metrics.get_missing_ratio(table) == pytest.approx(0.5)

    def test_no_missing_fields(self):
        name = f'dqscraper{next(_uid_counter)}'
        scraper = make_scraper(name)
        table = f'video_snapshots_{next(_uid_counter)}'

        ratio = scraper.record_data_quality(table, {'a': 1, 'b': 'x'})
        assert ratio == 0.0
        assert metrics.get_missing_ratio(table) == 0.0

    def test_all_missing_fields(self):
        name = f'dqscraper{next(_uid_counter)}'
        scraper = make_scraper(name)
        table = f'video_snapshots_{next(_uid_counter)}'

        ratio = scraper.record_data_quality(table, {'a': None, 'b': None})
        assert ratio == 1.0
        assert metrics.get_missing_ratio(table) == 1.0

    def test_non_dict_returns_zero_and_does_not_report(self):
        name = f'dqscraper{next(_uid_counter)}'
        scraper = make_scraper(name)
        table = f'creator_snapshots_{next(_uid_counter)}'

        assert scraper.record_data_quality(table, None) == 0.0
        assert scraper.record_data_quality(table, [1, 2]) == 0.0
        assert scraper.record_data_quality(table, {}) == 0.0
        assert metrics.get_missing_ratio(table) == 0.0

    def test_reporting_failure_does_not_raise(self, monkeypatch):
        name = f'dqscraper{next(_uid_counter)}'
        scraper = make_scraper(name)

        def broken_set(*args, **kwargs):
            raise RuntimeError('boom')

        monkeypatch.setattr(base_scraper_module, 'set_missing_ratio',
                            broken_set)
        # 不抛错，仍返回计算出的比率
        assert scraper.record_data_quality('t', {'a': None}) == 1.0


# ===========================================================================
# 7. 端到端：经 scrape_product 全链路（复用 Shop scraper，不改其代码）
# ===========================================================================

class _StubSessionManager:
    """隔离本地 data/session.json 新鲜度的最小 session 桩。"""

    def get_cookies(self):
        return {'sessionid_ss': 'stub', 'ttwid': 'stub'}


class TestEndToEndViaShopScraper:
    def test_shop_scrape_success_records_metrics(self):
        from src.scrapers import ShopProductScraper

        FIXTURES = Path(__file__).resolve().parent.parent / 'tests' / 'fixtures'
        html = (FIXTURES / 'product_page_sample.html').read_text(
            encoding='utf-8')

        scraper = ShopProductScraper(
            client=MockClient(lambda url: MockResponse(200, html)),
            session_manager=_StubSessionManager(),
            min_interval=0, max_interval=0,
        )
        result = scraper.scrape_product('7301234567890123456')
        assert result is not None

        name = 'shopproductscraper'
        # ShopProductScraper 的埋点落在 BaseScraper.fetch 一层
        assert get_count(name, 'success') >= 1.0
        assert get_duration_count(name) >= 1.0

    def test_shop_scrape_waf_records_metrics(self):
        from src.scrapers import ShopProductScraper

        scraper = ShopProductScraper(
            client=MockClient(lambda url: WAFBlockedException(url, 403)),
            session_manager=_StubSessionManager(),
            min_interval=0, max_interval=0, cooldown_seconds=0.05,
        )
        with pytest.raises(WAFBlockedException):
            scraper.scrape_product('123')

        name = 'shopproductscraper'
        assert get_count(name, 'waf') >= 1.0
        assert metrics.get_waf_block_count(name) >= 1.0
