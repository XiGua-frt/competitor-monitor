"""tests for ProxyHealthChecker — fake checker_fn 全链路 / 并发探测 / 失败上报。"""

import threading
import time

from src.pools.health_checker import ProxyHealthChecker
from src.pools.ip_pool import IPPool

PROXIES = ['http://10.0.0.1:8080', 'http://10.0.0.2:8080',
           'http://10.0.0.3:8080']


class FakeClock:
    def __init__(self, start=0.0):
        self.now = start

    def __call__(self):
        return self.now


def make_checker(fn, **kw):
    pool = IPPool(PROXIES, cooldown_seconds=60, clock=FakeClock())
    checker = ProxyHealthChecker(pool, checker_fn=fn, **kw)
    return pool, checker


class TestProbeAll:
    def test_all_healthy(self):
        pool, checker = make_checker(lambda url: True)
        summary = checker.probe_all()
        assert summary == {'total': 3, 'ok': 3, 'failed': 0}
        assert pool.healthy_count() == 3
        # 成功进入统计
        assert all(s['success_count'] == 1 for s in pool.stats())

    def test_all_failed_reported(self):
        pool, checker = make_checker(lambda url: False)
        summary = checker.probe_all()
        assert summary['failed'] == 3
        assert pool.healthy_count() == 0

    def test_mixed_results(self):
        def fn(url):
            return url != 'http://10.0.0.2:8080'

        pool, checker = make_checker(fn)
        summary = checker.probe_all()
        assert summary == {'total': 3, 'ok': 2, 'failed': 1}
        stats = {s['proxy']: s for s in pool.stats()}
        assert stats['http://10.0.0.2:8080']['fail_count'] == 1
        assert stats['http://10.0.0.1:8080']['success_count'] == 1

    def test_empty_pool_noop(self):
        pool = IPPool([], clock=FakeClock())
        checker = ProxyHealthChecker(pool, checker_fn=lambda u: True)
        assert checker.probe_all() == {'total': 0, 'ok': 0, 'failed': 0}

    def test_checker_exception_counts_as_failure(self):
        def fn(url):
            raise RuntimeError('timeout')

        pool, checker = make_checker(fn)
        summary = checker.probe_all()
        assert summary['failed'] == 3
        assert pool.healthy_count() == 0

    def test_checker_receives_proxy_urls(self):
        seen = []

        def fn(url):
            seen.append(url)
            return True

        _, checker = make_checker(fn)
        checker.probe_all()
        assert sorted(seen) == sorted(PROXIES)


class TestConcurrentProbe:
    def test_concurrency_bounded_and_parallel(self):
        """3 个代理在 max_workers=3 下应并发进入 checker（barrier 验证）。"""
        barrier = threading.Barrier(3, timeout=2)

        def fn(url):
            barrier.wait()  # 3 个全部到位才通过 → 证明并发执行
            return True

        pool, checker = make_checker(fn, max_workers=5)
        summary = checker.probe_all()
        assert summary == {'total': 3, 'ok': 3, 'failed': 0}

    def test_workers_capped_at_max_workers(self):
        """6 个代理、max_workers=2：任一时刻最多 2 个并发（事件计数验证）。"""
        lock = threading.Lock()
        peak = {'n': 0, 'cur': 0}

        def fn(url):
            with lock:
                peak['cur'] += 1
                peak['n'] = max(peak['n'], peak['cur'])
            time.sleep(0.05)
            with lock:
                peak['cur'] -= 1
            return True

        pool = IPPool([f'http://10.0.0.{i}:8080' for i in range(6)],
                      cooldown_seconds=60, clock=FakeClock())
        checker = ProxyHealthChecker(pool, checker_fn=fn, max_workers=2)
        summary = checker.probe_all()
        assert summary['ok'] == 6
        assert peak['n'] <= 2

    def test_single_proxy_pool(self):
        pool = IPPool(['http://p1:8080'], cooldown_seconds=60,
                      clock=FakeClock())
        checker = ProxyHealthChecker(pool, checker_fn=lambda u: True)
        assert checker.probe_all() == {'total': 1, 'ok': 1, 'failed': 0}


class TestBackgroundLoop:
    def test_not_started_by_default(self):
        pool, checker = make_checker(lambda url: True)
        assert checker._thread is None  # 单测不 start，不启后台线程

    def test_start_stop_daemon_loop(self):
        count = {'n': 0}

        def fn(url):
            count['n'] += 1
            return True

        pool, checker = make_checker(fn, interval=0.05)
        checker.start()
        assert checker._thread.daemon is True
        time.sleep(0.25)
        checker.stop()
        assert count['n'] >= 3 * len(PROXIES)  # 至少跑了多轮
        assert checker._thread is None

    def test_start_is_idempotent(self):
        _, checker = make_checker(lambda url: True, interval=60)
        checker.start()
        thread = checker._thread
        checker.start()
        assert checker._thread is thread
        checker.stop()
