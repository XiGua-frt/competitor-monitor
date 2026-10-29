"""Integration tests for Cookie pool ↔ scrape pipeline (Phase 3 + Phase 4).

全部 mock: fakeredis + fake scraper/client; 无网络、无真实 Redis、无浏览器。
覆盖: 池关闭原路径 / 影子模式 / 正常注入与释放 / 异常释放 /
空池 fail_closed 与 fallback_single / shop+itemlist 全链路 /
cookie 一致性 / 日志脱敏。
"""

from datetime import datetime, timezone
from unittest.mock import Mock, patch

import pytest
import fakeredis

import main as main_cli
from src.common.config import config
from src.common.exceptions import WAFBlockedException
from src.common.logger import logger as loguru_logger
from src.pools import integration
from src.pools.cookie_pool import CookiePoolManager
from src.pools.lease import LeaseManager
from src.pools.ip_pool import IPPoolManager

SECRET_COOKIE_VALUE = 'SECRET_COOKIE_ABC123'


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
class FakeSession:
    def __init__(self):
        self.cookies = {}


class FakeClient:
    """替身客户端: 暴露 .session 供 inject 注入。"""

    def __init__(self):
        self.session = FakeSession()


class FakeCurl:
    def __init__(self):
        self.session = FakeSession()


class ClientNoSessionAttr:
    """模拟 AntiDetectionClient 真实结构: session 藏在 _curl.session。"""

    def __init__(self):
        self._curl = FakeCurl()


class FakeSnapshotEngine:
    def __init__(self):
        self.saved = []

    def save_snapshot(self, data, table):
        self.saved.append((table, data))
        return True


class FakeShopScraper:
    """ShopProductScraper 替身: 记录注入的 client, 返回固定数据。"""

    instances = []
    category_result = ['p1', 'p2']
    product_error = None

    def __init__(self, client):
        self.client = client
        FakeShopScraper.instances.append(self)

    def scrape_category(self, category_url):
        if FakeShopScraper.category_error is not None:
            raise FakeShopScraper.category_error
        return list(FakeShopScraper.category_result)

    def scrape_product(self, pid):
        if FakeShopScraper.product_error is not None:
            raise FakeShopScraper.product_error
        return {'product_id': pid, 'title': 'fake'}

    @classmethod
    def reset(cls):
        cls.instances = []
        cls.category_result = ['p1', 'p2']
        cls.category_error = None
        cls.product_error = None


# class attribute used above must exist before reset() assigns it
FakeShopScraper.category_error = None


class FakeItemListScraper:
    instances = []
    videos_error = None

    def __init__(self, client):
        self.client = client
        FakeItemListScraper.instances.append(self)

    def scrape_user_videos(self, sec_uid, unique_id=None, count=35, max_pages=3):
        if FakeItemListScraper.videos_error is not None:
            raise FakeItemListScraper.videos_error
        return [
            {
                'video_id': f'v{i}',
                'author_id': sec_uid,
                'create_time': int(datetime.now(timezone.utc).timestamp()),
                'play_count': 100 + i,
                'digg_count': 10 + i,
                'comment_count': 5 + i,
                'share_count': 1 + i,
            }
            for i in range(3)
        ]

    @classmethod
    def reset(cls):
        cls.instances = []
        cls.videos_error = None


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def redis_client():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def pool(redis_client):
    return CookiePoolManager(redis_client=redis_client)


@pytest.fixture(autouse=True)
def reset_fakes():
    FakeShopScraper.reset()
    FakeItemListScraper.reset()
    yield
    FakeShopScraper.reset()
    FakeItemListScraper.reset()


def add_account(pool, username='user_a'):
    return pool.add_account(
        username=username,
        cookies={'sessionid': SECRET_COOKIE_VALUE, 'tt-target-idc': 'useast2a'},
        proxy_id='proxy_test',
    )


def enable_pool(monkeypatch, pool, shadow=False, fail_policy='fail_closed'):
    """开启池并预置惰性单例为 fakeredis 后端的 LeaseManager(不连真实 Redis)。"""
    monkeypatch.setattr(config, 'POOL_ENABLED', True)
    monkeypatch.setattr(config, 'POOL_SHADOW_MODE', shadow)
    monkeypatch.setattr(config, 'POOL_FAIL_POLICY', fail_policy)
    monkeypatch.setattr(config, 'POOL_STRATEGY', 'weighted')
    monkeypatch.setattr(config, 'POOL_LEASE_TTL_SECONDS', 60)
    mgr = LeaseManager(pool, lease_ttl_seconds=60, max_acquire_attempts=5)
    monkeypatch.setattr(integration, '_lease_manager', mgr)
    return mgr


