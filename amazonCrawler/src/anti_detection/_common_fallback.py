"""Common-layer fallback stubs for Agent B (anti_detection/pools).

仅当 src.common.exceptions / src.common.logger 尚未由 Agent A 实现时作为
import fallback 使用；Agent A 版本就绪后各模块优先 import src.common。
"""

import sys

try:
    from loguru import logger as _loguru_logger

    _HAS_LOGURU = True
except ImportError:  # pragma: no cover
    import logging as _logging

    _loguru_logger = _logging.getLogger("amazonCrawler")
    _HAS_LOGURU = False


def get_logger(name: str):
    if _HAS_LOGURU:
        return _loguru_logger.bind(module=name)
    return _loguru_logger.getChild(name)


class CrawlerException(Exception):
    """Base exception for all crawler errors."""


class BotBlockedException(CrawlerException):
    """Raised when Amazon returns 503 / captcha / robot-check page."""

    def __init__(self, url: str, status_code: int = None):
        self.url = url
        self.status_code = status_code
        super().__init__(
            f"Bot challenge blocked request to {url} (status: {status_code})"
        )


class RateLimitException(CrawlerException):
    """Raised when rate limit is exceeded."""

    def __init__(self, message: str = "Rate limit exceeded"):
        super().__init__(message)


class SessionExpiredException(CrawlerException):
    """Raised when session/cookie expires."""

    def __init__(self, message: str = "Session expired, need to re-login"):
        super().__init__(message)


class ParsingException(CrawlerException):
    """Raised when parsing HTML/JSON fails."""

    def __init__(self, field: str, reason: str = ""):
        self.field = field
        self.reason = reason
        super().__init__(f"Failed to parse field '{field}': {reason}")


class QuotaExceededException(CrawlerException):
    """Raised when daily quota is exceeded."""

    def __init__(self, resource_type: str, limit: int):
        self.resource_type = resource_type
        self.limit = limit
        super().__init__(
            f"Daily quota exceeded for {resource_type} (limit: {limit})"
        )
