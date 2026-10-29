"""Unit tests for src/pools/ip_pool.py IPPoolManager.

Mock 策略：
- Redis 使用 fakeredis.FakeRedis(decode_responses=True)，不连真实 Redis
- 健康检查：_health_check_proxy 在函数内部 `from ..anti_detection.curl_client
  import CurlCffiClient`（局部导入），因此必须 patch
  `src.anti_detection.curl_client.CurlCffiClient`（patch src.pools.ip_pool 名字空间无效）。
  mock 的 .get 返回 status_code=200/503，其余逻辑（latency 统计、计数、状态流转）走真实代码。
- 日志为 loguru，pytest caplog 无法捕获，使用 loguru sink fixture。
"""

import json
from datetime import datetime
from unittest.mock import Mock, patch

import pytest
import redis
from fakeredis import FakeRedis
from loguru import logger as loguru_logger

from src.pools.ip_pool import IPPoolManager
from src.pools.models import ProxyNode, ProxyStatus
from src.pools.exceptions import (
    InvalidProxyException,
    NoAvailableProxyException,
    ProxyFailedException,
)

VALID_PROXY = 'http://user:pass@10.0.0.1:8080'


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_redis():
    """每个测试独立的 fakeredis 实例。"""
    return FakeRedis(decode_responses=True)


@pytest.fixture
def mock_curl():
    """patch CurlCffiClient（局部导入，必须 patch 其定义模块）。

    默认 .get 返回 status_code=200（健康检查通过）；测试内可改写
    mock_curl.return_value.get.return_value.status_code 模拟失败。
    """
    with patch('src.anti_detection.curl_client.CurlCffiClient') as mock_cls:
        mock_cls.return_value.get.return_value = Mock(status_code=200)
        yield mock_cls


@pytest.fixture
def manager(fake_redis, mock_curl):
    """带 mock 健康检查（默认通过）的 IPPoolManager。"""
    return IPPoolManager(redis_client=fake_redis)


@pytest.fixture
def log_capture():
    """loguru 日志捕获（caplog 对 loguru 无效）。"""
    records = []
    handler_id = loguru_logger.add(records.append, level="DEBUG")
    yield records
    loguru_logger.remove(handler_id)


def _log_text(records) -> str:
    return ''.join(str(r) for r in records)


def _add_proxy(manager) -> str:
    return manager.add_proxy(VALID_PROXY, region='US')


# ---------------------------------------------------------------------------
# 1. add_proxy 成功路径
# ---------------------------------------------------------------------------

class TestAddProxySuccess:
    def test_add_proxy_returns_active_id(self, manager):
        proxy_id = _add_proxy(manager)

        assert proxy_id.startswith('proxy_')
        assert len(proxy_id) == len('proxy_') + 8

        node = manager.proxies[proxy_id]
        assert node.status == ProxyStatus.ACTIVE.value
        assert node.region == 'US'
        assert node.proxy_type == 'http'

    def test_add_proxy_get_proxy_roundtrip(self, manager):
        proxy_id = _add_proxy(manager)

        node = manager.get_proxy()  # 无账号，任取一个可用代理
        assert node.proxy_id == proxy_id
        assert node.proxy_url == VALID_PROXY
        assert node.is_available()

    def test_add_proxy_health_check_uses_proxy_and_tiktok(self, manager, mock_curl):
        _add_proxy(manager)

        client = mock_curl.return_value
        client.get.assert_called_once()
        args, kwargs = client.get.call_args
        assert args[0] == 'https://www.tiktok.com'
        # 代理确实被挂到 client session 上
        assert client.session.proxies == {'http': VALID_PROXY, 'https': VALID_PROXY}

    def test_add_proxy_records_check_metrics(self, manager):
        proxy_id = _add_proxy(manager)

        node = manager.proxies[proxy_id]
        assert node.total_checks == 1
        assert node.success_checks == 1
        assert node.success_rate == 1.0
        assert node.avg_latency > 0
        assert isinstance(node.last_check_time, datetime)

    def test_add_proxy_in_redis_active_set(self, manager, fake_redis):
        proxy_id = _add_proxy(manager)

        assert fake_redis.sismember('proxies:active', proxy_id)
        stored = fake_redis.hgetall(f'proxy:{proxy_id}')
        assert stored['proxy_url'] == VALID_PROXY
        assert stored['status'] == 'active'