def disable_pool(monkeypatch):
    """保持池关闭默认态, 并确保单例从未被创建。"""
    monkeypatch.setattr(config, 'POOL_ENABLED', False)
    monkeypatch.setattr(config, 'POOL_SHADOW_MODE', False)
    monkeypatch.setattr(integration, '_lease_manager', None)


def patch_cli(monkeypatch, engine):
    """让 run_*_scrape 不连库、不构造真实 client/scraper。"""
    monkeypatch.setattr(main_cli, 'check_db', lambda: None)
    monkeypatch.setattr(main_cli, 'SnapshotDiffEngine', lambda: engine)
    monkeypatch.setattr(main_cli, '_make_client', FakeClient)
    monkeypatch.setattr(main_cli, 'ShopProductScraper', FakeShopScraper)
    monkeypatch.setattr(main_cli, 'ItemListScraper', FakeItemListScraper)


def lease_keys(redis_client):
    return [k for k in redis_client.keys('lease:*')]


# ---------------------------------------------------------------------------
# 1. 池关闭: 原路径, 不触碰 Redis
# ---------------------------------------------------------------------------
class TestPoolDisabled:
    def test_disabled_uses_original_path_never_touches_redis(
            self, monkeypatch, pool):
        engine = FakeSnapshotEngine()
        patch_cli(monkeypatch, engine)
        disable_pool(monkeypatch)
        add_account(pool)  # 池里有号也不该被触碰

        code = main_cli.run_shop_scrape('https://example.com/cat', limit=5)

        assert code == main_cli.EXIT_OK
        assert len(engine.saved) == 2
        # 惰性单例从未创建 → 从未连 Redis / 分配 lease
        assert integration._lease_manager is None
        assert lease_keys(pool.redis) == []
        # 未注入任何 cookie
        assert FakeShopScraper.instances[0].client.session.cookies == {}

    def test_disabled_lease_context_is_empty(self, monkeypatch):
        with integration.pool_scoped_client('shop', task_id='t1') as ctx:
            assert ctx.lease is None
            assert ctx.shadow is False
            assert ctx.active_lease is None

    def test_dry_run_flag_skips_pool(self, monkeypatch):
        def _boom():
            raise AssertionError('dry_run must not touch lease manager')

        monkeypatch.setattr(integration, 'get_lease_manager', _boom)
        with integration.pool_scoped_client('shop', task_id='t1',
                                            dry_run=True) as ctx:
            assert ctx.lease is None


# ---------------------------------------------------------------------------
# 2. 影子模式: 选号记日志, 不注入
# ---------------------------------------------------------------------------
class TestShadowMode:
    def test_shadow_acquires_releases_but_no_injection(
            self, monkeypatch, pool, redis_client):
        engine = FakeSnapshotEngine()
        patch_cli(monkeypatch, engine)
        account_id = add_account(pool)
        enable_pool(monkeypatch, pool, shadow=True)

        code = main_cli.run_shop_scrape('https://example.com/cat')

        assert code == main_cli.EXIT_OK
        # client 未被注入 cookie
        assert FakeShopScraper.instances[0].client.session.cookies == {}
        # lease acquire/release 均发生: key 出现后被清理
        assert lease_keys(redis_client) == []
        # 底层计数 +1(成功释放)
        assert pool.accounts[account_id].daily_request_count == 1
        assert pool.accounts[account_id].success_requests == 1

    def test_shadow_log_contains_selection_result(
            self, monkeypatch, pool):
        engine = FakeSnapshotEngine()
        patch_cli(monkeypatch, engine)
        account_id = add_account(pool)
        enable_pool(monkeypatch, pool, shadow=True)

        messages = []
        handler_id = loguru_logger.add(messages.append, level='DEBUG')
        try:
            main_cli.run_shop_scrape('https://example.com/cat')
        finally:
            loguru_logger.remove(handler_id)

        blob = ''.join(str(m) for m in messages)
        assert 'result=shadow' in blob
        assert f'account_id={account_id}' in blob
        assert SECRET_COOKIE_VALUE not in blob


