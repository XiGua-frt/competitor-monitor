"""Unit tests for AutoLoginBootstrapper (fully mocked: no browser, no network, no real Redis)."""

import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import Mock, AsyncMock, patch

import pytest
from fakeredis import FakeRedis

from src.pools.bootstrapper import AutoLoginBootstrapper
from src.pools.cookie_pool import CookiePoolManager
from src.pools.ip_pool import IPPoolManager
from src.pools.models import ProxyNode


@pytest.fixture
def tmp_dir():
    d = Path(tempfile.mkdtemp(prefix='tk_bootstrapper_test_'))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def mock_redis():
    return FakeRedis(decode_responses=True)


@pytest.fixture
def pools(mock_redis):
    return CookiePoolManager(mock_redis), IPPoolManager(mock_redis)


@pytest.fixture
def bootstrapper(tmp_dir, pools):
    cookie_pool, ip_pool = pools
    with patch('src.pools.bootstrapper.CAMOUFOX_AVAILABLE', True):
        bs = AutoLoginBootstrapper(
            cookie_pool=cookie_pool, ip_pool=ip_pool,
            headless=True, save_cookies_dir=tmp_dir / 'cookies')
    return bs


def make_mock_page(cookies):
    page = AsyncMock()
    page.context.cookies.return_value = cookies
    return page


class TestInit:
    def test_init_params(self, bootstrapper, tmp_dir):
        assert bootstrapper.headless is True
        assert bootstrapper.save_cookies_dir == tmp_dir / 'cookies'
        assert bootstrapper.save_cookies_dir.exists()
        delay = AutoLoginBootstrapper.LOGIN_CONFIG['post_login_delay']
        assert delay[0] < delay[1] and delay[0] > 0

    def test_init_requires_camoufox(self, tmp_dir):
        with patch('src.pools.bootstrapper.CAMOUFOX_AVAILABLE', False):
            with pytest.raises(ImportError, match='camoufox'):
                AutoLoginBootstrapper(
                    cookie_pool=Mock(), ip_pool=Mock(), save_cookies_dir=tmp_dir)

    def test_critical_cookies_in_first_five(self):
        critical = AutoLoginBootstrapper.REQUIRED_COOKIES[:5]
        assert 'sessionid' in critical and 'ttwid' in critical
        assert len(AutoLoginBootstrapper.REQUIRED_COOKIES) >= 27


class TestLoginValidation:
    @pytest.mark.asyncio
    async def test_missing_phone_returns_none(self, bootstrapper):
        with patch('src.pools.bootstrapper.AsyncCamoufox') as mb:
            assert await bootstrapper._login_single_account({'password': 'pw'}) is None
            mb.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_password_returns_none(self, bootstrapper):
        with patch('src.pools.bootstrapper.AsyncCamoufox') as mb:
            assert await bootstrapper._login_single_account({'phone': '13800138000'}) is None
            mb.assert_not_called()


class TestProxySelection:
    @pytest.mark.asyncio
    async def test_get_available_proxy(self, bootstrapper):
        node = ProxyNode(proxy_id='p1', proxy_url='http://u:p@h:8080')
        bootstrapper.ip_pool = Mock(get_proxy=Mock(return_value=node))
        assert await bootstrapper._get_available_proxy() == ('p1', 'http://u:p@h:8080')

    @pytest.mark.asyncio
    async def test_no_proxy_or_error_returns_empty(self, bootstrapper):
        bootstrapper.ip_pool = Mock(get_proxy=Mock(return_value=None))
        assert await bootstrapper._get_available_proxy() == ('', None)
        bootstrapper.ip_pool = Mock(get_proxy=Mock(side_effect=RuntimeError('down')))
        assert await bootstrapper._get_available_proxy() == ('', None)


class TestCookieExtraction:
    @pytest.mark.asyncio
    async def test_extract_filters_empty_name_or_value(self, bootstrapper):
        page = make_mock_page([
            {'name': 'sessionid', 'value': 's1'},
            {'name': '', 'value': 'x'},
            {'name': 'ttwid', 'value': ''},
            {'name': 'msToken', 'value': 'm1'},
        ])
        assert await bootstrapper._extract_cookies(page) == {'sessionid': 's1', 'msToken': 'm1'}

    @pytest.mark.asyncio
    async def test_missing_critical_cookies_does_not_block(self, bootstrapper):
        # 缺少前5个关键字段时仅告警，不阻断返回
        page = make_mock_page([{'name': 'other', 'value': 'v'}])
        browser = AsyncMock()
        browser.__aenter__.return_value = browser
        browser.new_page.return_value = page
        with patch('src.pools.bootstrapper.AsyncCamoufox', return_value=browser) as mb, \
             patch('asyncio.sleep', new_callable=AsyncMock):
            cookies = await bootstrapper._perform_login('13800138000', 'pw', None)
        assert cookies == {'other': 'v'}
        assert mb.call_args.kwargs['headless'] is True