# ---------------------------------------------------------------------------
# 2. add_proxy 健康检查失败
# ---------------------------------------------------------------------------

class TestAddProxyHealthCheckFailed:
    def test_health_check_failure_raises(self, manager, mock_curl):
        mock_curl.return_value.get.return_value = Mock(status_code=503)

        with pytest.raises(ProxyFailedException):
            manager.add_proxy(VALID_PROXY)

    def test_failed_proxy_not_persisted(self, manager, fake_redis, mock_curl):
        mock_curl.return_value.get.return_value = Mock(status_code=500)

        with pytest.raises(ProxyFailedException):
            manager.add_proxy(VALID_PROXY)

        # 池中不留任何记录
        assert fake_redis.smembers('proxies:active') == set()
        assert fake_redis.keys('proxy:*') == []
        assert manager.proxies == {}

    def test_health_check_exception_counts_as_failure(self, manager, mock_curl):
        # 请求抛异常也应视为检查失败
        mock_curl.return_value.get.side_effect = ConnectionError('proxy unreachable')

        with pytest.raises(ProxyFailedException):
            manager.add_proxy(VALID_PROXY)
        assert manager.proxies == {}


# ---------------------------------------------------------------------------
# 3. add_proxy 非法 URL
# ---------------------------------------------------------------------------

class TestValidateProxyUrl:
    @pytest.mark.parametrize('bad_url', [
        '',                    # 空串
        '1.2.3.4:8080',        # 缺 scheme
        'ftp://1.2.3.4:8080',  # 不支持的 scheme
        'http://',             # 缺 host
    ])
    def test_invalid_url_rejected(self, manager, mock_curl, bad_url):
        with pytest.raises(InvalidProxyException):
            manager.add_proxy(bad_url)
        # 校验发生在网络检查之前，不应触碰 CurlCffiClient
        mock_curl.assert_not_called()

    @pytest.mark.parametrize('good_url', [
        'http://1.2.3.4:8080',
        'http://user:pass@proxy.example.com:8080',
        'socks5://1.2.3.4:1080',
        'https://proxy.example.com',
    ])
    def test_valid_url_passes_validation(self, manager, good_url):
        # 只测 _validate_proxy_url 本身，不做网络检查
        assert manager._validate_proxy_url(good_url) is True

    def test_invalid_url_not_saved(self, manager, fake_redis):
        with pytest.raises(InvalidProxyException):
            manager.add_proxy('1.2.3.4:8080')
        assert fake_redis.smembers('proxies:active') == set()


# ---------------------------------------------------------------------------
# 4. remove_proxy
# ---------------------------------------------------------------------------

class TestRemoveProxy:
    def test_remove_proxy_success(self, manager, fake_redis):
        proxy_id = _add_proxy(manager)

        assert manager.remove_proxy(proxy_id) is True
        assert proxy_id not in manager.proxies
        assert not fake_redis.sismember('proxies:active', proxy_id)
        assert fake_redis.hgetall(f'proxy:{proxy_id}') == {}

    def test_remove_proxy_then_get_proxy_raises(self, manager):
        proxy_id = _add_proxy(manager)
        manager.remove_proxy(proxy_id)

        with pytest.raises(NoAvailableProxyException):
            manager.get_proxy()

    def test_remove_nonexistent_proxy_returns_false(self, manager):
        assert manager.remove_proxy('proxy_deadbeef') is False


# ---------------------------------------------------------------------------
# 5. get_proxy 过滤不健康/不可用代理
# ---------------------------------------------------------------------------

