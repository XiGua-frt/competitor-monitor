"""Custom exceptions for the TikTok crawler."""


class CrawlerException(Exception):
    """Base exception for all crawler errors."""
    pass


class WAFBlockedException(CrawlerException):
    """Raised when WAF/anti-bot blocks the request."""
    
    def __init__(self, url: str, status_code: int = None):
        self.url = url
        self.status_code = status_code
        super().__init__(f"WAF blocked request to {url} (status: {status_code})")


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
        super().__init__(f"Daily quota exceeded for {resource_type} (limit: {limit})")