class TestPerformLogin:
    def _setup_browser(self, mock_camoufox, page):
        browser = AsyncMock()
        browser.__aenter__.return_value = browser
        browser.new_page.return_value = page
        mock_camoufox.return_value = browser

    @pytest.mark.asyncio
    async def test_login_call_sequence_and_proxy_config(self, bootstrapper):
        cookies = [{'name': 'sessionid', 'value': 's'}, {'name': 'ttwid', 'value': 't'}]
        page = make_mock_page(cookies)
        with patch('src.pools.bootstrapper.AsyncCamoufox') as mb, \
             patch('asyncio.sleep', new_callable=AsyncMock):
            self._setup_browser(mb, page)
            cfg = AutoLoginBootstrapper.LOGIN_CONFIG
            result = await bootstrapper._perform_login(
                '13800138000', 'pw', 'http://u:p@h:8080')
        assert result == {'sessionid': 's', 'ttwid': 't'}
        # 验证调用序列: goto -> click 登录 -> click 手机登录 -> fill 手机号 -> fill 密码 -> click 提交
        page.goto.assert_awaited_once_with(cfg['douyin_url'], wait_until='networkidle')
        page.click.assert_any_await(cfg['login_button_selector'])
        page.click.assert_any_await(cfg['phone_login_selector'])
        page.click.assert_any_await(cfg['submit_selector'])
        page.fill.assert_any_await(cfg['phone_input_selector'], '13800138000')
        page.fill.assert_any_await(cfg['password_input_selector'], 'pw')
        page.wait_for_url.assert_awaited_once()
        page.close.assert_awaited_once()
        # 代理透传给浏览器配置
        assert mb.call_args.kwargs['proxy'] == {'server': 'http://u:p@h:8080'}
        assert mb.call_args.kwargs['headless'] is True

    @pytest.mark.asyncio
    async def test_wait_for_url_timeout_still_extracts(self, bootstrapper):
        page = make_mock_page([{'name': 'sessionid', 'value': 's'}])
        page.wait_for_url.side_effect = Exception('timeout')
        with patch('src.pools.bootstrapper.AsyncCamoufox') as mb, \
             patch('asyncio.sleep', new_callable=AsyncMock):
            self._setup_browser(mb, page)
            assert await bootstrapper._perform_login('13800138000', 'pw', None) == {'sessionid': 's'}

    @pytest.mark.asyncio
    async def test_no_cookies_returns_none(self, bootstrapper):
        page = make_mock_page([])
        with patch('src.pools.bootstrapper.AsyncCamoufox') as mb, \
             patch('asyncio.sleep', new_callable=AsyncMock):
            self._setup_browser(mb, page)
            assert await bootstrapper._perform_login('13800138000', 'pw', None) is None

    @pytest.mark.asyncio
    async def test_browser_error_returns_none_and_closes_page(self, bootstrapper):
        page = make_mock_page([])
        page.goto.side_effect = RuntimeError('crash')
        with patch('src.pools.bootstrapper.AsyncCamoufox') as mb, \
             patch('asyncio.sleep', new_callable=AsyncMock):
            self._setup_browser(mb, page)
            assert await bootstrapper._perform_login('13800138000', 'pw', None) is None
        page.close.assert_awaited_once()