# ---------------------------------------------------------------------------
# 3. 池开启 + 正常路径: 注入 + 成功释放
# ---------------------------------------------------------------------------
class TestPoolEnabledSuccess:
    def test_cookies_injected_and_lease_released_success(
            self, monkeypatch, pool, redis_client):
        engine = FakeSnapshotEngine()
        patch_cli(monkeypatch, engine)
        account_id = add_account(pool)
        enable_pool(monkeypatch, pool)

        code = main_cli.run_shop_scrape('https://example.com/cat', limit=5)

        assert code == main_cli.EXIT_OK
        # 注入与账号 cookies 一致
        injected = FakeShopScraper.instances[0].client.session.cookies
        assert injected == {'sessionid': SECRET_COOKIE_VALUE,
                            'tt-target-idc': 'useast2a'}
        # lease key 已清理
        assert lease_keys(redis_client) == []
        # cookie_pool 计数 +1
        session = pool.accounts[account_id]
        assert session.daily_request_count == 1
        assert session.total_requests == 1
        assert session.success_requests == 1

    def test_empty_scrape_result_releases_failure(
            self, monkeypatch, pool, redis_client):
        engine = FakeSnapshotEngine()
        patch_cli(monkeypatch, engine)
        account_id = add_account(pool)
        enable_pool(monkeypatch, pool)
        FakeShopScraper.category_result = []

        code = main_cli.run_shop_scrape('https://example.com/cat')

        assert code == main_cli.EXIT_SCRAPE_FAILED
        assert lease_keys(redis_client) == []
        session = pool.accounts[account_id]
        assert session.daily_request_count == 1
        assert session.success_requests == 0  # 失败释放

    def test_scraper_exception_still_releases_lease(
            self, monkeypatch, pool, redis_client):
        engine = FakeSnapshotEngine()
        patch_cli(monkeypatch, engine)
        account_id = add_account(pool)
        enable_pool(monkeypatch, pool)
        FakeShopScraper.category_error = WAFBlockedException(
            'https://example.com/cat', 403)

        with pytest.raises(WAFBlockedException):
            main_cli.run_shop_scrape('https://example.com/cat')

        # 异常路径 lease 仍被释放(finally 保证), 不换号重试
        assert lease_keys(redis_client) == []
        session = pool.accounts[account_id]
        assert session.daily_request_count == 1
        assert session.success_requests == 0

    def test_identity_fixed_within_task(self, monkeypatch, pool):
        """一个任务内身份固定: 单 lease, 采集全程同一 client/账号。"""
        engine = FakeSnapshotEngine()
        patch_cli(monkeypatch, engine)
        add_account(pool)
        add_account(pool, 'user_b')
        enable_pool(monkeypatch, pool)

        code = main_cli.run_shop_scrape('https://example.com/cat')

        assert code == main_cli.EXIT_OK
        assert len(FakeShopScraper.instances) == 1  # 全程同一个 client