class TestGetProxyFiltering:
    def _inject_failed_active_proxy(self, manager, fake_redis):
        """构造一个 status=failed 但仍挂在 proxies:active 集合里的代理。"""
        node = ProxyNode(
            proxy_id='proxy_failed01',
            proxy_url='http://1.2.3.4:9999',
            status=ProxyStatus.FAILED.value,
            # 注意：不设 last_check_time 会触发 _save_proxy 的 DataError
            # （ProxyNode.to_dict 未处理 None，见报告 bug#1）
            last_check_time=datetime.now(),
        )
        manager._save_proxy(node)
        fake_redis.sadd('proxies:active', node.proxy_id)
        return node

    def test_get_proxy_skips_failed_status(self, manager, fake_redis):
        self._inject_failed_active_proxy(manager, fake_redis)

        # active 集合非空，但唯一成员不可用
        assert fake_redis.smembers('proxies:active')
        with pytest.raises(NoAvailableProxyException):
            manager.get_proxy()

    def test_get_proxy_skips_fully_loaded_proxy(self, manager, fake_redis):
        proxy_id = _add_proxy(manager)
        node = manager.proxies[proxy_id]
        node.max_accounts = 2
        manager._save_proxy(node)

        manager.bind_proxy_to_account(proxy_id, 'acc_a')
        manager.bind_proxy_to_account(proxy_id, 'acc_b')
        assert not node.is_available()  # 绑满

        with pytest.raises(NoAvailableProxyException):
            manager.get_proxy()

    def test_get_proxy_returns_only_available(self, manager, fake_redis):
        self._inject_failed_active_proxy(manager, fake_redis)
        healthy_id = _add_proxy(manager)

        node = manager.get_proxy()
        assert node.proxy_id == healthy_id
        assert node.status == ProxyStatus.ACTIVE.value

    def test_get_proxy_with_account_auto_assigns_and_binds(self, manager):
        proxy_id = _add_proxy(manager)

        node = manager.get_proxy(account_id='acc_auto')
        assert node.proxy_id == proxy_id
        assert manager.get_bound_proxy_id('acc_auto') == proxy_id

        # 再次获取走绑定复用，仍是同一代理
        assert manager.get_proxy(account_id='acc_auto').proxy_id == proxy_id


# ---------------------------------------------------------------------------
# 6. 绑定/解绑
# ---------------------------------------------------------------------------

class TestBinding:
    def test_bind_unbind_roundtrip(self, manager):
        proxy_id = _add_proxy(manager)

        assert manager.bind_proxy_to_account(proxy_id, 'acc_1') is True
        assert manager.get_bound_proxy_id('acc_1') == proxy_id
        assert 'acc_1' in manager.proxies[proxy_id].bound_accounts

        assert manager.unbind_proxy_from_account('acc_1') is True
        assert manager.get_bound_proxy_id('acc_1') is None
        assert 'acc_1' not in manager.proxies[proxy_id].bound_accounts

    def test_unbind_without_binding_returns_false(self, manager):
        assert manager.unbind_proxy_from_account('acc_never_bound') is False

    def test_bind_nonexistent_proxy_returns_false(self, manager):
        assert manager.bind_proxy_to_account('proxy_missing', 'acc_1') is False

    def test_bind_rejects_when_full(self, manager):
        proxy_id = _add_proxy(manager)
        for i in range(3):  # 默认 max_accounts=3
            assert manager.bind_proxy_to_account(proxy_id, f'acc_{i}') is True

        assert manager.bind_proxy_to_account(proxy_id, 'acc_overflow') is False
        assert manager.get_bound_proxy_id('acc_overflow') is None

    def test_bind_same_account_twice_is_idempotent(self, manager):
        proxy_id = _add_proxy(manager)

        assert manager.bind_proxy_to_account(proxy_id, 'acc_1') is True
        assert manager.bind_proxy_to_account(proxy_id, 'acc_1') is True
        assert manager.proxies[proxy_id].bound_accounts.count('acc_1') == 1


# ---------------------------------------------------------------------------
# 7. rotate_proxy
# ---------------------------------------------------------------------------

