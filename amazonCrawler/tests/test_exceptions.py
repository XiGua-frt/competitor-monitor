"""src/common/exceptions.py 异常体系单测。"""

import pytest

from src.common.exceptions import (
    BotBlockedException,
    CrawlerException,
    ParsingException,
    QuotaExceededException,
    RateLimitException,
    SessionExpiredException,
)

ALL_SUBCLASSES = [
    BotBlockedException,
    RateLimitException,
    SessionExpiredException,
    ParsingException,
    QuotaExceededException,
]


class TestHierarchy:
    """继承关系：全部子类可被 except CrawlerException 捕获。"""

    @pytest.mark.parametrize("exc_cls", ALL_SUBCLASSES)
    def test_subclass_of_base(self, exc_cls):
        assert issubclass(exc_cls, CrawlerException)
        assert issubclass(CrawlerException, Exception)

    @pytest.mark.parametrize("exc_cls", ALL_SUBCLASSES)
    def test_catchable_as_base(self, exc_cls):
        with pytest.raises(CrawlerException):
            try:
                raise exc_cls()
            except TypeError:
                # ParsingException/QuotaExceededException 有必填参数，用关键字补齐
                if exc_cls is ParsingException:
                    raise ParsingException("title", "empty")
                if exc_cls is QuotaExceededException:
                    raise QuotaExceededException("asins", 200)
                raise


class TestAttributes:
    """异常携带结构化属性（供上层日志/监控使用）。"""

    def test_bot_blocked_positional(self):
        e = BotBlockedException("https://www.amazon.com/dp/B0XXX", 503)
        assert e.url == "https://www.amazon.com/dp/B0XXX"
        assert e.status_code == 503
        assert "B0XXX" in str(e)

    def test_bot_blocked_message_only(self):
        e = BotBlockedException(message="soft cooldown hit")
        assert e.url == ""
        assert "soft cooldown" in str(e)

    def test_parsing_exception(self):
        e = ParsingException("price", "apex_desktop missing")
        assert e.field == "price"
        assert e.reason == "apex_desktop missing"
        assert "price" in str(e)

    def test_quota_exception(self):
        e = QuotaExceededException("asins", 200)
        assert e.resource_type == "asins"
        assert e.limit == 200
        assert "200" in str(e)

    def test_rate_limit_and_session_defaults(self):
        assert "Rate limit" in str(RateLimitException())
        assert "Session expired" in str(SessionExpiredException())
