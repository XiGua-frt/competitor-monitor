"""Camoufox anti-detection browser client for login state management.

登录状态机:
    NAVIGATE -> FORM_READY -> (自动填表 | 半自动人工) -> SUBMITTED/CAPTCHA/MANUAL_WAIT
    -> SUCCESS / FAILED

凭据来源: 环境变量 TIKTOK_USERNAME / TIKTOK_PASSWORD (经 src/common/config.py 读取)。
严禁硬编码任何账号密码。
"""

import asyncio
from typing import Optional, Dict, Any
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from ..common.config import config
from ..common.logger import get_logger
from ..common.exceptions import SessionExpiredException

logger = get_logger(__name__)

# 登录成功判定: TikTok 登录态 Cookie 名
LOGIN_COOKIE_NAMES = ('sessionid', 'sessionid_ss')

LOGIN_URL = 'https://www.tiktok.com/login'

# 登录状态机状态
STATE_NAVIGATE = 'navigate'
STATE_FORM_READY = 'form_ready'
STATE_SUBMITTED = 'submitted'
STATE_CAPTCHA = 'captcha'
STATE_MANUAL_WAIT = 'manual_wait'
STATE_SUCCESS = 'success'
STATE_FAILED = 'failed'


class CamoufoxClient:
    """Camoufox anti-detection browser wrapper for obtaining login state."""

    def __init__(self, headless: Optional[bool] = None):
        """Initialize camoufox browser client.

        Args:
            headless: 是否无头运行; None 时使用 config.BROWSER_HEADLESS
        """
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._playwright = None
        self._camoufox_ctx = None
        self._headless: bool = config.BROWSER_HEADLESS if headless is None else headless

        # 半自动模式轮询参数 (测试时可调小)
        self.login_timeout: float = 300.0   # 等待人工完成验证码的总超时(秒)
        self.poll_interval: float = 2.0     # 登录成功轮询间隔(秒)

    # ------------------------------------------------------------------
    # 浏览器生命周期
    # ------------------------------------------------------------------
    async def start(self):
        """Start browser instance (camoufox 优先, 不可用时回退 playwright firefox)."""
        logger.info(f"Starting browser (headless={self._headless})...")

        try:
            await self._start_camoufox()
        except ImportError:
            logger.warning("camoufox not installed, falling back to playwright firefox")
            await self._start_playwright()
        except Exception as e:
            # 内核包已装但浏览器内核缺失(如 CamoufoxNotInstalled)等启动期异常,
            # 同样回退到 playwright firefox (2026-10-08 实机踩坑: GitHub 403 拉不到内核)
            # 注: 沙箱删除代理可能抛 BaseException 级异常, 这里用 except BaseException
            # 兜底但放行 KeyboardInterrupt/SystemExit
            logger.warning(f"camoufox failed to start ({type(e).__name__}: {e}), "
                           f"falling back to playwright firefox")
            await self._start_playwright()
        except BaseException as e:  # noqa: BLE001 — 沙箱 broker 异常非 Exception 子类
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            logger.warning(f"camoufox aborted ({type(e).__name__}), "
                           f"falling back to playwright firefox")
            await self._start_playwright()

        self.page = await self.context.new_page()
        logger.info("Browser started successfully")

    async def _start_camoufox(self):
        """通过 camoufox 反检测内核启动 (Playwright API)."""
        from camoufox.async_api import AsyncCamoufox

        # 预检: 内核未下载时直接抛 ImportError 走 playwright 回退,
        # 避免 AsyncCamoufox.__aenter__ 中途(UBO下载/缓存清理)抛出
        # 非 Exception 级异常导致整个进程死亡 (2026-10-08 实机踩坑)
        try:
            from camoufox.pkgman import installed_verstr
            installed_verstr()
        except Exception as e:
            raise ImportError(
                f"camoufox kernel not available ({type(e).__name__}); "
                f"run `python -m camoufox fetch` or use playwright fallback"
            ) from e

        kwargs: Dict[str, Any] = {'headless': self._headless}
        if config.PROXY_ENABLED:
            kwargs['proxy'] = {'server': config.PROXY_URL}

        self._camoufox_ctx = AsyncCamoufox(**kwargs)
        self.browser = await self._camoufox_ctx.__aenter__()
        self.context = await self.browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            locale='en-US',
            timezone_id=config.TIMEZONE,
        )

    async def _start_playwright(self):
        """playwright firefox 回退通道."""
        self._playwright = await async_playwright().start()

        launch_kwargs: Dict[str, Any] = {
            'headless': self._headless,
            'executable_path': config.BROWSER_EXECUTABLE_PATH or None,
        }
        if config.PROXY_ENABLED:
            # Firefox 代理必须在启动时设置, 不能在 context 上设置
            launch_kwargs['proxy'] = {'server': config.PROXY_URL}

        self.browser = await self._playwright.firefox.launch(**launch_kwargs)
        self.context = await self.browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            locale='en-US',
            timezone_id=config.TIMEZONE,
        )

    async def close(self):
        """Close browser and cleanup."""
        if self._camoufox_ctx:
            try:
                await self._camoufox_ctx.__aexit__(None, None, None)
            except Exception as e:
                logger.debug(f"camoufox context exit error: {e}")
            self._camoufox_ctx = None
        if self.page:
            await self.page.close()
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self._playwright:
            await self._playwright.stop()

        self.page = None
        self.context = None
        self.browser = None
        self._playwright = None
        logger.info("Browser closed")

    # ------------------------------------------------------------------
    # TikTok 登录流程 (状态机)
    # ------------------------------------------------------------------
    async def login_tiktok(self, save_to_session: bool = True) -> Dict[str, str]:
        """Perform TikTok login and extract cookies.

        流程:
            1. 优先自动登录 (环境变量凭据自动填表);
            2. 触发验证码/自动登录失败时, 降级为半自动模式
               (弹出有界面浏览器等待人工完成, 轮询检测登录成功);
            3. 登录成功后提取 Cookie, 交由 SessionManager 持久化.

        Args:
            save_to_session: 是否将 Cookie 交给 SessionManager 持久化

        Returns:
            Dictionary of cookies

        Raises:
            SessionExpiredException: When login fails
        """
        if not self.page:
            await self.start()

        logger.info("Navigating to TikTok login page...")

        try:
            await self.page.goto(LOGIN_URL, timeout=30000)
            await self.page.wait_for_load_state('domcontentloaded')
        except Exception as e:
            logger.error(f"Failed to open TikTok login page: {e}")
            raise SessionExpiredException(f"Failed to open TikTok login page: {e}")

        state = STATE_FORM_READY
        username = config.TIKTOK_USERNAME
        password = config.TIKTOK_PASSWORD

        if username and password:
            logger.info("Credentials found in environment, attempting automatic login")
            await self._switch_to_password_login()
            await self._fill_login_form(username, password)

            if await self._detect_captcha():
                state = STATE_CAPTCHA
                logger.warning("Captcha detected during automatic login")
            else:
                state = STATE_SUBMITTED
        else:
            logger.info(
                "No TIKTOK_USERNAME/TIKTOK_PASSWORD in environment, "
                "entering semi-automatic mode directly"
            )
            state = STATE_MANUAL_WAIT

        if state == STATE_SUBMITTED:
            # 自动提交后轮询是否已登录 (可能有延迟弹验证码)
            if await self._wait_for_login():
                state = STATE_SUCCESS
            else:
                logger.warning("Automatic login did not complete, falling back to manual mode")
                state = STATE_MANUAL_WAIT

        if state in (STATE_CAPTCHA, STATE_MANUAL_WAIT):
            await self._enter_manual_mode()
            if await self._wait_for_login():
                state = STATE_SUCCESS
            else:
                state = STATE_FAILED

        if state != STATE_SUCCESS:
            raise SessionExpiredException(
                "TikTok login failed: login was not completed within the timeout"
            )

        logger.info("TikTok login succeeded")
        cookies = await self.get_cookies()

        if save_to_session:
            # 交给 SessionManager 持久化 (写 data/session.json)
            from .session_manager import session_manager
            session_manager.set_cookies(cookies)

        return cookies

    async def _switch_to_password_login(self):
        """从默认二维码登录切换到账号密码登录 tab."""
        candidates = [
            self.page.get_by_text('Use phone / email / username'),
            self.page.locator('[data-e2e="login-tab"]'),
        ]
        for tab in candidates:
            try:
                if await tab.count() > 0:
                    await tab.first.click(timeout=5000)
                    await self.page.wait_for_load_state('domcontentloaded')
                    return
            except Exception as e:
                logger.debug(f"Login tab candidate failed: {e}")
                continue
        # 某些页面变体直接展示表单
        logger.warning("Password login tab not found, trying to fill form directly")

    async def _fill_login_form(self, username: str, password: str):
        """填写登录表单并点击登录按钮."""
        username_input = self.page.locator('input[name="username"], input[type="text"]')
        await username_input.first.fill(username)

        password_input = self.page.locator('input[type="password"]')
        await password_input.first.fill(password)

        submit_button = self.page.locator('button[type="submit"]')
        await submit_button.first.click()
        logger.info("Login form submitted")

    async def _detect_captcha(self) -> bool:
        """检测页面是否出现验证码."""
        captcha = self.page.locator(
            'iframe[src*="captcha"], .captcha_verify_container, [class*="captcha_verify"]'
        )
        try:
            count = await captcha.count()
        except Exception as e:
            logger.debug(f"Captcha detection error: {e}")
            return False
        return count > 0

    async def _enter_manual_mode(self):
        """半自动模式: 弹出可见浏览器窗口, 等待人工完成登录."""
        logger.info(
            f"Semi-automatic mode: please complete login (captcha/2FA) in the opened "
            f"browser window, polling for success every {self.poll_interval:.0f}s "
            f"(timeout {self.login_timeout:.0f}s)"
        )
        if self._headless:
            logger.info("Running headless, relaunching browser with UI for manual login")
            await self._relaunch_headed()
        try:
            await self.page.bring_to_front()
        except Exception as e:
            logger.debug(f"bring_to_front failed: {e}")

    async def _relaunch_headed(self):
        """关闭无头浏览器并以有界面模式重启."""
        await self.close()
        self._headless = False
        await self.start()

    async def _wait_for_login(self) -> bool:
        """轮询检测登录是否成功 (以 sessionid Cookie 出现为准)."""
        attempts = max(1, int(self.login_timeout / self.poll_interval))
        for i in range(attempts):
            if await self._check_login_success():
                return True
            if i < attempts - 1:
                await asyncio.sleep(self.poll_interval)
        return False

    async def _check_login_success(self) -> bool:
        """通过登录态 Cookie 判断是否登录成功."""
        try:
            cookies = await self.context.cookies()
        except Exception as e:
            logger.debug(f"Cookie check error: {e}")
            return False
        return any(c.get('name') in LOGIN_COOKIE_NAMES for c in cookies)

    # ------------------------------------------------------------------
    # Cookie / 请求辅助
    # ------------------------------------------------------------------
    async def get_cookies(self) -> Dict[str, str]:
        """Get current cookies from browser context.

        Returns:
            Dictionary of cookies

        Raises:
            SessionExpiredException: When browser not started
        """
        if not self.context:
            raise SessionExpiredException("Browser not started")

        cookies = await self.context.cookies()
        return {cookie['name']: cookie['value'] for cookie in cookies}

    async def fetch(self, url: str, method: str = 'GET', headers: Optional[Dict[str, str]] = None,
                    **kwargs) -> Dict[str, Any]:
        """在浏览器上下文中请求 URL (复用登录态 Cookie), 供 browser 通道使用.

        Returns:
            {'status_code': int, 'text': str, 'url': str}
        """
        if not self.page:
            await self.start()

        response = await self.page.request.fetch(
            url, method=method, headers=headers or {}, **kwargs
        )
        return {
            'status_code': response.status,
            'text': await response.text(),
            'url': response.url,
        }


# Synchronous wrapper for easier integration
def get_tiktok_cookies(save_to_session: bool = True) -> Dict[str, str]:
    """Synchronous wrapper to get TikTok cookies.

    Returns:
        Dictionary of cookies
    """
    async def _get_cookies():
        client = CamoufoxClient()
        try:
            await client.start()
            cookies = await client.login_tiktok(save_to_session=save_to_session)
            return cookies
        finally:
            await client.close()

    return asyncio.run(_get_cookies())
