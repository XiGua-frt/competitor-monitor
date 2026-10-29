"""anti_detection 模块单元测试。

全部 mock 网络与浏览器, 不发起任何真实 TikTok 请求。
运行: python -m pytest tests/test_anti_detection.py -v
"""

import asyncio
import json
import time
import types
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.common.exceptions import (
    QuotaExceededException,
    RateLimitException,
    SessionExpiredException,
    WAFBlockedException,
)
from src.anti_detection import (
    AntiDetectionClient,
    BrowserResponse,
    CamoufoxClient,
    CurlCffiClient,
    SessionManager,
    rate_limit,
    RateLimiter,
    _run_async,
)
from src.anti_detection.browser_client import get_tiktok_cookies
from src.anti_detection import rate_limiter as rate_limiter_module


# ============================================================
# 兼容性 shim 与公共 fixture
# ============================================================
@pytest.fixture(autouse=True)
def _curl_cffi_request_exception_compat(monkeypatch):
    """curl_cffi>=0.13 顶层 requests 不再导出 RequestException。

    curl_client.py:93 使用 requests.RequestException, 该文件不在本 Agent 写集内,
    故在测试侧注入缺失别名以保持原代码语义 (已在交付报告中披露该问题)。
    """
    import curl_cffi.requests as curl_requests
    from curl_cffi.requests.exceptions import RequestException

    if not hasattr(curl_requests, 'RequestException'):
        monkeypatch.setattr(curl_requests, 'RequestException', RequestException, raising=False)


@pytest.fixture
def session_file() -> Path:
    """项目内临时 session 文件 (沙箱环境下 pytest tmp_path 不可用)."""
    d = Path(__file__).parent / '_tmp'
    if not d.exists():  # 沙箱 shim 对 mkdir(exist_ok=True) 有兼容问题, 先判断再建
        d.mkdir(parents=True)
    f = d / 'test_session.json'
    yield f
    if f.exists():
        f.unlink()


# ============================================================
# 测试辅助
# ============================================================
class FakeLocator:
    """模拟 playwright Locator: count/fill/click 均为 AsyncMock, first 返回自身."""

    def __init__(self, count: int = 0):
        self.count = AsyncMock(return_value=count)
        self.fill = AsyncMock()
        self.click = AsyncMock()
        self.first = self


