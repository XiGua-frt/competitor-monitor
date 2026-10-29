"""curl_cffi client with chrome131 TLS fingerprint for Amazon requests.

参考 TKCrawler/src/anti_detection/curl_client.py 的四强特征设计，按 CONTRACT.md
适配 Amazon 的挑战页特征：
  - impersonate='chrome131'（curl_cffi 0.16.3 支持）
  - request() 方法 + looks_like_bot_challenge() 正文启发式
  - 强特征只认 <title> 短语 / 邮箱指纹 / captcha-image / aws-waf-token script，
    绝不做裸词 "captcha" 匹配——正常 Amazon 页的资源清单里就有 captcha 相关
    资源名（如 /aapi/…captcha…、Captcha 前端 JS），裸词匹配会把 200 正常页误判。

curl_cffi>=0.16 的异常导入路径是 curl_cffi.requests.exceptions（顶层不再导出）。
src.common.exceptions 不存在时用本地 fallback 定义，保证模块可独立 import。
"""

import re
from typing import Any, Dict, Optional

from curl_cffi import requests
from curl_cffi.requests import Response
from curl_cffi.requests.exceptions import RequestException

# ---- src.common 依赖：存在则直接用（Agent A 版本），否则本地 fallback ----
try:  # pragma: no cover - 取决于 Agent A 是否已就绪
    from src.common.exceptions import (
        BotBlockedException,
        CrawlerException,
        RateLimitException,
    )
    from src.common.logger import get_logger

    _COMMON_READY = True
except ImportError:
    from . import _common_fallback as _fb  # type: ignore

    BotBlockedException = _fb.BotBlockedException
    CrawlerException = _fb.CrawlerException
    RateLimitException = _fb.RateLimitException
    get_logger = _fb.get_logger
    _COMMON_READY = False

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Amazon bot challenge 正文启发式（只认强特征，绝不做裸词匹配）。
#
# 强特征（任一命中即判挑战页）：
#   1. <title> 含 Amazon 拦截短语：Robot Check / Sorry! Something went wrong /
#      Access Denied / captcha（<title> 是挑战页独有位置，正文资源名不受影响）
#   2. 正文含 api-services-support@amazon.com（Amazon 503/Robot Check 页的
#      联系邮箱，正常页不出现）
#   3. 实际元素特征：captcha-image 元素（id/class），或加载 aws-waf-token
#      的 script 标签（AWS WAF Bot Control 挑战）
#
# 反例（不误报）：正常商品页正文出现 "captcha" 资源名（JS/图片 URL），
# 因为不在 <title>，也不是 captcha-image 元素或 aws-waf-token script。
# ---------------------------------------------------------------------------

_CHALLENGE_TITLE_MARKERS = (
    'sorry! something went wrong',
    'robot check',
    'access denied',
    'captcha',
)

# Amazon challenge/503 页独有联系邮箱
_AMAZON_SUPPORT_EMAIL = 'api-services-support@amazon.com'

# captcha-image 实际元素（id/class），不是正文文本里的资源文件名
_CAPTCHA_IMAGE_RE = re.compile(r'<[^>]*(?:id|class)=["\'][^"\']*captcha-image', re.I)

# AWS WAF token script 标签（挑战页独有）
_AWS_WAF_TOKEN_RE = re.compile(
    r'<script[^>]+(?:src=["\'][^"\']*aws-waf-token|[^>]*aws-waf-token)', re.I
)

_TITLE_RE = re.compile(r'<title[^>]*>(.*?)</title>', re.S | re.I)


def looks_like_bot_challenge(text: str) -> bool:
    """判定响应正文是否为 Amazon bot/challenge 页（只认强特征，避免资源文件名误报）。

    Args:
        text: 响应正文 HTML

    Returns:
        True 表示命中挑战页强特征
    """
    if not text:
        return False

    # 强特征 1: <title> 挑战/拦截短语
    m = _TITLE_RE.search(text)
    if m:
        title = m.group(1).lower()
        if any(marker in title for marker in _CHALLENGE_TITLE_MARKERS):
            return True

    # 强特征 2: Amazon 挑战页联系邮箱
    if _AMAZON_SUPPORT_EMAIL in text:
        return True

    # 强特征 3: captcha-image 实际元素 / aws-waf-token script 标签
    if _CAPTCHA_IMAGE_RE.search(text) or _AWS_WAF_TOKEN_RE.search(text):
        return True

    return False