class TestRotateProxy:
    def test_rotate_moves_account_to_other_proxy(self, manager):
        id_a = manager.add_proxy('http://10.0.0.1:8080')
        id_b = manager.add_proxy('http://10.0.0.2:8080')

        # A 上先压一个负载，保证 rotate 后最小负载选到 B
        manager.bind_proxy_to_account(id_a, 'acc_other')
        manager.bind_proxy_to_account(id_a, 'acc_main')

        rotated = manager.rotate_proxy('acc_main')

        assert rotated.proxy_id == id_b
        assert rotated.proxy_id != id_a
        assert manager.get_bound_proxy_id('acc_main') == id_b
        assert 'acc_main' in manager.proxies[id_b].bound_accounts
        assert 'acc_main' not in manager.proxies[id_a].bound_accounts

    def test_rotate_with_no_available_proxy_raises(self, manager):
        with pytest.raises(NoAvailableProxyException):
            manager.rotate_proxy('acc_empty_pool')


# ---------------------------------------------------------------------------
# 8. get_pool_stats
# ---------------------------------------------------------------------------

class TestPoolStats:
    def test_stats_counts_and_avg_latency(self, manager):
        id_a = manager.add_proxy('http://10.0.0.1:8080')
        id_b = manager.add_proxy('http://10.0.0.2:8080')

        # 固定延迟便于断言（重新落库）
        manager.proxies[id_a].avg_latency = 100.0
        manager._save_proxy(manager.proxies[id_a])
        manager.proxies[id_b].avg_latency = 300.0
        manager._save_proxy(manager.proxies[id_b])

        stats = manager.get_pool_stats()

        assert stats['total_proxies'] == 2
        assert stats['active_proxies'] == 2
        assert stats['failed_proxies'] == 0
        assert stats['avg_latency_ms'] == 200.0
        assert 'timestamp' in stats

    def test_stats_empty_pool(self, manager):
        stats = manager.get_pool_stats()

        assert stats['total_proxies'] == 0
        assert stats['active_proxies'] == 0
        assert stats['failed_proxies'] == 0
        assert stats['avg_latency_ms'] == 0

    def test_stats_after_failure(self, manager, mock_curl):
        manager.add_proxy('http://10.0.0.1:8080')
        manager.add_proxy('http://10.0.0.2:8080')

        mock_curl.return_value.get.return_value = Mock(status_code=503)
        manager.health_check_all()

        stats = manager.get_pool_stats()
        assert stats['total_proxies'] == 2
        assert stats['active_proxies'] == 0
        assert stats['failed_proxies'] == 2


# ---------------------------------------------------------------------------
# 9. health_check_all
# ---------------------------------------------------------------------------

class TestHealthCheckAll:
    def test_health_check_all_healthy(self, manager):
        id_a = manager.add_proxy('http://10.0.0.1:8080')

        results = manager.health_check_all()

        assert len(results) == 1
        assert results[0]['proxy_id'] == id_a
        assert results[0]['healthy'] is True

        node = manager.proxies[id_a]
        assert node.status == ProxyStatus.ACTIVE.value
        assert node.total_checks == 2  # add 时 1 次 + 本轮 1 次

    def test_health_check_all_failure_moves_to_failed(self, manager, fake_redis, mock_curl):
        id_a = manager.add_proxy('http://10.0.0.1:8080')
        mock_curl.return_value.get.return_value = Mock(status_code=503)

        results = manager.health_check_all()

        assert len(results) == 1
        assert results[0]['healthy'] is False

        node = manager.proxies[id_a]
        assert node.status == ProxyStatus.FAILED.value
        assert node.total_checks == 2
        assert node.success_checks == 1  # 失败不计入 success
        # 2026-10-08 修复：非 200 分支现在调用 update_success_rate()，
        # success_rate 反映真实成功率 (1 成功 / 2 总计 = 0.5)
        assert node.success_rate == 0.5

        # 集合迁移
        assert id_a not in fake_redis.smembers('proxies:active')
        assert id_a in fake_redis.smembers('proxies:failed')

    def test_failed_proxy_not_rechecked_by_health_check_all(self, manager, fake_redis, mock_curl):
        """当前实现只检查 proxies:active 集合，failed 代理不会自动复活（记录现状）。"""
        manager.add_proxy('http://10.0.0.1:8080')
        mock_curl.return_value.get.return_value = Mock(status_code=503)
        manager.health_check_all()

        # 即使代理已恢复（mock 返回 200），failed 集合中的代理也不会被复查
        mock_curl.return_value.get.return_value = Mock(status_code=200)
        mock_curl.return_value.get.reset_mock()
        results = manager.health_check_all()

        assert results == []
        mock_curl.return_value.get.assert_not_called()

    def test_health_check_all_skips_missing_hash(self, manager, fake_redis):
        """active 集合里有 id 但 hash 已被删（如手动 remove 后残留）应跳过。"""
        fake_redis.sadd('proxies:active', 'proxy_ghost000')
        results = manager.health_check_all()
        assert results == []


