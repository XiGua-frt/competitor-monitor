"""tests for IPPool — acquire 轮询 / 失败冷却 / healthy_count。"""

import pytest

from src.pools.ip_pool import IPPool


class FakeClock:
    """可控单调时钟（time.monotonic 替身）。"""

    def __init__(self, start=0.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


PROXIES = [
    'http://10.0.0.1:8080',
    'http://10.0.0.2:8080',
    'http://10.0.0.3:8080',
]


class TestAcquireRoundRobin:
    """acquire 按列表顺序轮询。"""

    def test_round_robin_order(self):
        pool = IPPool(PROXIES)
        acquired = [pool.acquire() for _ in range(6)]
        assert acquired == PROXIES * 2

    def test_acquire_single_cycle(self):
        pool = IPPool(PROXIES)
        assert pool.acquire() == 'http://10.0.0.1:8080'
        assert pool.acquire() == 'http://10.0.0.2:8080'
        assert pool.acquire() == 'http://10.0.0.3:8080'

    def test_acquire_empty_pool_returns_none(self):
        assert IPPool([]).acquire() is None

    def test_acquire_from_single_proxy_repeats(self):
        pool = IPPool(['http://p1:8080'])
        assert pool.acquire() == 'http://p1:8080'
        assert pool.acquire() == 'http://p1:8080'


class TestFailureCooldown:
    """report_failure 标记冷却 30 分钟，冷却期内跳过，到期恢复。"""

    def test_failed_proxy_skipped(self):
        clock = FakeClock()
        pool = IPPool(PROXIES, clock=clock)
        pool.report_failure('http://10.0.0.1:8080')

        acquired = [pool.acquire() for _ in range(4)]
        # p1 冷却中，轮询只剩 p2/p3 交替
        assert acquired == ['http://10.0.0.2:8080', 'http://10.0.0.3:8080',
                            'http://10.0.0.2:8080', 'http://10.0.0.3:8080']

    def test_cooldown_default_30_minutes(self):
        pool = IPPool(PROXIES)
        assert pool.cooldown_seconds == 30 * 60

    def test_cooldown_expires_and_proxy_recovers(self):
        clock = FakeClock()
        pool = IPPool(PROXIES, cooldown_seconds=60, clock=clock)
        pool.report_failure('http://10.0.0.1:8080')

        assert pool.acquire() == 'http://10.0.0.2:8080'  # 冷却中跳过 p1
        clock.advance(61)  # 冷却到期
        # 轮询游标继续前移：p2 之后是 p3，再到已恢复的 p1
        assert pool.acquire() == 'http://10.0.0.3:8080'
        assert pool.acquire() == 'http://10.0.0.1:8080'  # p1 冷却到期恢复参与轮询

    def test_repeated_failure_resets_cooldown(self):
        clock = FakeClock()
        pool = IPPool(PROXIES, cooldown_seconds=60, clock=clock)
        pool.report_failure('http://10.0.0.1:8080')
        clock.advance(30)
        pool.report_failure('http://10.0.0.1:8080')  # 重新计时
        clock.advance(31)  # 距第一次失败 61s，但距第二次仅 31s → 仍冷却
        assert pool.acquire() == 'http://10.0.0.2:8080'

    def test_report_failure_unknown_proxy_no_crash(self):
        pool = IPPool(PROXIES)
        pool.report_failure('http://unknown:9999')  # 仅告警，不抛异常
        assert pool.healthy_count() == 3

    def test_all_cooldown_returns_none(self):
        clock = FakeClock()
        pool = IPPool(PROXIES, clock=clock)
        for p in PROXIES:
            pool.report_failure(p)
        assert pool.acquire() is None


class TestHealthyCount:
    def test_all_healthy_initially(self):
        assert IPPool(PROXIES).healthy_count() == 3

    def test_after_failure(self):
        clock = FakeClock()
        pool = IPPool(PROXIES, clock=clock)
        pool.report_failure(PROXIES[0])
        assert pool.healthy_count() == 2

    def test_recovers_after_cooldown(self):
        clock = FakeClock()
        pool = IPPool(PROXIES, cooldown_seconds=60, clock=clock)
        pool.report_failure(PROXIES[0])
        assert pool.healthy_count() == 2
        clock.advance(61)
        assert pool.healthy_count() == 3

    def test_empty_pool(self):
        assert IPPool([]).healthy_count() == 0
