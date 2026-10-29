"""Amazon 爬虫自定义异常体系（Agent A 正式实现）。

继承关系：
    CrawlerException                     基类，业务代码统一捕获入口
    ├── BotBlockedException              Amazon 503 / captcha / Access Denied 页
    ├── RateLimitException               触发限流（本地或对端）
    ├── SessionExpiredException          会话/cookie 失效
    ├── ParsingException(field, reason)  HTML/JSON 解析失败，定位到字段
    └── QuotaExceededException(rt, lim)  日配额用尽（Redis 或本地计数）

BotBlockedException 兼容两种调用风格：
    BotBlockedException("https://...", 503)          # 位置参数 url, status
    BotBlockedException(message="soft cooldown hit") # 纯消息
"""


class CrawlerException(Exception):
    """所有爬虫异常的基类。"""


class BotBlockedException(CrawlerException):
    """被 Amazon 反爬拦截（503 软冷却页 / Robot Check / Access Denied / captcha）。

    对应 TKCrawler 的 WAFBlockedException；scraper 层捕获后进入软冷却。
    """

    def __init__(self, url: str = "", status_code: int | None = None,
                 message: str | None = None):
        self.url = url
        self.status_code = status_code
        if message is None:
            message = f"Bot challenge blocked request to {url or 'N/A'} (status: {status_code})"
        super().__init__(message)


class RateLimitException(CrawlerException):
    """请求频率超限，需要退避后重试。"""

    def __init__(self, message: str = "Rate limit exceeded"):
        super().__init__(message)


class SessionExpiredException(CrawlerException):
    """会话 / cookie 失效，需要重新建立会话。"""

    def __init__(self, message: str = "Session expired, need to re-login"):
        super().__init__(message)


class ParsingException(CrawlerException):
    """HTML/JSON 解析失败；field 定位解析不出的字段，reason 说明原因。"""

    def __init__(self, field: str, reason: str = ""):
        self.field = field
        self.reason = reason
        super().__init__(f"Failed to parse field '{field}': {reason}")


class QuotaExceededException(CrawlerException):
    """日配额用尽（resource_type: 'asins' / 'search' 等）。"""

    def __init__(self, resource_type: str, limit: int):
        self.resource_type = resource_type
        self.limit = limit
        super().__init__(f"Daily quota exceeded for {resource_type} (limit: {limit})")
