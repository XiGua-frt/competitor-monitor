"""curl_cffi client for TLS/JA3/JA4 fingerprint simulation."""

import re
import time
from typing import Optional, Dict, Any

from curl_cffi import requests
from curl_cffi.requests import Response
from curl_cffi.requests.exceptions import RequestException  # curl_cffi>=0.16 不再顶层导出

from ..common.config import config
from ..common.logger import get_logger
from ..common.exceptions import WAFBlockedException, RateLimitException

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# WAF 挑战页正文启发式（只认强特征，绝不做裸词匹配）。
#
# Agent O 实机探针（docs/probe/probe-findings-2026-10-07.md）证实：TikTok 正常
# 页面的资源清单里就含 captcha-*.js 等文件名（单页 20+ 处 "captcha" 字样），
# 对响应正文裸词匹配 "captcha" 会把 200 正常页误判为 WAF 拦截页，进而触发
# 纪律性停机。因此正文判定只接受以下强特征之一：
#   1. <title> 含挑战/拦截短语（裸词 verify/denied 不算，tag 页标题可能含）
#   2. Cloudflare challenge 全局脚本 window._cf_chl
#   3. 实际加载 reCAPTCHA 的 <script src=".../recaptcha/api.js"> 标签
#      （只匹配 script src，不匹配正文文本里的资源文件名）
#   4. 响应体积异常小（<2KB）且含挑战关键词与表单元素（TikTok 真实页 ~400KB）
# ---------------------------------------------------------------------------

_WAF_TITLE_MARKERS = (
    'captcha', 'human verification', 'verify to continue', 'verify you are',
    'are you a robot', 'access denied', 'attention required', 'just a moment',
    'security check', 'request blocked',
)

# Cloudflare challenge 全局对象（出现在挑战页 <script> 内）
_CF_CHALLENGE_MARKER = 'window._cf_chl'

# reCAPTCHA 实际加载标签（<script src=".../recaptcha/api.js">），不是正文文本
_RECAPTCHA_SCRIPT_RE = re.compile(
    r'<script[^>]+src=["\'][^"\']*recaptcha/(?:api|enterprise)\.js', re.I)

# 挑战小页判定阈值与关键词
_SMALL_BODY_BYTES = 2 * 1024
_SMALL_BODY_KEYWORDS = ('captcha', 'challenge', 'verify', 'denied', 'blocked')
_FORM_MARKERS = ('<form', '<input', '<button', 'action=')

_TITLE_RE = re.compile(r'<title[^>]*>(.*?)</title>', re.S | re.I)


def looks_like_waf_challenge(text: str) -> bool:
    """判定响应正文是否为 WAF/反爬挑战页（只认强特征，避免资源文件名误报）。

    已用 Agent O 真实证据验证：
    - docs/probe/20261007T144634Z_tag_webdev_try1_body.html（200 正常页，正文
      含 20+ 处 captcha-*.js 资源名）必须判为 False；
    - 最小 Cloudflare 挑战页 fixture 判为 True。
    """
    if not text:
        return False

    # 强特征 1: <title> 挑战/拦截短语
    m = _TITLE_RE.search(text)
    if m:
        title = m.group(1).lower()
        if any(marker in title for marker in _WAF_TITLE_MARKERS):
            return True

    # 强特征 2: Cloudflare challenge 脚本
    if _CF_CHALLENGE_MARKER in text:
        return True

    # 强特征 3: 实际加载 reCAPTCHA 的 script 标签
    if _RECAPTCHA_SCRIPT_RE.search(text):
        return True

    # 强特征 4: 异常小的响应体 + 挑战关键词 + 表单元素
    # （正常页面几百 KB；截断的 JSON 错误响应等无表单元素，不会误中）
    if len(text) < _SMALL_BODY_BYTES:
        low = text.lower()
        if (any(k in low for k in _SMALL_BODY_KEYWORDS)
                and any(f in low for f in _FORM_MARKERS)):
            return True

    return False