class CurlCffiClient:
    """curl_cffi wrapper with chrome131 fingerprint.

    request() 只负责 URL/headers 构造与挑战判定，网络路径由 curl_cffi session
    承担；单测不测网络路径。
    """

    DEFAULT_IMPERSONATE = 'chrome131'

    # 视为 bot 拦截的 HTTP 状态码（由 request 内部判定，与正文启发式互补）
    BLOCK_STATUS_CODES = (403, 418, 503)

    def __init__(self, impersonate: str = None, proxies: Optional[Dict[str, str]] = None):
        """Initialize curl_cffi client.

        Args:
            impersonate: Browser to impersonate (default: chrome131)
            proxies: Optional proxies dict, e.g. {'http': url, 'https': url}
        """
        self.impersonate = impersonate or self.DEFAULT_IMPERSONATE
        self.session = requests.Session(impersonate=self.impersonate)
        if proxies:
            self.session.proxies = proxies

    def request(
        self,
        url: str,
        method: str = 'GET',
        headers: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> Response:
        """Make HTTP request with fingerprint simulation.

        Args:
            url: Target URL
            method: HTTP method
            headers: Custom headers (merged over chrome131 defaults)
            **kwargs: Additional requests parameters (timeout, cookies, ...)

        Returns:
            Response object

        Raises:
            BotBlockedException: 503/403 状态码或正文命中挑战页强特征
            RateLimitException: 429 状态码
        """
        # 默认 headers：chrome131 导航请求基线，调用方可按需覆盖
        default_headers = {
            'User-Agent': self._get_user_agent(),
            'Accept': (
                'text/html,application/xhtml+xml,application/xml;q=0.9,'
                'image/avif,image/webp,*/*;q=0.8'
            ),
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'sec-ch-ua': (
                '"Google Chrome";v="131", "Chromium";v="131", '
                '"Not_A Brand";v="24"'
            ),
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"macOS"',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
        }
        if headers:
            default_headers.update(headers)

        timeout = kwargs.pop('timeout', 30)
        response = self.session.request(
            method=method,
            url=url,
            headers=default_headers,
            timeout=timeout,
            **kwargs,
        )

        # 状态码层判定
        if response.status_code == 429:
            logger.warning(f"Rate limited: {url}")
            raise RateLimitException(f"Rate limit exceeded for {url}")

        if response.status_code in self.BLOCK_STATUS_CODES:
            logger.warning(f"Potential bot block: {response.status_code} for {url}")
            raise BotBlockedException(url, response.status_code)

        # 正文强特征启发式（部分挑战以 200 返回）
        if self._body_matches_challenge(response):
            logger.warning(
                f"Bot challenge page detected (status {response.status_code}) for {url}"
            )
            raise BotBlockedException(url, response.status_code)

        logger.debug(f"Request success: {method} {url} -> {response.status_code}")
        return response

    @staticmethod
    def _body_matches_challenge(response: Response) -> bool:
        """对响应正文跑强特征启发式；正文不可读时静默放行（零误伤原则）。"""
        try:
            text = response.text
        except Exception as e:
            logger.debug(f"Bot challenge heuristic skipped (unreadable body): {e}")
            return False
        return looks_like_bot_challenge(text)

    def get(self, url: str, **kwargs: Any) -> Response:
        """GET request."""
        return self.request(url, method='GET', **kwargs)

    def post(self, url: str, **kwargs: Any) -> Response:
        """POST request."""
        return self.request(url, method='POST', **kwargs)

    def _get_user_agent(self) -> str:
        """Get User-Agent for chrome131."""
        return (
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/131.0.0.0 Safari/537.36'
        )

    def close(self) -> None:
        """Close session."""
        self.session.close()
