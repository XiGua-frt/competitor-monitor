"""Anti-detection layer: chrome131 client + rate limiter + session manager (Agent B)。"""

from .curl_client import CurlCffiClient, looks_like_bot_challenge
from .rate_limiter import RateLimiter
from .session_manager import SessionManager

# 异常符号透传（无论来自 src.common 还是 fallback），调用方统一从此 import
try:
    from src.common.exceptions import (
        BotBlockedException,
        CrawlerException,
        QuotaExceededException,
        RateLimitException,
    )
except ImportError:
    from ._common_fallback import (
        BotBlockedException,
        CrawlerException,
        QuotaExceededException,
        RateLimitException,
    )

__all__ = [
    'CurlCffiClient',
    'looks_like_bot_challenge',
    'RateLimiter',
    'SessionManager',
    'BotBlockedException',
    'CrawlerException',
    'QuotaExceededException',
    'RateLimitException',
]