# ---------------------------------------------------------------------------
# 4. 空池: fail_closed / fallback_single
# ---------------------------------------------------------------------------
class TestEmptyPoolPolicy:
    def test_fail_closed_raises_distinguishable_error(self, monkeypatch, pool):
        engine = FakeSnapshotEngine()
        patch_cli(monkeypatch, engine)
        enable_pool(monkeypatch, pool, fail_policy='fail_closed')
        # 池为空: 不 add_account

        with pytest.raises(integration.PoolUnavailableError) as exc_info:
            main_cli.run_shop_scrape('https://example.com/cat')

        # 不静默回退: scraper 从未被构造, 没有任何数据入库
        assert FakeShopScraper.instances == []
        assert engine.saved == []
        assert 'fail_closed' in str(exc_info.value)

    def test_fail_closed_maps_to_exit_code_2(self, monkeypatch, pool):
        """main() 捕获 PoolUnavailableError → 退出码 2(依赖错误)。"""
        engine = FakeSnapshotEngine()
        patch_cli(monkeypatch, engine)
        enable_pool(monkeypatch, pool, fail_policy='fail_closed')

        code = main_cli.main(['scrape', 'shop',
                              '--category-url', 'https://example.com/cat'])
        assert code == main_cli.EXIT_CONFIG_ERROR

    def test_fallback_single_returns_to_original_path(
            self, monkeypatch, pool):
        engine = FakeSnapshotEngine()
        patch_cli(monkeypatch, engine)
        enable_pool(monkeypatch, pool, fail_policy='fallback_single')
        # 池为空

        messages = []
        handler_id = loguru_logger.add(messages.append, level='DEBUG')
        try:
            code = main_cli.run_shop_scrape('https://example.com/cat')
        finally:
            loguru_logger.remove(handler_id)

        assert code == main_cli.EXIT_OK
        # 回退原路径: 未注入 cookie, 但采集照常完成
        assert FakeShopScraper.instances[0].client.session.cookies == {}
        assert len(engine.saved) == 2
        blob = ''.join(str(m) for m in messages)
        assert 'result=fallback' in blob

    def test_exhausted_pool_fail_closed(self, monkeypatch, pool):
        """全占用(LeaseExhaustedException)同样走 fail_closed。"""
        engine = FakeSnapshotEngine()
        patch_cli(monkeypatch, engine)
        enable_pool(monkeypatch, pool, fail_policy='fail_closed')
        add_account(pool)
        mgr = integration.get_lease_manager()
        mgr.acquire('shop', 'task_other')  # 唯一账号被占

        with pytest.raises(integration.PoolUnavailableError):
            main_cli.run_shop_scrape('https://example.com/cat')

        mgr.release(mgr.active_leases()[0], success=True)


# ---------------------------------------------------------------------------
# 5. itemlist 全链路
# ---------------------------------------------------------------------------
class TestItemListFullChain:
    def test_itemlist_full_chain_inject_and_release(
            self, monkeypatch, pool, redis_client):
        engine = FakeSnapshotEngine()
        patch_cli(monkeypatch, engine)
        account_id = add_account(pool)
        enable_pool(monkeypatch, pool)

        code = main_cli.run_itemlist_scrape('SECUID123', handle='maker',
                                            count=35, pages=1)

        assert code == main_cli.EXIT_OK
        injected = FakeItemListScraper.instances[0].client.session.cookies
        assert injected == {'sessionid': SECRET_COOKIE_VALUE,
                            'tt-target-idc': 'useast2a'}
        assert len(engine.saved) == 3
        assert all(table == 'video_snapshots' for table, _ in engine.saved)
        assert lease_keys(redis_client) == []
        assert pool.accounts[account_id].daily_request_count == 1

    def test_itemlist_exception_still_releases(
            self, monkeypatch, pool, redis_client):
        engine = FakeSnapshotEngine()
        patch_cli(monkeypatch, engine)
        add_account(pool)
        enable_pool(monkeypatch, pool)
        FakeItemListScraper.videos_error = WAFBlockedException(
            'https://www.tiktok.com/@x', 403)

        with pytest.raises(WAFBlockedException):
            main_cli.run_itemlist_scrape('SECUID123')

        assert lease_keys(redis_client) == []


# ---------------------------------------------------------------------------
# 6. 注入一致性 / 零侵入注入细节
# ---------------------------------------------------------------------------
class TestInjectCookie:
    def test_injected_cookies_match_lease_account(self, pool):
        account_id = add_account(pool)
        mgr = LeaseManager(pool, lease_ttl_seconds=60)
        lease = mgr.acquire('shop', 'task_001')

        client = FakeClient()
        integration.inject_cookie_to_client(client, lease)

        assert client.session.cookies == dict(lease.account.cookies)
        assert client.session.cookies['sessionid'] == SECRET_COOKIE_VALUE
        assert lease.account_id == account_id

    def test_inject_via_curl_session_for_real_client_shape(self, pool):
        """AntiDetectionClient 真实结构(session 在 _curl.session)也能注入。"""
        add_account(pool)
        mgr = LeaseManager(pool, lease_ttl_seconds=60)
        lease = mgr.acquire('shop', 'task_001')

        client = ClientNoSessionAttr()
        integration.inject_cookie_to_client(client, lease)

        assert client._curl.session.cookies['sessionid'] == SECRET_COOKIE_VALUE

    def test_inject_none_lease_is_noop(self):
        client = FakeClient()
        integration.inject_cookie_to_client(client, None)
        assert client.session.cookies == {}

    def test_inject_does_not_touch_proxy_settings(self, pool, monkeypatch):
        add_account(pool)
        mgr = LeaseManager(pool, lease_ttl_seconds=60)
        lease = mgr.acquire('shop', 'task_001')

        client = FakeClient()
        client.session.proxies = {'http': 'http://127.0.0.1:7892'}
        monkeypatch.setattr(config, 'PROXY_ENABLED', True)
        integration.inject_cookie_to_client(client, lease)

        # 注入只更新 cookies, 代理配置原样保留(本阶段不接 IP 池)
        assert client.session.proxies == {'http': 'http://127.0.0.1:7892'}


