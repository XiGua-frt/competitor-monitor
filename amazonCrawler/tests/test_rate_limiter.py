"""tests for RateLimiter — 本地计数模式（Redis 不可用）+ wait() 极小间隔注入。

单测禁止真实网络与真实 Redis；wait() 通过注入 min=max 极小间隔避免真实 sleep。
"""

import time

import pytest

from src.anti_detection.rate_limiter import RateLimiter
# 与实现同源：无论走 src.common 还是 fallback，都用 anti_detection 透传的类
from src.anti_detection import QuotaExceededException


@pytest.fixture(autouse=True)
def no_real_redis(monkeypatch):
    """保险丝：本机即使有真实 Redis 在跑，测试也绝不建立真实连接。

    把 redis.Redis 构造器替换为报错桩——任何模块在测试路径中尝试自建
    Redis 连接都会立即显式失败，而不是悄悄连上 localhost:6379。
    """
    import redis as _redis_mod

    def _forbidden(*a, **kw):
        raise AssertionError("tests must not construct a real redis.Redis client")

    monkeypatch.setattr(_redis_mod, "Redis", _forbidden, raising=False)
    monkeypatch.setattr(_redis_mod, "StrictRedis", _forbidden, raising=False)


class DeadRedis:
    """模拟 Redis 不可用：任何调用都抛异常，触发本地计数降级。"""

    def incr(self, *a, **kw):
        raise ConnectionError("redis down")

    def expire(self, *a, **kw):
        raise ConnectionError("redis down")

    def get(self, *a, **kw):
        raise ConnectionError("redis down")


class FakeRedis:
    """可用 Redis 的最小行为，验证 Redis 路径与降级切换。"""

    def __init__(self):
        self.store = {}
        self.fail_after = None  # 设为数字后第 N 次调用开始失败
        self.fail = False       # True 时所有调用失败
        self._calls = 0

    def _maybe_fail(self):
        self._calls += 1
        if self.fail or (self.fail_after is not None and self._calls > self.fail_after):
            raise ConnectionError("redis down")

    def incr(self, key):
        self._maybe_fail()
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    def expire(self, key, ttl):
        self._maybe_fail()
        self.store[f'{key}:ttl'] = ttl

    def get(self, key):
        self._maybe_fail()
        return self.store.get(key)


class TestWait:
    """wait() 用极小间隔注入，不真实长 sleep。"""

    def test_wait_small_interval_returns_fast(self):
        limiter = RateLimiter(min_interval=0.001, max_interval=0.003)
        start = time.time()
        waited = limiter.wait()
        assert time.time() - start < 1.0
        assert waited < 1.0

    def test_wait_second_call_enforces_interval(self):
        # 第一次 wait 重置基准，第二次强制等待 ~0.01s
        limiter = RateLimiter(min_interval=0.01, max_interval=0.011)
        limiter.wait()
        start = time.time()
        limiter.wait()
        assert time.time() - start >= 0.005

    def test_wait_no_wait_when_elapsed_exceeds_interval(self):
        limiter = RateLimiter(min_interval=0.001, max_interval=0.002)
        limiter.wait()
        time.sleep(0.01)  # 已超过最大间隔 → 下次 wait 不应等待
        waited = limiter.wait()
        assert waited == 0.0

    def test_interval_bounds_swapped_if_inverted(self):
        limiter = RateLimiter(min_interval=15.0, max_interval=8.0)
        assert limiter.min_interval == 8.0
        assert limiter.max_interval == 15.0


class TestLocalQuotaFallback:
    """Redis 不可用时自动降级本地计数。"""

    def test_check_quota_counts_up_to_limit(self):
        limiter = RateLimiter(min_interval=0, max_interval=0, redis_client=DeadRedis())
        for _ in range(3):
            assert limiter.check_quota('asins', 3) is True
        with pytest.raises(QuotaExceededException):
            limiter.check_quota('asins', 3)

    def test_quota_exception_fields(self):
        limiter = RateLimiter(min_interval=0, max_interval=0, redis_client=DeadRedis())
        limiter.check_quota('search', 1)
        with pytest.raises(QuotaExceededException) as exc_info:
            limiter.check_quota('search', 1)
        assert exc_info.value.resource_type == 'search'
        assert exc_info.value.limit == 1

    def test_independent_resource_types(self):
        limiter = RateLimiter(min_interval=0, max_interval=0, redis_client=DeadRedis())
        assert limiter.check_quota('asins', 1) is True
        assert limiter.check_quota('search', 1) is True
        with pytest.raises(QuotaExceededException):
            limiter.check_quota('asins', 1)

    def test_no_redis_client_uses_local(self):
        limiter = RateLimiter(min_interval=0, max_interval=0, redis_client=None)
        assert limiter.check_quota('asins', 2) is True
        assert limiter.check_quota('asins', 2) is True
        with pytest.raises(QuotaExceededException):
            limiter.check_quota('asins', 2)

    def test_quota_status(self):
        limiter = RateLimiter(min_interval=0, max_interval=0, redis_client=DeadRedis())
        limiter.check_quota('asins', 5)
        status = limiter.get_quota_status('asins', 5)
        assert status == {'used': 1, 'limit': 5, 'remaining': 4}


class TestRedisPath:
    """Redis 可用时走 Redis 计数；中途故障降级本地且不丢已用计数语义。"""

    def test_redis_path_counts(self):
        redis = FakeRedis()
        limiter = RateLimiter(min_interval=0, max_interval=0, redis_client=redis)
        assert limiter.check_quota('asins', 2) is True
        assert limiter.check_quota('asins', 2) is True
        with pytest.raises(QuotaExceededException):
            limiter.check_quota('asins', 2)
        # Redis 里确实写入了计数
        assert any(v == 3 for v in redis.store.values() if isinstance(v, int))

    def test_midway_redis_failure_falls_back_to_local(self):
        redis = FakeRedis()
        redis.fail_after = 2  # 前两次成功，之后全部失败
        limiter = RateLimiter(min_interval=0, max_interval=0, redis_client=redis)
        assert limiter.check_quota('asins', 4) is True  # redis incr #1
        assert limiter.check_quota('asins', 4) is True  # redis incr #2
        # 第 3 次：redis 挂 → 降级本地，本地计数由 Redis 快照播种（=2）
        assert limiter.check_quota('asins', 4) is True  # 本地第 3 次
        assert limiter.check_quota('asins', 4) is True  # 本地第 4 次 = limit
        with pytest.raises(QuotaExceededException):
            limiter.check_quota('asins', 4)  # 第 5 次超限

    def test_fallback_counts_seed_from_redis_snapshot(self):
        """降级后配额语义连续：Redis 阶段已消耗的计数不因降级而重置。"""
        redis = FakeRedis()
        redis.fail_after = 1
        limiter = RateLimiter(min_interval=0, max_interval=0, redis_client=redis)
        assert limiter.check_quota('asins', 3) is True  # redis 计 1
        redis.fail = True  # 模拟此后每次调用都挂
        redis._calls = 99
        # 降级后 used 应从 1 起算，而非从 0
        assert limiter.check_quota('asins', 3) is True  # 本地计 2
        assert limiter.check_quota('asins', 3) is True  # 本地计 3
        with pytest.raises(QuotaExceededException):
            limiter.check_quota('asins', 3)
