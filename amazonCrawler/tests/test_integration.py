"""tests for pools integration — pool_scoped_client 全链路（注入假池）/ lease
冲突 → PoolUnavailableError / report_outcome 上报。全部离线。"""

import pytest

from src.pools.fingerprint_pool import FingerprintPool
from src.pools.integration import (
    PoolScopedContext,
    PoolUnavailableError,
    inject_proxy_to_client,
    pool_scoped_client,
    report_outcome,
    reset_for_tests,
)
from src.pools.ip_pool import IPPool
from src.pools.lease import LeaseManager

PROXIES = ['http://10.0.0.1:8080', 'http://10.0.0.2:8080']


class FakeClient:
    """pool_scoped_client 组装产物的替身（拦截 CurlCffiClient 构造）。"""

    def __init__(self, impersonate=None, proxies=None):
        self.impersonate = impersonate
        self.proxies = proxies
        self.session = None
        self.closed = False

    def request(self, url, method='GET', headers=None, **kw):
        raise RuntimeError('offline')

    def close(self):
        self.closed = True


@pytest.fixture()
def fake_build(monkeypatch):
    """把 integration._build_client 替换为 FakeClient 工厂，记录构造参数。"""
    import src.pools.integration as integration

    created = []

    def factory(fingerprint, proxy_url):
        proxies = ({'http': proxy_url, 'https': proxy_url}
                   if proxy_url else None)
        client = FakeClient(impersonate=fingerprint, proxies=proxies)
        created.append(client)
        return client

    monkeypatch.setattr(integration, '_build_client', factory)
    return created


@pytest.fixture(autouse=True)
def _reset_singletons():
    reset_for_tests()
    yield
    reset_for_tests()


def make_pools():
    return (IPPool(PROXIES), FingerprintPool(strategy='rotate'))


class TestPoolScopedClient:
    def test_yields_context_with_proxy_and_fingerprint(self, fake_build):
        ip_pool, fp_pool = make_pools()
        with pool_scoped_client('pdp', ip_pool=ip_pool,
                                fingerprint_pool=fp_pool) as ctx:
            assert isinstance(ctx, PoolScopedContext)
            assert ctx.proxy_url in PROXIES
            assert ctx.fingerprint == 'chrome131'  # rotate 首个
            assert ctx.client.impersonate == 'chrome131'
            assert ctx.client.proxies == {'http': ctx.proxy_url,
                                          'https': ctx.proxy_url}

    def test_empty_pool_falls_back_to_single_proxy(self, fake_build):
        import src.pools.integration as integration

        class FakeConfig:
            PROXY_URL = 'http://fallback:7892'

        integration._app_config = FakeConfig()
        try:
            with pool_scoped_client('pdp', ip_pool=IPPool([]),
                                    fingerprint_pool=FingerprintPool()) as ctx:
                assert ctx.proxy_url == 'http://fallback:7892'
        finally:
            integration._app_config = None

    def test_no_proxy_bare_client(self, fake_build):
        import src.pools.integration as integration

        class FakeConfig:
            PROXY_URL = None

        integration._app_config = FakeConfig()
        try:
            with pool_scoped_client('pdp', ip_pool=IPPool([]),
                                    fingerprint_pool=FingerprintPool()) as ctx:
                assert ctx.proxy_url is None
                assert ctx.client.proxies is None
        finally:
            integration._app_config = None

    def test_scraper_receives_client(self, fake_build):
        class FakeScraper:
            _client = None

        ip_pool, fp_pool = make_pools()
        scraper = FakeScraper()
        with pool_scoped_client('pdp', scraper=scraper, ip_pool=ip_pool,
                                fingerprint_pool=fp_pool) as ctx:
            assert scraper._client is ctx.client

    def test_client_closed_on_exit(self, fake_build):
        ip_pool, fp_pool = make_pools()
        with pool_scoped_client('pdp', ip_pool=ip_pool,
                                fingerprint_pool=fp_pool) as ctx:
            pass
        assert ctx.client.closed is True  # 退出自动清理 session 级资源

    def test_lease_released_after_exit(self, fake_build, tmp_path):
        ip_pool, fp_pool = make_pools()
        manager = LeaseManager(lock_dir=tmp_path / 'locks')
        with pool_scoped_client('pdp', lease_manager=manager, ip_pool=ip_pool,
                                fingerprint_pool=fp_pool):
            pass
        # lease 已释放：同 task_type 可再次进入
        with pool_scoped_client('pdp', lease_manager=manager, ip_pool=ip_pool,
                                fingerprint_pool=fp_pool):
            pass