# ---------------------------------------------------------------------------
# 10. 序列化往返（跨 manager、同一 FakeRedis）
# ---------------------------------------------------------------------------

class TestSerializationRoundtrip:
    def test_new_manager_reads_back_proxy(self, manager, fake_redis):
        proxy_id = manager.add_proxy('http://user:pass@10.0.0.7:3128',
                                     proxy_type='socks5', region='EU', max_accounts=5)
        manager.bind_proxy_to_account(proxy_id, 'acc_keep')

        manager2 = IPPoolManager(redis_client=fake_redis)

        assert proxy_id in manager2.proxies
        node = manager2.proxies[proxy_id]
        assert node.proxy_url == 'http://user:pass@10.0.0.7:3128'
        assert node.proxy_type == 'socks5'
        assert node.region == 'EU'
        assert node.max_accounts == 5
        assert node.status == ProxyStatus.ACTIVE.value
        assert node.bound_accounts == ['acc_keep']
        assert isinstance(node.last_check_time, datetime)
        assert isinstance(node.created_at, datetime)

        # 绑定关系同样读回
        assert manager2.get_bound_proxy_id('acc_keep') == proxy_id
        # 新 manager 也能正常取代理
        assert manager2.get_proxy(account_id='acc_keep').proxy_id == proxy_id

    @pytest.mark.xfail(
        reason='BUG: ProxyNode.to_dict 未把 None 落为空串（对比 AccountSession.to_dict '
               '有 None→"" 保护），last_check_time=None 时 _save_proxy 触发 '
               'redis.exceptions.DataError，见报告',
        strict=False,
    )
    def test_save_proxy_with_none_last_check_time(self, manager):
        """可选 datetime 字段为 None 的节点应能落库（当前抛 DataError）。"""
        node = ProxyNode(proxy_id='proxy_nonesave', proxy_url='http://1.2.3.4:80')
        assert node.last_check_time is None
        manager._save_proxy(node)  # 当前: redis.exceptions.DataError

        loaded = manager._load_proxy('proxy_nonesave')
        assert loaded is not None
        assert loaded.last_check_time is None

    def test_redis_payload_is_json_serializable_for_bound_accounts(self, manager, fake_redis):
        proxy_id = _add_proxy(manager)
        manager.bind_proxy_to_account(proxy_id, 'acc_json')

        raw = fake_redis.hget(f'proxy:{proxy_id}', 'bound_accounts')
        assert json.loads(raw) == ['acc_json']


# ---------------------------------------------------------------------------
# 11. 代理凭据不进日志
# ---------------------------------------------------------------------------

class TestCredentialLogging:
    SECRET_URL = 'http://alice:s3cret@10.0.0.9:8080'

    def test_success_path_does_not_log_credentials(self, manager, log_capture):
        manager.add_proxy(self.SECRET_URL)

        text = _log_text(log_capture)
        assert 'alice:s3cret' not in text, '代理凭据泄露到日志'

    @pytest.mark.xfail(
        reason='BUG: ip_pool.py add_proxy 失败分支 logger.error(f"Proxy {proxy_url} '
               'failed health check") 将带凭据的完整 URL 写入日志，见报告',
        strict=False,
    )
    def test_failure_path_does_not_log_credentials(self, manager, mock_curl, log_capture):
        mock_curl.return_value.get.return_value = Mock(status_code=503)

        with pytest.raises(ProxyFailedException):
            manager.add_proxy(self.SECRET_URL)

        text = _log_text(log_capture)
        assert 'alice:s3cret' not in text, '代理凭据泄露到日志'