class CurlCffiClient:
    """curl_cffi wrapper with chrome131 fingerprint."""

    # 网络层瞬时错误重试参数（类属性便于测试 patch）
    TRANSIENT_RETRIES = 1
    TRANSIENT_RETRY_BACKOFF_S = 1.5

    def __init__(self, impersonate: str = None):
        """Initialize curl_cffi client.
        
        Args:
            impersonate: Browser to impersonate (default: chrome131)
        """
        self.impersonate = impersonate or config.CURL_CFFI_IMPERSONATE
        self.session = requests.Session(impersonate=self.impersonate)
        
        # Configure proxy if enabled
        if config.PROXY_ENABLED:
            self.session.proxies = {
                'http': config.PROXY_URL,
                'https': config.PROXY_URL
            }
        
        logger.info(f"CurlCffiClient initialized with impersonate={self.impersonate}")
    
    def request(
        self,
        url: str,
        method: str = 'GET',
        headers: Optional[Dict[str, str]] = None,
        **kwargs
    ) -> Response:
        """Make HTTP request with fingerprint simulation.
        
        Args:
            url: Target URL
            method: HTTP method
            headers: Custom headers
            **kwargs: Additional requests parameters
        
        Returns:
            Response object
        
        Raises:
            WAFBlockedException: When request is blocked by WAF
            RateLimitException: When rate limited
        """
        # Default headers
        # NOTE 2026-10-08: sec-ch-ua family + Sec-Fetch-* are required for
        # shop.tiktok.com (argus "Security Check" triggers when missing —
        # verified in docs/probe/shop_retest_2026-10-08.md). Defaults match a
        # chrome131 XHR/fetch call; page-navigation requests should override
        # Sec-Fetch-* per request.
        default_headers = {
            'User-Agent': self._get_user_agent(),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'sec-ch-ua': '"Chromium";v="131", "Google Chrome";v="131", "Not-A.Brand";v="99"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"macOS"',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
            'Sec-Fetch-User': '?1',
        }

        if headers:
            default_headers.update(headers)

        # 网络层瞬时错误重试（2026-10-09 实测：代理侧偶发 curl 35 SSL 重置）。
        # 只重试未收到响应的 RequestException（连接/SSL/超时）；WAF/429 是
        # 已收到响应后的策略性失败，绝不重试（合规约束：不硬怼风控）。
        max_attempts = 1 + self.TRANSIENT_RETRIES
        for attempt in range(1, max_attempts + 1):
            try:
                # 调用方可覆盖默认超时；不能同时写死 timeout=30 又转发 **kwargs，
                # 否则调用方传 timeout 会报 "multiple values for keyword argument"
                timeout = kwargs.pop('timeout', 30)
                response = self.session.request(
                    method=method,
                    url=url,
                    headers=default_headers,
                    timeout=timeout,
                    **kwargs
                )

                # Check for WAF/bot detection
                if response.status_code in [403, 418, 503]:
                    logger.warning(f"Potential WAF block: {response.status_code} for {url}")
                    raise WAFBlockedException(url, response.status_code)

                if response.status_code == 429:
                    logger.warning(f"Rate limited: {url}")
                    raise RateLimitException("Rate limit exceeded")

                # 正文强特征启发式：部分 WAF 以 200 返回挑战页（见模块头注释），
                # 只认 title 短语/CF challenge/recaptcha 标签/挑战小页，不做裸词匹配。
                if self._body_matches_waf_challenge(response):
                    logger.warning(
                        f"WAF challenge page detected (status {response.status_code}) "
                        f"for {url}"
                    )
                    raise WAFBlockedException(url, response.status_code)

                logger.debug(f"Request success: {method} {url} -> {response.status_code}")
                return response

            except RequestException as e:
                if attempt < max_attempts:
                    logger.warning(
                        f"Transient network error (attempt {attempt}/{max_attempts}), "
                        f"retrying in {self.TRANSIENT_RETRY_BACKOFF_S}s: {method} {url} - {e}"
                    )
                    time.sleep(self.TRANSIENT_RETRY_BACKOFF_S)
                    continue
                logger.error(f"Request failed: {method} {url} - {e}")
                raise

    @staticmethod
    def _body_matches_waf_challenge(response) -> bool:
        """对响应正文跑强特征启发式；正文不可读时静默放行（零误伤原则）。"""
        try:
            text = response.text
        except Exception as e:
            logger.debug(f"WAF body heuristic skipped (unreadable body): {e}")
            return False
        return looks_like_waf_challenge(text)
    
    def get(self, url: str, **kwargs) -> Response:
        """GET request."""
        return self.request(url, method='GET', **kwargs)
    
    def post(self, url: str, **kwargs) -> Response:
        """POST request."""
        return self.request(url, method='POST', **kwargs)
    
    def _get_user_agent(self) -> str:
        """Get User-Agent for chrome131."""
        # Chrome 131 User-Agent
        return (
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/131.0.0.0 Safari/537.36'
        )
    
    def close(self):
        """Close session."""
        self.session.close()
        logger.info("CurlCffiClient closed")
