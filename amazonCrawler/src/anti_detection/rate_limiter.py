"""Rate limiter: random interval waits + Redis daily quotas with local fallback.

参考 TKCrawler/src/anti_detection/rate_limiter.py 设计：
  - wait(): 在 [min_interval, max_interval] 随机取间隔，与上次请求时间差不足则 sleep
  - check_quota(resource_type, limit): Redis INCR + 当日过期 key；Redis 不可用
    自动降级本地计数（进程内 dict + 当日重置）
  - 极小间隔可通过构造参数注入（单测避免真实 sleep）
"""

import random
import time
from datetime import date
from typing import Dict, Optional

try:  # pragma: no cover - 取决于 Agent A 是否已就绪
    from src.common.exceptions import QuotaExceededException
    from src.common.logger import get_logger

    _COMMON_READY = True
except ImportError:
    from . import _common_fallback as _fb  # type: ignore

    QuotaExceededException = _fb.QuotaExceededException
    get_logger = _fb.get_logger
    _COMMON_READY = False

logger = get_logger(__name__)

# Redis key 前缀与 TTL（当日秒数过期）
_QUOTA_KEY_PREFIX = 'amz:quota:'
_QUOTA_TTL_SECONDS = 86400


class RateLimiter:
    """Rate limiter with random intervals and daily quotas (Redis -> local fallback)."""

    def __init__(
        self,
        min_interval: float = 8.0,
        max_interval: float = 15.0,
        redis_client=None,
    ):
        """Initialize rate limiter.

        Args:
            min_interval: Minimum interval in seconds
            max_interval: Maximum interval in seconds
            redis_client: Optional Redis client for distributed quota counting;
                传入 None 或不可用实例都会走本地计数降级
        """
        if min_interval > max_interval:
            min_interval, max_interval = max_interval, min_interval
        self.min_interval = min_interval
        self.max_interval = max_interval
        self._redis = redis_client

        # 本地状态（Redis 降级 / wait 基准）
        self._last_request_time: float = 0.0
        self._local_counts: Dict[str, int] = {}
        self._local_counts_date: Optional[date] = None
        # 最近一次成功的 Redis 计数快照：Redis 中途不可用时用它播种本地计数，
        # 保证降级前后配额语义连续（不会绕过 Redis 阶段已消耗的配额）
        self._last_redis_counts: Dict[str, int] = {}

    # ------------------------------------------------------------------ wait

    def wait(self) -> float:
        """随机区间等待：距上次请求不足随机间隔则 sleep 补齐。

        Returns:
            本次实际等待的秒数（0 表示无需等待）
        """
        elapsed = time.time() - self._last_request_time
        interval = random.uniform(self.min_interval, self.max_interval)

        waited = 0.0
        if elapsed < interval:
            wait_time = interval - elapsed
            logger.debug(f"Rate limiting: waiting {wait_time:.2f}s")
            time.sleep(wait_time)
            waited = wait_time

        self._last_request_time = time.time()
        return waited

    # ---------------------------------------------------------------- quota

    def check_quota(self, resource_type: str, limit: int) -> bool:
        """检查并消耗当日配额；超出则抛 QuotaExceededException。

        Args:
            resource_type: 资源类型（如 'asins', 'search'）
            limit: 当日上限

        Returns:
            True 表示配额内且已计数

        Raises:
            QuotaExceededException: 当日配额已耗尽
        """
        if self._redis is not None and self._try_redis_quota(resource_type, limit):
            return True
        return self._local_quota(resource_type, limit)

    def get_quota_status(self, resource_type: str, limit: int) -> Dict[str, int]:
        """当前配额状态：{'used', 'limit', 'remaining'}（只读，不计数）。"""
        used = self._read_used(resource_type)
        return {
            'used': used,
            'limit': limit,
            'remaining': max(0, limit - used),
        }

    # ------------------------------------------------------------------ internal

    def _try_redis_quota(self, resource_type: str, limit: int) -> bool:
        """Redis INCR 路径；任何 Redis 异常都降级本地并返回 False。

        Redis 原子性保证多进程共享配额；key 按日（UTC+本地 date）生成，
        当日首次 INCR 时设置 TTL。
        """
        try:
            key = f"{_QUOTA_KEY_PREFIX}{resource_type}:{date.today().isoformat()}"
            count = self._redis.incr(key)
            if count == 1:
                self._redis.expire(key, _QUOTA_TTL_SECONDS)

            # 快照成功计数：供 Redis 故障降级本地时播种，保证语义连续
            self._last_redis_counts[key] = count

            if count > limit:
                logger.warning(
                    f"Daily quota exceeded for {resource_type}: {count}/{limit}"
                )
                raise QuotaExceededException(resource_type, limit)

            logger.debug(f"Quota check (redis): {resource_type} {count}/{limit}")
            return True
        except QuotaExceededException:
            raise
        except Exception as e:
            logger.warning(f"Redis quota unavailable, falling back to local: {e}")
            self._redis = None
            # 用最后一次成功的 Redis 计数播种本地计数，避免降级后重置配额
            if self._local_counts_date != date.today():
                self._local_counts = {}
                self._local_counts_date = date.today()
            for k, v in self._last_redis_counts.items():
                rt = k[len(_QUOTA_KEY_PREFIX):].rsplit(':', 1)[0]
                self._local_counts[rt] = max(self._local_counts.get(rt, 0), v)
            return False

    def _local_quota(self, resource_type: str, limit: int) -> bool:
        """本地计数降级路径（进程内 dict，按日重置）。"""
        today = date.today()
        if self._local_counts_date != today:
            self._local_counts = {}
            self._local_counts_date = today

        count = self._local_counts.get(resource_type, 0)
        if count >= limit:
            logger.warning(
                f"Daily quota exceeded for {resource_type}: {count}/{limit} (local)"
            )
            raise QuotaExceededException(resource_type, limit)

        self._local_counts[resource_type] = count + 1
        logger.debug(f"Quota check (local): {resource_type} {count + 1}/{limit}")
        return True

    def _read_used(self, resource_type: str) -> int:
        """读取已用配额，用于观测；Redis 异常时读本地。"""
        if self._redis is not None:
            try:
                key = f"{_QUOTA_KEY_PREFIX}{resource_type}:{date.today().isoformat()}"
                val = self._redis.get(key)
                if val is not None:
                    return int(val)
            except Exception:
                pass
        if self._local_counts_date == date.today():
            return self._local_counts.get(resource_type, 0)
        return 0