class TestImportToPool:
    @pytest.mark.asyncio
    async def test_import_with_explicit_proxy(self, bootstrapper, mock_redis):
        bootstrapper.ip_pool = Mock()
        cookies = {'sessionid': 's', 'ttwid': 't'}
        account_id = await bootstrapper.import_to_pool('13800138000', cookies, 'proxy_x')
        account = bootstrapper.cookie_pool._load_account(account_id)
        assert account.username == '13800138000'
        assert account.proxy_id == 'proxy_x'
        assert account.cookies == cookies
        bootstrapper.ip_pool.get_proxy.assert_not_called()  # 已指定代理则不再自动分配

    @pytest.mark.asyncio
    async def test_import_auto_assigns_proxy(self, bootstrapper):
        node = ProxyNode(proxy_id='proxy_auto', proxy_url='http://u:p@h:8080')
        bootstrapper.ip_pool = Mock(get_proxy=Mock(return_value=node))
        account_id = await bootstrapper.import_to_pool('13800138001', {'sessionid': 's'}, '')
        assert bootstrapper.cookie_pool._load_account(account_id).proxy_id == 'proxy_auto'

    @pytest.mark.asyncio
    async def test_import_without_proxy(self, bootstrapper):
        bootstrapper.ip_pool = Mock(get_proxy=Mock(return_value=None))
        account_id = await bootstrapper.import_to_pool('13800138002', {'sessionid': 's'}, '')
        assert bootstrapper.cookie_pool._load_account(account_id).proxy_id == ''


class TestLoginFlowAndDelay:
    @pytest.mark.asyncio
    async def test_post_login_delay_in_range_and_order(self, bootstrapper):
        delays = []

        async def fake_sleep(d):
            delays.append(d)

        with patch.object(bootstrapper, '_perform_login',
                          new=AsyncMock(return_value={'sessionid': 's', 'ttwid': 't'})), \
             patch.object(bootstrapper, '_save_cookies_backup', new=AsyncMock()) as m_backup, \
             patch.object(bootstrapper, 'import_to_pool', new=AsyncMock(return_value='acc_1')) as m_import, \
             patch('asyncio.sleep', side_effect=fake_sleep):
            result = await bootstrapper._login_single_account(
                {'phone': '13800138000', 'password': 'pw'})
        assert result == 'acc_1'
        # 延迟随机值落在 LOGIN_CONFIG 配置的区间内
        lo, hi = AutoLoginBootstrapper.LOGIN_CONFIG['post_login_delay']
        assert len(delays) == 1 and lo <= delays[0] <= hi
        # 备份先于导入池，且导入时绑定空代理（ip_pool 无可用代理）
        m_backup.assert_awaited_once()
        assert m_import.await_args.args[0] == '13800138000'
        assert m_import.await_args.args[2] == ''

    @pytest.mark.asyncio
    async def test_batch_login_partial_failure(self, bootstrapper):
        ok = AsyncMock(return_value='acc_ok')
        bad = AsyncMock(side_effect=RuntimeError('boom'))
        with patch.object(bootstrapper, '_login_single_account',
                          new=AsyncMock(side_effect=['acc_a', bad.side_effect])):
            results = await bootstrapper.batch_login(
                [{'phone': 'a'}, {'phone': 'b'}], max_concurrent=2)
        assert results == {'a': 'acc_a'}

    @pytest.mark.asyncio
    async def test_batch_login_all_success(self, bootstrapper):
        with patch.object(bootstrapper, '_login_single_account',
                          new=AsyncMock(side_effect=['id1', 'id2'])):
            results = await bootstrapper.batch_login(
                [{'phone': 'p1', 'password': 'x'}, {'phone': 'p2', 'password': 'y'}])
        assert results == {'p1': 'id1', 'p2': 'id2'}


class TestVerifyAndBackup:
    @pytest.mark.asyncio
    async def test_verify_login_status(self, bootstrapper):
        acc_id = await bootstrapper.import_to_pool(
            '13800138000', {'sessionid': 's', 'ttwid': 't'}, 'p')
        assert await bootstrapper.verify_login_status(acc_id) is True
        # 缺少 sessionid 则无效
        bad_id = await bootstrapper.import_to_pool(
            '13800138001', {'ttwid': 't'}, 'p')
        assert await bootstrapper.verify_login_status(bad_id) is False
        # 账号不存在
        assert await bootstrapper.verify_login_status('acc_missing') is False

    @pytest.mark.asyncio
    async def test_save_cookies_backup(self, bootstrapper):
        cookies = {'sessionid': 's', 'ttwid': 't'}
        await bootstrapper._save_cookies_backup('13800138000', cookies)
        files = list(bootstrapper.save_cookies_dir.glob('13800138000_*.json'))
        assert len(files) == 1
        data = json.loads(files[0].read_text(encoding='utf-8'))
        assert data['phone'] == '13800138000'
        assert data['cookies'] == cookies
        assert data['cookie_count'] == 2
