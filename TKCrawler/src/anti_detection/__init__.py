"""Anti-detection module for bypassing TikTok's WAF and bot detection.

本文件除导出各子模块符号外, 还定义了任务卡契约要求的核心编排类:
    - AntiDetectionClient  (request / switch_channel, WAF 时自动 curl -> browser 降级)
    - SessionManager       (在 session_manager.SessionManager 基础上补充契约要求的
                            get_session / refresh_cookie 方法; 由于写集白名单限制,
                            子类定义于此而不是修改 session_manager.py)
"""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Dict, Any

from .curl_client import CurlCffiClient
from .browser_client import CamoufoxClient
from .session_manager import SessionManager as _SessionManagerBase
from .rate_limiter import rate_limit, RateLimiter

from ..common.config import config
from ..common.logger import get_logger
from ..common.exceptions import WAFBlockedException

logger = get_logger(__name__)


class SessionManager(_SessionManagerBase):
    """任务卡契约兼容的 SessionManager.

    在原有 Cookie 存取/is_alive 能力之上, 补充契约签名:
        get_session()     -> 返回带登录态 Cookie 的 curl_cffi Session
        refresh_cookie()  -> 重新登录并刷新 Cookie, 返回是否成功
    """

    def get_session(self):
        """返回携带当前登录态 Cookie 的 curl_cffi Session."""
        cookies = self.get_cookies()
        client = CurlCffiClient()
        client.session.cookies.update(cookies)
        return client.session

    def refresh_cookie(self) -> bool:
        """重新登录刷新 Cookie (复用基类 refresh_session 逻辑)."""
        return self.refresh_session()


class BrowserResponse:
    """浏览器通道响应适配器, 对齐 curl_cffi Response 的常用接口."""

    def __init__(self, status_code: int, text: str, url: str = ''):
        self.status_code = status_code
        self.text = text
        self.url = url
        self.content = text.encode('utf-8')

    def json(self) -> Any:
        return json.loads(self.text)

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def __repr__(self) -> str:
        return f"<BrowserResponse [{self.status_code}] url={self.url!r}>"


def _run_async(coro):
    """在同步上下文中执行协程; 若当前线程已有事件循环则在子线程运行."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result()


class AntiDetectionClient:
    """统一请求入口: curl_cffi 主通道 + camoufox 浏览器备用通道.

    遇到 WAF 拦截时自动从 curl 通道降级到 browser 通道重试一次.
    """

    CHANNEL_CURL = 'curl'
    CHANNEL_BROWSER = 'browser'
    _VALID_CHANNELS = (CHANNEL_CURL, CHANNEL_BROWSER)

    def __init__(self, channel: Optional[str] = None):
        """Initialize anti-detection client.

        Args:
            channel: 初始通道 ('curl' | 'browser'); 默认取 config.PRIMARY_CHANNEL
        """
        if channel is None:
            channel = (
                self.CHANNEL_CURL
                if config.PRIMARY_CHANNEL == 'curl_cffi'
                else self.CHANNEL_BROWSER
            )
        if channel not in self._VALID_CHANNELS:
            raise ValueError(
                f"Invalid channel {channel!r}, must be one of {self._VALID_CHANNELS}"
            )

        self._curl = CurlCffiClient()
        self._browser: Optional[CamoufoxClient] = None
        self._channel = channel
        logger.info(f"AntiDetectionClient initialized with channel={self._channel}")

    @property
    def channel(self) -> str:
        """当前请求通道."""
        return self._channel

    def switch_channel(self, channel: str):
        """切换请求通道 ('curl' | 'browser')."""
        if channel not in self._VALID_CHANNELS:
            raise ValueError(
                f"Invalid channel {channel!r}, must be one of {self._VALID_CHANNELS}"
            )
        self._channel = channel
        logger.info(f"Switched request channel to {channel}")

    def request(self, url: str, method: str = 'GET',
                headers: Optional[Dict[str, str]] = None, **kwargs):
        """发起请求, 返回 curl_cffi Response 或 BrowserResponse.

        Raises:
            WAFBlockedException: curl 与 browser 通道均被 WAF 拦截时
            RateLimitException: 请求触发限流
        """
        if self._channel == self.CHANNEL_BROWSER:
            return self._request_via_browser(url, method=method, headers=headers, **kwargs)

        try:
            return self._curl.request(url, method=method, headers=headers, **kwargs)
        except WAFBlockedException:
            logger.warning(f"WAF block on curl channel for {url}, "
                           "switching to browser channel and retrying once")
            self.switch_channel(self.CHANNEL_BROWSER)
            return self._request_via_browser(url, method=method, headers=headers, **kwargs)

    def _request_via_browser(self, url: str, method: str = 'GET',
                             headers: Optional[Dict[str, str]] = None, **kwargs) -> BrowserResponse:
        """通过浏览器通道请求 (复用登录态 Cookie)."""
        if self._browser is None:
            self._browser = CamoufoxClient()

        data = _run_async(self._browser.fetch(url, method=method, headers=headers, **kwargs))
        return BrowserResponse(
            status_code=data['status_code'],
            text=data['text'],
            url=data['url'],
        )

    def close(self):
        """释放底层资源."""
        if self._browser is not None:
            try:
                _run_async(self._browser.close())
            except Exception as e:
                logger.debug(f"Browser client close error: {e}")
            self._browser = None
        self._curl.close()
        logger.info("AntiDetectionClient closed")


__all__ = [
    'AntiDetectionClient',
    'BrowserResponse',
    'CurlCffiClient',
    'CamoufoxClient',
    'SessionManager',
    'rate_limit',
    'RateLimiter',
]
