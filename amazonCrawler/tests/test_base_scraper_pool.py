"""tests for BaseScraper pool wiring — use_pool=True 成败上报钩子（全 mock 离线）。

use_pool=False 路径回归由现有 test_pdp_scraper.py / test_e2e_acceptance.py 覆盖。
"""

import pytest

import src.scrapers.base_scraper as bs_mod
from src.scrapers.base_scraper import BaseScraper

PROXIES = ['http://10.0.0.1:8080', 'http://10.0.0.2:8080']


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class FakeResponse:
    def __init__(self, status_code=200, text='<html>ok</html>'):
        self.status_code = status_code
        self.text = text


class FakeClient:
    """注入 BaseScraper 的最小客户端。"""

    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.calls = []

    def request(self, url, method='GET', headers=None, **kw):
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        return self.responses.pop(0)


class FakeIPPool:
    """integration.report_outcome 的池替身（记录上报调用）。"""

    def __init__(self, urls=None):
        self._proxies = list(urls or PROXIES)
        self.successes = []
        self.failures = []

    def acquire(self):
        return self._proxies[0] if self._proxies else None

    def report_success(self, url, response_time=None):
        self.successes.append((url, response_time))

    def report_failure(self, url):
        self.failures.append(url)


class FakeFingerprintPool:
    def __init__(self):
        self.banned = []

    def acquire(self):
        return 'chrome131'

    def report_ban(self, fp):
        self.banned.append(fp)

    def report_success(self, fp):
        pass


@pytest.fixture()
def pool_env(monkeypatch):
    """隔离 report_outcome 的池依赖：注入 fake 池 + 跳过 cookie 池。"""
    import src.pools.integration as integration

    ip_pool = FakeIPPool()
    fp_pool = FakeFingerprintPool()
    monkeypatch.setattr(integration, 'get_ip_pool', lambda: ip_pool)
    monkeypatch.setattr(integration, 'get_fingerprint_pool',
                        lambda: fp_pool)
    monkeypatch.setattr(integration, 'get_cookie_pool', lambda: None)
    return ip_pool, fp_pool


def make_scraper(client, use_pool=True, min_interval=0, max_interval=0,
                 **kw):
    return BaseScraper(client=client, min_interval=min_interval,
                       max_interval=max_interval, cooldown_seconds=60,
                       use_pool=use_pool, **kw)


class TestUsePoolOff:
    def test_default_is_off(self, monkeypatch):
        monkeypatch.delenv('AMZ_POOL_ENABLED', raising=False)
        s = BaseScraper(client=FakeClient())
        assert s.use_pool is False

    def test_env_enabled(self, monkeypatch):
        monkeypatch.setenv('AMZ_POOL_ENABLED', '1')
        s = BaseScraper(client=FakeClient())
        assert s.use_pool is True

    def test_explicit_false_overrides_env(self, monkeypatch):
        monkeypatch.setenv('AMZ_POOL_ENABLED', 'true')
        s = BaseScraper(client=FakeClient(), use_pool=False)
        assert s.use_pool is False

    def test_off_path_fetch_identical(self):
        client = FakeClient(responses=[FakeResponse()])
        s = make_scraper(client, use_pool=False)
        resp = s.fetch('http://x/1')
        assert resp.status_code == 200
        assert s._pool_ctx is None
        assert s.ensure_pool_client() is None  # 池关闭不触碰 integration


class TestUsePoolOn:
    def test_fetch_success_reports_success(self, pool_env):
        ip_pool, fp_pool = pool_env
        client = FakeClient(responses=[FakeResponse()])
        s = make_scraper(client, use_pool=True)
        s.fetch('http://x/1')
        # fetch 内部 ensure_pool_client 走真实 integration → _build_client
        # 被 integration 自己构造（CurlCffiClient 离线构造成功，无网络）；
        # 上报通过 report_outcome → 注入的 fake 池
        assert len(ip_pool.successes) == 1
        assert ip_pool.failures == []
        s.close_pool_client()

    def test_503_reports_blocked(self, pool_env):
        ip_pool, fp_pool = pool_env
        client = FakeClient(responses=[FakeResponse(status_code=503)])
        s = make_scraper(client, use_pool=True)
        with pytest.raises(Exception):
            s.fetch('http://x/1')
        assert len(ip_pool.failures) == 1
        assert len(fp_pool.banned) == 1
        assert ip_pool.successes == []
        s.close_pool_client()

    def test_bot_challenge_200_reports_blocked(self, pool_env):
        ip_pool, fp_pool = pool_env
        page = ('<html><title>Robot Check</title>'
                '<input id="captcha-image"></html>')
        client = FakeClient(responses=[FakeResponse(status_code=200,
                                                    text=page)])
        s = make_scraper(client, use_pool=True)
        with pytest.raises(Exception):
            s.fetch('http://x/1')
        assert len(ip_pool.failures) == 1
        s.close_pool_client()

    def test_429_reports_blocked(self, pool_env):
        ip_pool, fp_pool = pool_env
        client = FakeClient(responses=[FakeResponse(status_code=429)])
        s = make_scraper(client, use_pool=True)
        with pytest.raises(Exception):
            s.fetch('http://x/1')
        assert len(ip_pool.failures) == 1
        s.close_pool_client()

    def test_network_error_reports_blocked(self, pool_env):
        ip_pool, fp_pool = pool_env
        client = FakeClient(error=ConnectionError('boom'))
        s = make_scraper(client, use_pool=True)
        with pytest.raises(ConnectionError):
            s.fetch('http://x/1')
        assert len(ip_pool.failures) == 1
        s.close_pool_client()

    def test_injected_client_still_reported(self, pool_env):
        """use_pool=True 且显式注入 client：上报钩子依然走池。"""
        ip_pool, fp_pool = pool_env
        client = FakeClient(responses=[FakeResponse()])
        s = make_scraper(client, use_pool=True)
        s.fetch('http://x/1')
        assert len(ip_pool.successes) == 1
        s.close_pool_client()

    def test_ensure_pool_client_idempotent(self, pool_env):
        client = FakeClient(responses=[FakeResponse(), FakeResponse()])
        s = make_scraper(client, use_pool=True)
        s.fetch('http://x/1')
        ctx1 = s._pool_ctx
        s.fetch('http://x/2')
        assert s._pool_ctx is ctx1  # 复用同一池上下文，不重复 acquire lease
        s.close_pool_client()
        assert s._pool_ctx is None

    def test_close_pool_client_releases_lease(self, pool_env, tmp_path):
        from src.pools.lease import LeaseManager
        import src.pools.integration as integration

        client = FakeClient(responses=[FakeResponse()])
        s = make_scraper(client, use_pool=True)
        s.fetch('http://x/1')
        s.close_pool_client()
        # lease 已释放：同 task_type 可再次获取（用独立 manager 验证文件锁）
        manager = LeaseManager(lock_dir=tmp_path / 'locks')
        with manager.acquire_lease('fetch', ttl=60):
            pass