class TestLeaseConflict:
    def test_conflict_raises_pool_unavailable_error(self, fake_build, tmp_path):
        ip_pool, fp_pool = make_pools()
        manager = LeaseManager(lock_dir=tmp_path / 'locks')
        with pool_scoped_client('pdp', lease_manager=manager, ip_pool=ip_pool,
                                fingerprint_pool=fp_pool):
            with pytest.raises(PoolUnavailableError):
                with pool_scoped_client('pdp', lease_manager=manager,
                                        ip_pool=ip_pool,
                                        fingerprint_pool=fp_pool):
                    pass  # pragma: no cover

    def test_conflict_does_not_leak_context(self, fake_build, tmp_path):
        ip_pool, fp_pool = make_pools()
        manager = LeaseManager(lock_dir=tmp_path / 'locks')
        with pool_scoped_client('pdp', lease_manager=manager, ip_pool=ip_pool,
                                fingerprint_pool=fp_pool) as outer:
            with pytest.raises(PoolUnavailableError):
                with pool_scoped_client('pdp', lease_manager=manager,
                                        ip_pool=ip_pool,
                                        fingerprint_pool=fp_pool):
                    pass
            assert outer.client.closed is False  # 外层不受影响


class TestReportOutcome:
    def test_blocked_reports_all_pools(self, fake_build, tmp_path):
        ip_pool, fp_pool = make_pools()
        manager = LeaseManager(lock_dir=tmp_path / 'locks')
        with pool_scoped_client('pdp', lease_manager=manager, ip_pool=ip_pool,
                                fingerprint_pool=fp_pool) as ctx:
            report_outcome(ctx, blocked=True, ip_pool=ip_pool,
                           fingerprint_pool=fp_pool)
        assert ip_pool.healthy_count() == 1  # 绑定代理进入冷却
        assert fp_pool.healthy_count() == len(FINGERPRINTS_FPS) - 1

    def test_ok_reports_success(self, fake_build, tmp_path):
        ip_pool, fp_pool = make_pools()
        manager = LeaseManager(lock_dir=tmp_path / 'locks')
        with pool_scoped_client('pdp', lease_manager=manager, ip_pool=ip_pool,
                                fingerprint_pool=fp_pool) as ctx:
            report_outcome(ctx, ok=True, response_time=0.4, ip_pool=ip_pool,
                           fingerprint_pool=fp_pool)
        stats = {s['proxy']: s for s in ip_pool.stats()}
        assert stats[ctx.proxy_url]['success_count'] == 1
        assert stats[ctx.proxy_url]['avg_response_time'] == 0.4

    def test_blocked_wins_over_ok(self, fake_build, tmp_path):
        ip_pool, fp_pool = make_pools()
        manager = LeaseManager(lock_dir=tmp_path / 'locks')
        with pool_scoped_client('pdp', lease_manager=manager, ip_pool=ip_pool,
                                fingerprint_pool=fp_pool) as ctx:
            report_outcome(ctx, ok=True, blocked=True, ip_pool=ip_pool,
                           fingerprint_pool=fp_pool)
        assert ip_pool.healthy_count() == 1  # 按 blocked 处理

    def test_no_proxy_blocked_still_bans_fingerprint(self, fake_build, tmp_path):
        import src.pools.integration as integration

        class FakeConfig:
            PROXY_URL = None

        integration._app_config = FakeConfig()
        try:
            ip_pool, fp_pool = make_pools()
            ip_pool.update_proxies([])
            manager = LeaseManager(lock_dir=tmp_path / 'locks')
            with pool_scoped_client('pdp', lease_manager=manager,
                                    ip_pool=ip_pool,
                                    fingerprint_pool=fp_pool) as ctx:
                assert ctx.proxy_url is None
                report_outcome(ctx, blocked=True, ip_pool=ip_pool,
                               fingerprint_pool=fp_pool)
            assert fp_pool.healthy_count() == len(FINGERPRINTS_FPS) - 1
        finally:
            integration._app_config = None


FINGERPRINTS_FPS = ['chrome131', 'chrome124', 'chrome120', 'chrome116',
                    'edge101', 'safari17_0']


class TestInjectProxy:
    def test_inject_into_session(self):
        class FakeSession:
            proxies = None

        class FakeClient:
            session = FakeSession()

        client = FakeClient()
        assert inject_proxy_to_client(client, 'http://a:1') is True
        assert client.session.proxies == {'http': 'http://a:1',
                                          'https': 'http://a:1'}

    def test_no_session_returns_false(self):
        assert inject_proxy_to_client(FakeClient(), 'http://a:1') is False

    def test_empty_proxy_noop(self):
        assert inject_proxy_to_client(FakeClient(), None) is False


class TestSingletons:
    def test_get_ip_pool_cached(self):
        pool1 = __import__('src.pools.integration',
                           fromlist=['get_ip_pool']).get_ip_pool()
        pool2 = __import__('src.pools.integration',
                           fromlist=['get_ip_pool']).get_ip_pool()
        assert pool1 is pool2

    def test_reset_for_tests_clears(self):
        integration = __import__('src.pools.integration', fromlist=['x'])
        pool1 = integration.get_ip_pool()
        integration.reset_for_tests()
        pool2 = integration.get_ip_pool()
        assert pool1 is not pool2