# ---------------------------------------------------------------------------
# 7. 日志脱敏
# ---------------------------------------------------------------------------
class TestLogHygiene:
    def test_logs_never_contain_cookie_values(self, monkeypatch, pool):
        engine = FakeSnapshotEngine()
        patch_cli(monkeypatch, engine)
        add_account(pool)
        enable_pool(monkeypatch, pool)

        messages = []
        handler_id = loguru_logger.add(messages.append, level='DEBUG')
        try:
            code = main_cli.run_shop_scrape('https://example.com/cat')
        finally:
            loguru_logger.remove(handler_id)

        assert code == main_cli.EXIT_OK
        blob = ''.join(str(m) for m in messages)
        assert SECRET_COOKIE_VALUE not in blob
        assert 'sessionid' not in blob
        assert 'tt-target-idc' not in blob
        # 结构化字段正常输出
        assert 'task_id=' in blob
        assert 'scraper_type=shop' in blob
        assert 'result=success' in blob

    def test_fail_closed_log_contains_no_cookies(self, monkeypatch, pool):
        patch_cli(monkeypatch, FakeSnapshotEngine())
        enable_pool(monkeypatch, pool, fail_policy='fail_closed')

        messages = []
        handler_id = loguru_logger.add(messages.append, level='DEBUG')
        try:
            with pytest.raises(integration.PoolUnavailableError):
                main_cli.run_shop_scrape('https://example.com/cat')
        finally:
            loguru_logger.remove(handler_id)

        blob = ''.join(str(m) for m in messages)
        assert SECRET_COOKIE_VALUE not in blob


# ---------------------------------------------------------------------------
# 8. IP 池代理注入 (POOL_IP_ENABLED)
# ---------------------------------------------------------------------------
VALID_TRIAL_PROXY = 'http://trial_user:trial_secret@1.2.3.4:8080'


@pytest.fixture
def ip_pool(redis_client):
    """带 mock 健康检查(默认通过)的 IPPoolManager, 挂到 integration 单例。"""
    with patch('src.anti_detection.curl_client.CurlCffiClient') as mock_cls:
        mock_cls.return_value.get.return_value = Mock(status_code=200)
        mgr = IPPoolManager(redis_client=redis_client)
        yield mgr


def enable_ip_pool(monkeypatch, ip_pool_mgr):
    monkeypatch.setattr(config, 'POOL_IP_ENABLED', True)
    monkeypatch.setattr(integration, '_ip_pool_manager', ip_pool_mgr)


def acquire_lease(pool, username='user_a'):
    add_account(pool, username=username)
    return LeaseManager(pool, lease_ttl_seconds=60).acquire('shop', 'task_ip001')


