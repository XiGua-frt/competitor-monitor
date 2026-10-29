"""Custom exceptions for pool management."""


class PoolException(Exception):
    """Base exception for pool errors."""
    pass


class NoAvailableAccountException(PoolException):
    """Raised when no account is available in the pool."""
    
    def __init__(self, message: str = "No available accounts in pool"):
        super().__init__(message)


class NoAvailableProxyException(PoolException):
    """Raised when no proxy is available in the pool."""
    
    def __init__(self, message: str = "No available proxies in pool"):
        super().__init__(message)


class AccountSuspendedException(PoolException):
    """Raised when trying to use a suspended account."""
    
    def __init__(self, account_id: str):
        super().__init__(f"Account {account_id} is suspended")
        self.account_id = account_id


class ProxyFailedException(PoolException):
    """Raised when proxy fails health check."""
    
    def __init__(self, proxy_id: str, reason: str = ""):
        super().__init__(f"Proxy {proxy_id} failed: {reason}")
        self.proxy_id = proxy_id
        self.reason = reason


class InvalidProxyException(PoolException):
    """Raised when proxy URL is invalid."""
    
    def __init__(self, proxy_url: str):
        super().__init__(f"Invalid proxy URL: {proxy_url}")
        self.proxy_url = proxy_url


class AccountQuotaExceededException(PoolException):
    """Raised when account daily quota is exceeded."""
    
    def __init__(self, account_id: str, quota: int):
        super().__init__(f"Account {account_id} exceeded daily quota ({quota})")
        self.account_id = account_id
        self.quota = quota