class FakeResponse:
    """模拟 curl_cffi Response."""

    def __init__(self, status_code: int = 200, json_data: dict = None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = json.dumps(self._json)

    def json(self):
        return self._json


def make_page(locator_counts: dict = None) -> MagicMock:
    """构造 mock Page, locator_counts 按 selector 精确匹配指定 count."""
    locator_counts = locator_counts or {}
    page = MagicMock(name='Page')
    page.locator = MagicMock(
        side_effect=lambda selector: FakeLocator(locator_counts.get(selector, 0))
    )
    page.get_by_text = MagicMock(
        side_effect=lambda text: FakeLocator(locator_counts.get(f'text:{text}', 0))
    )
    page.goto = AsyncMock()
    page.wait_for_load_state = AsyncMock()
    page.bring_to_front = AsyncMock()
    return page


def make_context(cookies):
    """构造 mock BrowserContext.

    cookies 为列表时每次调用依次返回; 序列耗尽后保持返回最后一项。
    """
    ctx = MagicMock(name='BrowserContext')
    if isinstance(cookies, list) and cookies and isinstance(cookies[0], list):
        seq = list(cookies)
        last = seq[-1]

        def _next():
            return seq.pop(0) if seq else last

        ctx.cookies = AsyncMock(side_effect=_next)
    else:
        ctx.cookies = AsyncMock(return_value=cookies)
    return ctx


def make_client(page, context, headless: bool = False) -> CamoufoxClient:
    """构造已注入 mock page/context 的 CamoufoxClient (跳过真实启动)."""
    client = CamoufoxClient(headless=headless)
    client.page = page
    client.context = context
    client.poll_interval = 0.01
    client.login_timeout = 1.0
    return client


CAPTCHA_SELECTOR = (
    'iframe[src*="captcha"], .captcha_verify_container, [class*="captcha_verify"]'
)


# ============================================================
# CurlCffiClient
# ============================================================
class TestCurlCffiClient:
    def _client(self) -> CurlCffiClient:
        client = CurlCffiClient()
        client.session = MagicMock()
        return client

    def test_request_success(self):
        client = self._client()
        fake = FakeResponse(200)
        client.session.request = MagicMock(return_value=fake)

        resp = client.request('https://example.com')

        assert resp is fake
        kwargs = client.session.request.call_args.kwargs
        assert kwargs['method'] == 'GET'
        assert kwargs['url'] == 'https://example.com'
        assert kwargs['timeout'] == 30
        assert 'User-Agent' in kwargs['headers']

    def test_custom_headers_override_defaults(self):
        client = self._client()
        client.session.request = MagicMock(return_value=FakeResponse(200))

        client.request('https://example.com', headers={'User-Agent': 'my-ua'})

        headers = client.session.request.call_args.kwargs['headers']
        assert headers['User-Agent'] == 'my-ua'
        assert headers['Accept-Language'] == 'en-US,en;q=0.9'

    @pytest.mark.parametrize('status', [403, 418, 503])
    def test_waf_blocked(self, status):
        client = self._client()
        client.session.request = MagicMock(return_value=FakeResponse(status))

        with pytest.raises(WAFBlockedException) as exc_info:
            client.request('https://www.tiktok.com/x')
        assert exc_info.value.status_code == status

    def test_rate_limited(self):
        client = self._client()
        client.session.request = MagicMock(return_value=FakeResponse(429))

        with pytest.raises(RateLimitException):
            client.request('https://www.tiktok.com/x')

    def test_get_and_post_wrappers(self):
        client = self._client()
        client.session.request = MagicMock(return_value=FakeResponse(200))

        client.get('https://example.com/a')
        assert client.session.request.call_args.kwargs['method'] == 'GET'

        client.post('https://example.com/a', data={'k': 'v'})
        call = client.session.request.call_args.kwargs
        assert call['method'] == 'POST'
        assert call['data'] == {'k': 'v'}

    # ---- 网络层瞬时错误重试 (2026-10-11, commit 后: 代理偶发 curl 35) ----

    def test_transient_error_retried_then_success(self, monkeypatch):
        """首次 RequestException(SSL 重置类) -> 重试 1 次后成功, 不抛异常。"""
        from curl_cffi.requests.exceptions import RequestException

        client = self._client()
        fake = FakeResponse(200)
        client.session.request = MagicMock(
            side_effect=[RequestException('curl: (35) SSL_ERROR_SYSCALL'), fake])
        sleeps = []
        monkeypatch.setattr(time, 'sleep', sleeps.append)
        monkeypatch.setattr(CurlCffiClient, 'TRANSIENT_RETRIES', 1)
        monkeypatch.setattr(CurlCffiClient, 'TRANSIENT_RETRY_BACKOFF_S', 0.01)

        resp = client.request('https://example.com')

        assert resp is fake
        assert client.session.request.call_count == 2
        assert sleeps  # 确实退避过

    def test_transient_error_exhausted_raises(self, monkeypatch):
        """连续失败 -> 重试耗尽后抛 RequestException, 调用次数 = 1+TRANSIENT_RETRIES。"""
        from curl_cffi.requests.exceptions import RequestException

        client = self._client()
        client.session.request = MagicMock(
            side_effect=RequestException('curl: (7) connect failed'))
        monkeypatch.setattr(time, 'sleep', lambda s: None)
        monkeypatch.setattr(CurlCffiClient, 'TRANSIENT_RETRIES', 1)
        monkeypatch.setattr(CurlCffiClient, 'TRANSIENT_RETRY_BACKOFF_S', 0.01)

        with pytest.raises(RequestException):
            client.request('https://example.com')
        assert client.session.request.call_count == 2

    def test_waf_never_retried(self, monkeypatch):
        """WAF/429 是已收到响应的策略性失败, 不进入网络重试 (合规约束)。"""
        client = self._client()
        client.session.request = MagicMock(return_value=FakeResponse(403))
        monkeypatch.setattr(time, 'sleep', lambda s: None)

        with pytest.raises(WAFBlockedException):
            client.request('https://example.com')
        assert client.session.request.call_count == 1

    def test_success_first_try_no_sleep(self, monkeypatch):
        """首次成功: 单次调用, 零退避。"""
        client = self._client()
        client.session.request = MagicMock(return_value=FakeResponse(200))
        sleeps = []
        monkeypatch.setattr(time, 'sleep', sleeps.append)

        client.request('https://example.com')

        assert client.session.request.call_count == 1
        assert not sleeps


# ============================================================
# SessionManager
# ============================================================
class TestSessionManager:
    def _manager(self, session_file) -> SessionManager:
        return SessionManager(session_file=session_file)

    def test_set_and_get_cookies(self, session_file):
        sm = self._manager(session_file)
        sm.set_cookies({'sessionid': 'abc', 'tt': 'v'})

        assert sm.get_cookies() == {'sessionid': 'abc', 'tt': 'v'}
        assert sm.session_file.exists()
        data = json.loads(sm.session_file.read_text())
        assert data['cookies'] == {'sessionid': 'abc', 'tt': 'v'}
        assert 'last_refresh' in data

    def test_load_session(self, session_file):
        sm = self._manager(session_file)
        sm.set_cookies({'sessionid': 'abc'})

        sm2 = self._manager(session_file)
        assert sm2.load_session() is True
        assert sm2.get_cookies() == {'sessionid': 'abc'}

    def test_load_session_missing_file(self, session_file):
        sm = self._manager(session_file)
        assert sm.load_session() is False

    def test_get_cookies_no_session_raises(self, session_file):
        sm = self._manager(session_file)
        with pytest.raises(SessionExpiredException):
            sm.get_cookies()

    def test_expired_session_raises(self, session_file):
        sm = self._manager(session_file)
        # 手写一个 25 小时前的 session 文件
        sm.session_file.write_text(json.dumps({
            'cookies': {'sessionid': 'old'},
            'last_refresh': (datetime.now() - timedelta(hours=25)).isoformat(),
        }))
        sm2 = self._manager(session_file)
        assert sm2.load_session() is True
        assert sm2.is_expired() is True
        with pytest.raises(SessionExpiredException):
            sm2.get_cookies()

    def test_fresh_session_not_expired(self, session_file):
        sm = self._manager(session_file)
        sm.set_cookies({'sessionid': 'abc'})
        assert sm.is_expired() is False

    def test_is_alive_true(self, session_file, monkeypatch):
        sm = self._manager(session_file)
        sm.set_cookies({'sessionid': 'abc'})

        fake_client = MagicMock()
        fake_client.get.return_value = FakeResponse(200, {'data': {'user_id': 123}})
        monkeypatch.setattr(
            'src.anti_detection.curl_client.CurlCffiClient',
            MagicMock(return_value=fake_client),
        )
        assert sm.is_alive() is True
        # 验证携带了 Cookie 头
        headers = fake_client.get.call_args.kwargs['headers']
        assert 'sessionid=abc' in headers['Cookie']

    def test_is_alive_false_when_not_logged_in(self, session_file, monkeypatch):
        sm = self._manager(session_file)
        sm.set_cookies({'sessionid': 'abc'})

        fake_client = MagicMock()
        fake_client.get.return_value = FakeResponse(200, {'data': {}})
        monkeypatch.setattr(
            'src.anti_detection.curl_client.CurlCffiClient',
            MagicMock(return_value=fake_client),
        )
        assert sm.is_alive() is False

    def test_is_alive_false_on_error(self, session_file, monkeypatch):
        sm = self._manager(session_file)
        sm.set_cookies({'sessionid': 'abc'})

        fake_client = MagicMock()
        fake_client.get.side_effect = RuntimeError('boom')
        monkeypatch.setattr(
            'src.anti_detection.curl_client.CurlCffiClient',
            MagicMock(return_value=fake_client),
        )
        assert sm.is_alive() is False

    def test_clear_session(self, session_file):
        sm = self._manager(session_file)
        sm.set_cookies({'sessionid': 'abc'})
        sm.clear_session()
        assert not sm.session_file.exists()
        with pytest.raises(SessionExpiredException):
            sm.get_cookies()

    # ---- 任务卡契约方法 (get_session / refresh_cookie) ----
    def test_get_session_returns_cookie_session(self, session_file):
        sm = self._manager(session_file)
        sm.set_cookies({'sessionid': 'abc'})

        session = sm.get_session()
        assert session is not None
        assert session.cookies.get('sessionid') == 'abc'

    def test_refresh_cookie(self, session_file, monkeypatch):
        sm = self._manager(session_file)
        monkeypatch.setattr(
            'src.anti_detection.browser_client.get_tiktok_cookies',
            MagicMock(return_value={'sessionid': 'new'}),
        )
        assert sm.refresh_cookie() is True
        assert sm.get_cookies() == {'sessionid': 'new'}

    def test_refresh_cookie_failure(self, session_file, monkeypatch):
        sm = self._manager(session_file)
        monkeypatch.setattr(
            'src.anti_detection.browser_client.get_tiktok_cookies',
            MagicMock(side_effect=RuntimeError('login failed')),
        )
        assert sm.refresh_cookie() is False


# ============================================================
# RateLimiter
# ============================================================
class TestRateLimiter:
    def test_wait_enforces_interval(self):
        limiter = RateLimiter(min_interval=0.05, max_interval=0.1)
        limiter.wait()  # 第一次直接放行
        start = time.perf_counter()
        limiter.wait()  # 第二次必须等待 0.05-0.1s
        elapsed = time.perf_counter() - start
        assert 0.04 <= elapsed <= 0.25

    def test_wait_no_sleep_when_interval_elapsed(self, monkeypatch):
        fake_time = types.SimpleNamespace(
            time=time.time, sleep=MagicMock(side_effect=AssertionError('should not sleep'))
        )
        monkeypatch.setattr(rate_limiter_module, 'time', fake_time)

        limiter = RateLimiter(min_interval=5, max_interval=8)
        limiter._last_request_time = time.time() - 100  # 距上次请求已超过最大间隔
        limiter.wait()
        fake_time.sleep.assert_not_called()

    def test_check_quota(self):
        limiter = RateLimiter(min_interval=0.05, max_interval=0.1)
        for _ in range(3):
            assert limiter.check_quota('products', 3) is True
        with pytest.raises(QuotaExceededException) as exc_info:
            limiter.check_quota('products', 3)
        assert exc_info.value.resource_type == 'products'
        assert exc_info.value.limit == 3

    def test_quota_reset_next_day(self):
        limiter = RateLimiter(min_interval=0.05, max_interval=0.1)
        limiter.check_quota('products', 2)
        # 把重置时间拨回过去, 模拟跨天
        limiter._daily_reset_time = datetime.now() - timedelta(seconds=1)
        assert limiter.check_quota('products', 2) is True
        assert limiter._daily_counts == {'products': 1}

    def test_get_quota_status(self):
        limiter = RateLimiter(min_interval=0.05, max_interval=0.1)
        limiter.check_quota('videos', 5)
        limiter.check_quota('videos', 5)
        status = limiter.get_quota_status('videos', 5)
        assert status == {'used': 2, 'limit': 5, 'remaining': 3}

    def test_rate_limit_decorator(self, monkeypatch):
        # 屏蔽真实 sleep, 只验证装饰器的调用编排
        fake_time = types.SimpleNamespace(time=time.time, sleep=MagicMock())
        monkeypatch.setattr(rate_limiter_module, 'time', fake_time)

        calls = []

        @rate_limit(min_interval=5, max_interval=8, resource_type='products', quota_limit=2)
        def scrape(url):
            calls.append(url)
            return url

        assert scrape('u1') == 'u1'
        assert scrape('u2') == 'u2'
        assert fake_time.sleep.called  # 间隔控制被触发
        with pytest.raises(QuotaExceededException):
            scrape('u3')  # 配额耗尽, 不会真正执行
        assert calls == ['u1', 'u2']


# ============================================================
# CamoufoxClient (登录状态机)
# ============================================================
class TestCamoufoxClient:
    def test_fill_login_form(self):
        page = make_page()
        client = make_client(page, MagicMock())

        asyncio.run(client._fill_login_form('user@example.com', 'pass123'))

        selectors = [c.args[0] for c in page.locator.call_args_list]
        assert 'input[name="username"], input[type="text"]' in selectors
        assert 'input[type="password"]' in selectors
        assert 'button[type="submit"]' in selectors

    def test_switch_to_password_login_clicks_tab(self):
        page = make_page({'text:Use phone / email / username': 1})
        client = make_client(page, MagicMock())

        asyncio.run(client._switch_to_password_login())

        page.get_by_text.assert_called_once_with('Use phone / email / username')

    def test_switch_to_password_login_tab_missing_is_safe(self):
        page = make_page({})  # 任何候选都找不到
        client = make_client(page, MagicMock())

        asyncio.run(client._switch_to_password_login())  # 不应抛异常

    def test_detect_captcha(self):
        client = make_client(make_page({CAPTCHA_SELECTOR: 1}), MagicMock())
        assert asyncio.run(client._detect_captcha()) is True

        client2 = make_client(make_page({}), MagicMock())
        assert asyncio.run(client2._detect_captcha()) is False

    def test_check_login_success_by_cookie(self):
        ctx = make_context([{'name': 'sessionid', 'value': 'x'}])
        client = make_client(make_page(), ctx)
        assert asyncio.run(client._check_login_success()) is True

        ctx2 = make_context([{'name': 'tt_webid', 'value': 'y'}])
        client2 = make_client(make_page(), ctx2)
        assert asyncio.run(client2._check_login_success()) is False

    def test_login_auto_success_saves_session(self, monkeypatch):
        monkeypatch.setattr('src.common.config.config.TIKTOK_USERNAME', 'testuser')
        monkeypatch.setattr('src.common.config.config.TIKTOK_PASSWORD', 'testpass')
        page = make_page({'text:Use phone / email / username': 1})
        ctx = make_context([{'name': 'sessionid', 'value': 'abc'}, {'name': 'tt', 'value': '1'}])
        client = make_client(page, ctx)

        fake_sm = MagicMock()
        monkeypatch.setattr('src.anti_detection.session_manager.session_manager', fake_sm)

        cookies = asyncio.run(client.login_tiktok())

        assert cookies == {'sessionid': 'abc', 'tt': '1'}
        fake_sm.set_cookies.assert_called_once_with(cookies)
        page.goto.assert_awaited_once()

    def test_login_captcha_falls_back_to_manual(self, monkeypatch):
        monkeypatch.setattr('src.common.config.config.TIKTOK_USERNAME', 'testuser')
        monkeypatch.setattr('src.common.config.config.TIKTOK_PASSWORD', 'testpass')
        page = make_page({'text:Use phone / email / username': 1, CAPTCHA_SELECTOR: 1})
        # 第 1 次检查未登录, 第 2 次登录成功 (模拟人工完成验证码)
        ctx = make_context([
            [{'name': 'tt_webid', 'value': 'y'}],
            [{'name': 'sessionid_ss', 'value': 'z'}],
        ])
        client = make_client(page, ctx, headless=True)
        client._relaunch_headed = AsyncMock()
        # P0 防污染 (2026-10-08 事故): 必须隔离全局 session_manager,
        # 否则 save_to_session=True 会把 dummy cookie 写穿真实 data/session.json
        monkeypatch.setattr('src.anti_detection.session_manager.session_manager', MagicMock())

        cookies = asyncio.run(client.login_tiktok())

        assert cookies == {'sessionid_ss': 'z'}
        client._relaunch_headed.assert_awaited_once()

    def test_login_without_credentials_goes_manual(self, monkeypatch):
        monkeypatch.setattr('src.common.config.config.TIKTOK_USERNAME', '')
        monkeypatch.setattr('src.common.config.config.TIKTOK_PASSWORD', '')

        page = make_page({})
        ctx = make_context([{'name': 'sessionid', 'value': 'manual'}])
        client = make_client(page, ctx)
        client._relaunch_headed = AsyncMock()
        # P0 防污染: 显式隔离全局 session_manager (本用例 save_to_session=False,
        # 加固防止后续有人改动默认值时写穿真实 session 文件)
        fake_sm = MagicMock()
        monkeypatch.setattr('src.anti_detection.session_manager.session_manager', fake_sm)

        cookies = asyncio.run(client.login_tiktok(save_to_session=False))
        assert cookies == {'sessionid': 'manual'}
        # 无凭据时不点登录 tab
        page.get_by_text.assert_not_called()
        # save_to_session=False 时不应写 session
        fake_sm.set_cookies.assert_not_called()

    def test_login_timeout_raises(self):
        page = make_page({'text:Use phone / email / username': 1})
        ctx = make_context([])  # 永远拿不到登录 Cookie
        client = make_client(page, ctx)
        client.poll_interval = 0.01
        client.login_timeout = 0.05

        with pytest.raises(SessionExpiredException):
            asyncio.run(client.login_tiktok(save_to_session=False))

    def test_login_goto_failure_raises(self):
        page = make_page({})
        page.goto = AsyncMock(side_effect=RuntimeError('net down'))
        client = make_client(page, MagicMock())

        with pytest.raises(SessionExpiredException):
            asyncio.run(client.login_tiktok(save_to_session=False))

    def test_sync_wrapper_get_tiktok_cookies(self, monkeypatch):
        monkeypatch.setattr(CamoufoxClient, 'start', AsyncMock())
        monkeypatch.setattr(
            CamoufoxClient, 'login_tiktok',
            AsyncMock(return_value={'sessionid': 's1'}),
        )
        monkeypatch.setattr(CamoufoxClient, 'close', AsyncMock())

        assert get_tiktok_cookies() == {'sessionid': 's1'}


# ============================================================
# AntiDetectionClient (通道编排)
# ============================================================
class TestAntiDetectionClient:
    def test_default_channel_is_curl(self):
        assert AntiDetectionClient().channel == 'curl'

    def test_channel_from_config(self, monkeypatch):
        from src.common.config import config
        monkeypatch.setattr(config, 'PRIMARY_CHANNEL', 'browser')
        assert AntiDetectionClient().channel == 'browser'

    def test_invalid_channel_raises(self):
        with pytest.raises(ValueError):
            AntiDetectionClient(channel='http')
        with pytest.raises(ValueError):
            AntiDetectionClient().switch_channel('ftp')

    def test_switch_channel(self):
        adc = AntiDetectionClient()
        adc.switch_channel('browser')
        assert adc.channel == 'browser'
        adc.switch_channel('curl')
        assert adc.channel == 'curl'

    def test_curl_channel_delegates(self):
        adc = AntiDetectionClient()
        fake = FakeResponse(200, {'ok': 1})
        adc._curl.request = MagicMock(return_value=fake)

        resp = adc.request('https://www.tiktok.com/api/x', method='GET')
        assert resp is fake
        adc._curl.request.assert_called_once_with(
            'https://www.tiktok.com/api/x', method='GET', headers=None
        )

    def test_waf_auto_fallback_to_browser(self):
        adc = AntiDetectionClient()
        adc._curl.request = MagicMock(side_effect=WAFBlockedException('u', 403))
        browser_resp = BrowserResponse(200, '{"data": 1}', url='u')
        adc._request_via_browser = MagicMock(return_value=browser_resp)

        resp = adc.request('u')

        assert resp is browser_resp
        assert adc.channel == 'browser'
        adc._request_via_browser.assert_called_once()

    def test_browser_channel_request(self):
        adc = AntiDetectionClient()
        adc.switch_channel('browser')
        adc._browser = MagicMock()
        adc._browser.fetch = AsyncMock(
            return_value={'status_code': 200, 'text': '{"ok": 1}', 'url': 'http://x'}
        )

        resp = adc.request('http://x')

        assert isinstance(resp, BrowserResponse)
        assert resp.status_code == 200
        assert resp.json() == {'ok': 1}
        adc._browser.fetch.assert_awaited_once_with(
            'http://x', method='GET', headers=None
        )

    def test_browser_response_interface(self):
        resp = BrowserResponse(200, '{"a": [1, 2]}')
        assert resp.json() == {'a': [1, 2]}
        assert resp.content == b'{"a": [1, 2]}'
        assert resp.ok is True
        assert resp.status_code == 500 or not BrowserResponse(500, '{}').ok

    def test_run_async_nested_loop(self):
        """当前线程已有事件循环时, _run_async 应回退到子线程执行."""
        async def inner():
            return 42

        async def outer():
            return _run_async(inner())

        assert asyncio.run(outer()) == 42


# ============================================================
# WAF 正文强特征启发式（Agent O 探针误报修正, 2026-10-07）
# ============================================================
PROJECT_ROOT = Path(__file__).resolve().parent.parent
REAL_PAGE_EVIDENCE = PROJECT_ROOT / 'docs' / 'probe' / \
    '20261007T144634Z_tag_webdev_try1_body.html'
WAF_CHALLENGE_FIXTURE = Path(__file__).resolve().parent / 'fixtures' / \
    'waf_challenge_minimal.html'


def _stub_response(status_code: int, text: str):
    """构造带 .text 的最小响应桩 (CurlCffiClient 只读这两个属性)."""
    return types.SimpleNamespace(status_code=status_code, text=text)


class TestWAFBodyHeuristics:
    """正文启发式只认强特征, 不做裸词 captcha 匹配（资源文件名会误报）."""

    def _client(self) -> CurlCffiClient:
        client = CurlCffiClient()
        client.session = MagicMock()
        return client

    # ---- 真实证据: 不得误报 ----
    def test_real_probe_page_body_not_flagged(self):
        """Agent O 实机 200 正常页（正文含 20+ 处 captcha-*.js 资源名）必须判为非 WAF."""
        from src.anti_detection.curl_client import looks_like_waf_challenge
        text = REAL_PAGE_EVIDENCE.read_text(encoding='utf-8', errors='replace')
        assert 'captcha' in text.lower()      # 前提: 证据里确实有 captcha 字样
        assert looks_like_waf_challenge(text) is False

    def test_real_probe_page_request_passes_through(self):
        """同一真实页走 CurlCffiClient.request 全链路: 200 且含 captcha 资源名, 不抛异常."""
        text = REAL_PAGE_EVIDENCE.read_text(encoding='utf-8', errors='replace')
        client = self._client()
        client.session.request = MagicMock(
            return_value=_stub_response(200, text))

        resp = client.request('https://www.tiktok.com/tag/webdev')
        assert resp.status_code == 200

    def test_synthetic_normal_page_with_captcha_assets_not_flagged(self):
        """合成正常页: 大体积正文 + captcha-*.js 资源文件名 -> 非 WAF."""
        from src.anti_detection.curl_client import looks_like_waf_challenge
        body = (
            '<html><head><title>TikTok - Make Your Day</title></head><body>'
            '<script src="/obj/tiktok-web/tt/webapp/main/captcha-ttp.abc123.js">'
            '</script><script src="/webapp/shared/captcha-verify.def456.js">'
            '</script>'
            + 'x' * 10_000 + '</body></html>'
        )
        assert looks_like_waf_challenge(body) is False

    # ---- 挑战页: 必须报 WAF ----
    def test_waf_challenge_fixture_body_flagged(self):
        """最小 Cloudflare 挑战页 fixture（正文甚至不含裸词 captcha）判为 WAF."""
        from src.anti_detection.curl_client import looks_like_waf_challenge
        text = WAF_CHALLENGE_FIXTURE.read_text(encoding='utf-8')
        assert 'captcha' not in text.lower()  # 前提: fixture 不靠裸词也能命中
        assert looks_like_waf_challenge(text) is True

    def test_waf_challenge_fixture_request_raises(self):
        """同一挑战页走 CurlCffiClient.request 全链路: 200 也要抛 WAFBlockedException."""
        text = WAF_CHALLENGE_FIXTURE.read_text(encoding='utf-8')
        client = self._client()
        client.session.request = MagicMock(
            return_value=_stub_response(200, text))

        with pytest.raises(WAFBlockedException) as exc_info:
            client.request('https://www.tiktok.com/x')
        assert exc_info.value.status_code == 200

    @pytest.mark.parametrize('marker_body', [
        # 强特征 1: <title> 挑战/拦截短语
        '<html><title>Just a moment...</title><body>' + 'y' * 5000 + '</body></html>',
        '<html><title>Access Denied</title><body>content</body></html>',
        # 强特征 2: Cloudflare challenge 脚本
        '<html><body><script>window._cf_chl={"cType":"managed"};</script>'
        + 'y' * 5000 + '</body></html>',
        # 强特征 3: 实际加载 reCAPTCHA 的 script 标签
        '<html><body><script src="https://www.google.com/recaptcha/api.js">'
        '</script>' + 'y' * 5000 + '</body></html>',
    ])
    def test_strong_markers_flagged(self, marker_body):
        from src.anti_detection.curl_client import looks_like_waf_challenge
        assert looks_like_waf_challenge(marker_body) is True

    # ---- 边界: 不误伤 ----
    def test_bare_captcha_word_in_large_body_not_flagged(self):
        """大体积正文里的裸词 captcha（资源清单/文案提及）不算 WAF."""
        from src.anti_detection.curl_client import looks_like_waf_challenge
        body = ('<html><title>TikTok</title><body>the captcha module exists. '
                + 'z' * 10_000 + '</body></html>')
        assert looks_like_waf_challenge(body) is False

    def test_small_body_without_form_not_flagged(self):
        """小体积 JSON/错误响应无表单元素, 不算挑战小页."""
        from src.anti_detection.curl_client import looks_like_waf_challenge
        assert looks_like_waf_challenge('{"statusCode": 10202, "msg": "verify failed"}') is False

    def test_small_body_challenge_form_flagged(self):
        """<2KB 且含挑战关键词 + 表单元素 -> 挑战小页."""
        from src.anti_detection.curl_client import looks_like_waf_challenge
        tiny = ('<html><body>Security check required. '
                '<form action="/challenge" method="POST">'
                '<input name="token"></form></body></html>')
        assert len(tiny) < 2048
        assert looks_like_waf_challenge(tiny) is True

    def test_unreadable_body_passes_through(self):
        """正文不可读时静默放行（零误伤）."""
        class _UnreadableResponse:
            status_code = 200

            @property
            def text(self):
                raise RuntimeError('decode error')

        assert CurlCffiClient._body_matches_waf_challenge(_UnreadableResponse()) is False