class TestInjectProxy:
    def test_disabled_by_default_no_injection(self, pool, monkeypatch):
        monkeypatch.setattr(config, 'POOL_IP_ENABLED', False)
        lease = acquire_lease(pool)
        client = FakeClient()
        client.session.proxies = {'http': 'http://127.0.0.1:7892'}

        result = integration.inject_proxy_to_client(client, lease)

        assert result is None
        assert client.session.proxies == {'http': 'http://127.0.0.1:7892'}

    def test_enabled_injects_bound_proxy(self, pool, ip_pool, monkeypatch):
        enable_ip_pool(monkeypatch, ip_pool)
        lease = acquire_lease(pool)
        proxy_id = ip_pool.add_proxy(VALID_TRIAL_PROXY, region='US')
        assert ip_pool.bind_proxy_to_account(proxy_id, lease.account_id)

        client = FakeClient()
        result = integration.inject_proxy_to_client(client, lease)

        assert result == proxy_id
        assert client.session.proxies == {
            'http': VALID_TRIAL_PROXY,
            'https': VALID_TRIAL_PROXY,
        }

    def test_enabled_auto_assigns_when_unbound(self, pool, ip_pool, monkeypatch):
        enable_ip_pool(monkeypatch, ip_pool)
        lease = acquire_lease(pool)
        ip_pool.add_proxy(VALID_TRIAL_PROXY, region='US')

        client = FakeClient()
        result = integration.inject_proxy_to_client(client, lease)

        assert result is not None
        assert client.session.proxies['https'] == VALID_TRIAL_PROXY
        # 自动分配后应留下绑定关系
        assert ip_pool.get_bound_proxy_id(lease.account_id) == result

    def test_no_available_proxy_keeps_existing_config(self, pool, ip_pool, monkeypatch):
        enable_ip_pool(monkeypatch, ip_pool)  # 池为空
        lease = acquire_lease(pool)
        client = FakeClient()
        client.session.proxies = {'http': 'http://127.0.0.1:7892'}

        result = integration.inject_proxy_to_client(client, lease)

        assert result is None
        assert client.session.proxies == {'http': 'http://127.0.0.1:7892'}

    def test_none_lease_is_noop(self):
        client = FakeClient()
        assert integration.inject_proxy_to_client(client, None) is None
        assert not hasattr(client.session, 'proxies')

    def test_proxy_credentials_never_in_logs(self, pool, ip_pool, monkeypatch):
        enable_ip_pool(monkeypatch, ip_pool)
        lease = acquire_lease(pool)
        ip_pool.add_proxy(VALID_TRIAL_PROXY, region='US')

        client = FakeClient()
        messages = []
        handler_id = loguru_logger.add(messages.append, level='DEBUG')
        try:
            integration.inject_proxy_to_client(client, lease)
        finally:
            loguru_logger.remove(handler_id)

        blob = ''.join(str(m) for m in messages)
        assert 'trial_secret' not in blob
        assert 'trial_user' not in blob
        assert '1.2.3.4:8080' in blob  # host:port 可见, 凭据不可见
        assert 'proxy_id=' in blob

    def test_shadow_full_chain_no_proxy_injection(self, monkeypatch, pool, ip_pool):
        """影子模式: active_lease 为 None, 全链路不注入代理。"""
        engine = FakeSnapshotEngine()
        patch_cli(monkeypatch, engine)
        add_account(pool)
        enable_pool(monkeypatch, pool, shadow=True)
        enable_ip_pool(monkeypatch, ip_pool)
        ip_pool.add_proxy(VALID_TRIAL_PROXY, region='US')

        code = main_cli.run_shop_scrape('https://example.com/cat')

        assert code == main_cli.EXIT_OK
        for scraper in FakeShopScraper.instances:
            assert not hasattr(scraper.client.session, 'proxies')

    def test_real_chain_proxy_reaches_scraper_client(self, monkeypatch, pool, ip_pool):
        """非影子全链路: 代理注入到达 scraper 持有的 client。"""
        engine = FakeSnapshotEngine()
        patch_cli(monkeypatch, engine)
        add_account(pool)
        enable_pool(monkeypatch, pool)
        enable_ip_pool(monkeypatch, ip_pool)
        ip_pool.add_proxy(VALID_TRIAL_PROXY, region='US')

        code = main_cli.run_shop_scrape('https://example.com/cat')

        assert code == main_cli.EXIT_OK
        for scraper in FakeShopScraper.instances:
            assert scraper.client.session.proxies['https'] == VALID_TRIAL_PROXY


class TestMaskProxyUrl:
    def test_masks_userinfo(self):
        assert integration.mask_proxy_url(
            'http://user:secret@1.2.3.4:8080') == 'http://***@1.2.3.4:8080'

    def test_keeps_url_without_credentials(self):
        url = 'http://127.0.0.1:7892'
        assert integration.mask_proxy_url(url) == url

    def test_masked_output_hides_password(self):
        masked = integration.mask_proxy_url('http://u:p@host:9')
        assert 'p@' not in masked and 'u:' not in masked
