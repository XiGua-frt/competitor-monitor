"""tests for IPPool v2 — 滑动窗口成功率 / 动态降权 / weighted / stats 脱敏。

向后兼容回归：tests/test_ip_pool.py 继续覆盖 v1 原语义。
"""

import random

import pytest

from src.pools.ip_pool import IPPool


class FakeClock:
    def __init__(self, start=0.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


PROXIES = ['http://10.0.0.1:8080', 'http://10.0.0.2:8080',
           'http://10.0.0.3:8080']


class TestPerProxyStats:
    def test_success_count_and_rate(self):
        pool = IPPool(PROXIES)
        for _ in range(3):
            pool.report_success(PROXIES[0], response_time=0.5)
        pool.report_failure(PROXIES[0])
        stats = {s['proxy']: s for s in pool.stats()}
        p1 = stats['http://10.0.0.1:8080']
        assert p1['success_count'] == 3
        assert p1['fail_count'] == 1
        assert p1['success_rate'] == 0.75  # 滑动窗口 [1,1,1,0]
        assert p1['avg_response_time'] == 0.5

    def test_sliding_window_keeps_recent_n(self):
        pool = IPPool(PROXIES, window_size=5)
        # 6 次成功后 4 次失败 → 窗口只剩最近 5 个（4 失败 + 1 成功）
        for _ in range(6):
            pool.report_success(PROXIES[0])
        for _ in range(4):
            pool.report_failure(PROXIES[0])
        stats = {s['proxy']: s for s in pool.stats()}
        assert stats['http://10.0.0.1:8080']['success_rate'] == 0.2

    def test_empty_window_rate_is_one(self):
        pool = IPPool(PROXIES)
        stats = pool.stats()
        assert all(s['success_rate'] == 1.0 for s in stats)

    def test_avg_response_time_none_without_samples(self):
        pool = IPPool(PROXIES)
        assert pool.stats()[0]['avg_response_time'] is None

    def test_report_success_resets_continuous_fail(self):
        pool = IPPool(PROXIES)
        for _ in range(3):
            pool.report_failure(PROXIES[0])
        pool.report_success(PROXIES[0])
        stats = {s['proxy']: s for s in pool.stats()}
        assert stats['http://10.0.0.1:8080']['continuous_fail_count'] == 0


class TestDynamicEscalation:
    """连续失败 >=3 次 → 冷却 30min*4=2h；>=6 次 → *24=12h。"""

    def test_base_cooldown_below_threshold(self):
        clock = FakeClock()
        pool = IPPool(PROXIES, clock=clock)
        pool.report_failure(PROXIES[0])
        pool.report_failure(PROXIES[0])
        # 30min 后恢复（未达 3 次，无降权）
        clock.advance(30 * 60 + 1)
        assert pool.healthy_count() == 3

    def test_escalation_to_2h(self):
        clock = FakeClock()
        pool = IPPool(PROXIES, clock=clock)
        for _ in range(3):
            pool.report_failure(PROXIES[0])
        clock.advance(30 * 60 + 1)
        assert pool.healthy_count() == 2  # 仍冷却
        clock.advance(2 * 60 * 60)
        assert pool.healthy_count() == 3  # 2h 到期恢复

    def test_escalation_to_12h(self):
        clock = FakeClock()
        pool = IPPool(PROXIES, clock=clock)
        for _ in range(6):
            pool.report_failure(PROXIES[0])
        clock.advance(12 * 60 * 60 + 1)
        assert pool.healthy_count() == 3

    def test_success_resets_escalation(self):
        clock = FakeClock()
        pool = IPPool(PROXIES, clock=clock)
        for _ in range(3):
            pool.report_failure(PROXIES[0])
        pool.report_success(PROXIES[0])  # 重置连续计数
        clock.advance(30 * 60 + 1)
        assert pool.healthy_count() == 3  # 回到基础冷却


class TestWeightedAcquire:
    def test_weighted_default_round_robin_compat(self):
        pool = IPPool(PROXIES)
        acquired = [pool.acquire() for _ in range(3)]
        assert acquired == PROXIES  # 默认仍是严格轮询

    def test_weighted_low_rate_proxy_avoided(self):
        # 固定种子可控随机；p0 成功率 0 → 权重下限 0.05，几乎选不中
        rng = random.Random(42)
        pool = IPPool(PROXIES, strategy='weighted', rng=rng)
        for _ in range(20):
            pool.report_failure(PROXIES[0])
        for _ in range(20):
            pool.report_success(PROXIES[1])
        picks = {p: 0 for p in PROXIES}
        for _ in range(200):
            picks[pool.acquire(strategy='weighted')] += 1
        assert picks['http://10.0.0.1:8080'] == 0  # 概率 (0.05/2.05)^k，实测 0
        assert picks['http://10.0.0.2:8080'] > 100

    def test_weighted_all_cooldown_returns_none(self):
        clock = FakeClock()
        pool = IPPool(PROXIES, clock=clock)
        for p in PROXIES:
            pool.report_failure(p)
        assert pool.acquire(strategy='weighted') is None

    def test_weighted_never_excludes_fresh_proxies(self):
        rng = random.Random(1)
        pool = IPPool(PROXIES, strategy='weighted', rng=rng)
        # 全新代理无样本，权重都是 1.0 → 三者都可能出现
        picks = {pool.acquire(strategy='weighted') for _ in range(100)}
        assert picks == set(PROXIES)


class TestStatsMasking:
    def test_credentials_masked(self):
        pool = IPPool(['http://user:secret@1.2.3.4:8080'])
        s = pool.stats()[0]
        assert 'secret' not in s['proxy']
        assert 'user' not in s['proxy']
        assert s['proxy'] == 'http://***@1.2.3.4:8080'

    def test_stats_contains_expected_keys(self):
        pool = IPPool(PROXIES)
        s = pool.stats()[0]
        assert set(s) == {'proxy', 'success_count', 'fail_count',
                          'success_rate', 'avg_response_time',
                          'last_used_at', 'continuous_fail_count',
                          'in_cooldown'}


class TestUpdateProxies:
    def test_diff_sync_add_and_remove(self):
        pool = IPPool(PROXIES)
        pool.update_proxies([PROXIES[0], 'http://10.0.0.9:8080'])
        assert list(pool._proxies) == ['http://10.0.0.1:8080',
                                       'http://10.0.0.9:8080']
        assert pool.healthy_count() == 2

    def test_update_keeps_stats_of_surviving_proxy(self):
        pool = IPPool(PROXIES)
        pool.report_success(PROXIES[0], response_time=0.3)
        pool.update_proxies([PROXIES[0], PROXIES[1]])
        stats = {s['proxy']: s for s in pool.stats()}
        assert stats['http://10.0.0.1:8080']['success_count'] == 1
