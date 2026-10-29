"""Rate limiting decorator and utilities."""

import time
import random
import functools
from datetime import datetime, timedelta
from typing import Callable, Dict, Optional
import redis

from ..common.config import config
from ..common.logger import get_logger
from ..common.exceptions import RateLimitException, QuotaExceededException

logger = get_logger(__name__)


class RateLimiter:
    """Rate limiter with random intervals and daily quotas."""
    
    def __init__(
        self,
        min_interval: int = None,
        max_interval: int = None,
        redis_client: Optional[redis.Redis] = None
    ):
        """Initialize rate limiter.
        
        Args:
            min_interval: Minimum interval in seconds
            max_interval: Maximum interval in seconds
            redis_client: Optional Redis client for distributed rate limiting
        """
        self.min_interval = min_interval or config.REQUEST_MIN_INTERVAL
        self.max_interval = max_interval or config.REQUEST_MAX_INTERVAL
        self.redis_client = redis_client
        
        # Local state (fallback when Redis not available)
        self._last_request_time: float = 0
        self._daily_counts: Dict[str, int] = {}
        self._daily_reset_time: datetime = datetime.now() + timedelta(days=1)
    
    def wait(self):
        """Wait for rate limit interval."""
        elapsed = time.time() - self._last_request_time
        interval = random.uniform(self.min_interval, self.max_interval)
        
        if elapsed < interval:
            wait_time = interval - elapsed
            logger.debug(f"Rate limiting: waiting {wait_time:.2f}s")
            time.sleep(wait_time)
        
        self._last_request_time = time.time()
    
    def check_quota(self, resource_type: str, limit: int) -> bool:
        """Check if daily quota is exceeded.
        
        Args:
            resource_type: Type of resource (e.g., 'products', 'videos')
            limit: Daily limit
        
        Returns:
            True if within quota
        
        Raises:
            QuotaExceededException: When quota is exceeded
        """
        # Reset daily counts if needed
        if datetime.now() >= self._daily_reset_time:
            self._daily_counts = {}
            self._daily_reset_time = datetime.now() + timedelta(days=1)
        
        # Check quota
        current_count = self._daily_counts.get(resource_type, 0)
        
        if current_count >= limit:
            logger.warning(f"Daily quota exceeded for {resource_type}: {current_count}/{limit}")
            raise QuotaExceededException(resource_type, limit)
        
        # Increment count
        self._daily_counts[resource_type] = current_count + 1
        logger.debug(f"Quota check: {resource_type} {current_count + 1}/{limit}")
        
        return True
    
    def get_quota_status(self, resource_type: str, limit: int) -> Dict[str, int]:
        """Get current quota status.
        
        Args:
            resource_type: Type of resource
            limit: Daily limit
        
        Returns:
            Dictionary with 'used', 'limit', 'remaining'
        """
        used = self._daily_counts.get(resource_type, 0)
        return {
            'used': used,
            'limit': limit,
            'remaining': max(0, limit - used)
        }


def rate_limit(
    min_interval: int = None,
    max_interval: int = None,
    resource_type: str = None,
    quota_limit: int = None
) -> Callable:
    """Decorator for rate limiting functions.
    
    Args:
        min_interval: Minimum interval in seconds
        max_interval: Maximum interval in seconds
        resource_type: Resource type for quota tracking
        quota_limit: Daily quota limit
    
    Example:
        @rate_limit(min_interval=5, max_interval=8, resource_type='products', quota_limit=200)
        def scrape_product(product_id: str):
            pass
    """
    limiter = RateLimiter(min_interval, max_interval)
    
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Check quota if specified
            if resource_type and quota_limit:
                limiter.check_quota(resource_type, quota_limit)
            
            # Wait for rate limit
            limiter.wait()
            
            # Execute function
            return func(*args, **kwargs)
        
        return wrapper
    
    return decorator


# Global rate limiter instance
global_limiter = RateLimiter()
