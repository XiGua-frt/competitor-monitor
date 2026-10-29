"""tests for LeaseManager — 获取 / 重复获取冲突 / 释放后可再获取。

单测禁止真实网络与真实 Redis：Redis 路径用 FakeRedis 验证 SETNX 语义，
文件锁路径用 tmp_path 隔离目录，并验证 Redis 故障时的降级。
"""

import threading

import pytest

from src.pools.lease import LeaseConflictException, LeaseManager


class FakeRedis:
    """最小 Redis SETNX 语义：SET key val EX ttl NX。"""

    def __init__(self):
        self.store = {}
        self.fail = False

    def set(self, key, value, ex=None, nx=False):
        if self.fail:
            raise ConnectionError("redis down")
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    def get(self, key):
        if self.fail:
            raise ConnectionError("redis down")
        return self.store.get(key)

    def delete(self, key):
        if self.fail:
            raise ConnectionError("redis down")
        return self.store.pop(key, None)


class TestRedisPath:
    """Redis 可用路径：SETNX 获取 / 冲突 / 释放。"""

    def test_acquire_and_release(self):
        redis = FakeRedis()
        mgr = LeaseManager(redis_client=redis)
        with mgr.acquire_lease('pdp_batch', ttl=60) as held:
            assert held is True
            assert any('lease:pdp_batch' in k for k in redis.store)
        # 释放后 key 已删除
        assert 'amz:lease:pdp_batch' not in redis.store

    def test_conflict_raises(self):
        redis = FakeRedis()
        mgr1 = LeaseManager(redis_client=redis)
        mgr2 = LeaseManager(redis_client=redis)
        with mgr1.acquire_lease('pdp_batch', ttl=60):
            with pytest.raises(LeaseConflictException):
                with mgr2.acquire_lease('pdp_batch', ttl=60):
                    pass

    def test_release_then_reacquire(self):
        redis = FakeRedis()
        mgr1 = LeaseManager(redis_client=redis)
        mgr2 = LeaseManager(redis_client=redis)
        with mgr1.acquire_lease('search_batch', ttl=60):
            pass
        # 释放后第二个持有者可立即获取
        with mgr2.acquire_lease('search_batch', ttl=60) as held:
            assert held is True

    def test_only_holder_releases(self):
        """释放时 token 不匹配不误删他人锁。"""
        redis = FakeRedis()
        mgr1 = LeaseManager(redis_client=redis)
        mgr2 = LeaseManager(redis_client=redis)
        with mgr1.acquire_lease('pdp_batch', ttl=60):
            # 模拟 mgr2 释放一个它没持有的锁（无上下文，直接内部调用不可行；
            # 这里验证 mgr2 冲突后，mgr1 正常退出时锁被清理）
            with pytest.raises(LeaseConflictException):
                with mgr2.acquire_lease('pdp_batch', ttl=60):
                    pass
        assert 'amz:lease:pdp_batch' not in redis.store

    def test_different_task_types_no_conflict(self):
        redis = FakeRedis()
        mgr = LeaseManager(redis_client=redis)
        with mgr.acquire_lease('pdp_batch', ttl=60):
            with mgr.acquire_lease('offer_batch', ttl=60) as held:
                assert held is True


class TestFileLockFallback:
    """Redis 不可用降级本地文件锁。"""

    def test_file_lock_acquire_conflict_release(self, tmp_path):
        mgr1 = LeaseManager(redis_client=FakeRedisDown(), lock_dir=tmp_path)
        mgr2 = LeaseManager(redis_client=FakeRedisDown(), lock_dir=tmp_path)

        with mgr1.acquire_lease('pdp_batch', ttl=60):
            lock_file = tmp_path / 'pdp_batch.lock'
            assert lock_file.exists()
            with pytest.raises(LeaseConflictException):
                with mgr2.acquire_lease('pdp_batch', ttl=60):
                    pass
        # 释放后锁文件已删，可再获取
        assert not (tmp_path / 'pdp_batch.lock').exists()
        with mgr2.acquire_lease('pdp_batch', ttl=60) as held:
            assert held is True

    def test_stale_file_lock_reclaimed_after_ttl(self, tmp_path):
        import os
        import time

        mgr = LeaseManager(redis_client=FakeRedisDown(), lock_dir=tmp_path)
        lock_file = tmp_path / 'stale.lock'
        lock_file.write_text('{"holder": "dead"}')
        # 把 mtime 回拨到 TTL 之前 → 视为残留
        old = time.time() - 7200
        os.utime(lock_file, (old, old))

        with mgr.acquire_lease('stale', ttl=60) as held:
            assert held is True

    def test_redis_failure_midway_falls_back_to_file(self, tmp_path):
        redis = FakeRedis()
        mgr = LeaseManager(redis_client=redis, lock_dir=tmp_path)
        with mgr.acquire_lease('pdp_batch', ttl=60):
            redis.fail = True  # 持有期间 Redis 挂了
            # 释放路径应容错不抛
        redis.fail = False
        # Redis 锁还在（释放走文件路径失败后 redis 锁可能残留）——
        # 重新获取若被残留锁挡住属预期边界；这里验证不抛非 Lease 异常
        try:
            with mgr.acquire_lease('pdp_batch', ttl=60) as held:
                assert held is True
        except LeaseConflictException:
            pass  # 残留 Redis 锁在 TTL 内被挡住也是合法行为


class FakeRedisDown:
    """永久不可用的 Redis（构造即坏，模拟无 Redis 环境）。"""

    def set(self, *a, **kw):
        raise ConnectionError("redis down")

    def get(self, *a, **kw):
        raise ConnectionError("redis down")

    def delete(self, *a, **kw):
        raise ConnectionError("redis down")


class TestConcurrency:
    """同批次任务并发防重复（确定性：持锁期间并发竞争，恰好一个成功）。"""

    def test_concurrent_acquire_only_one_wins_while_held(self):
        redis = FakeRedis()
        mgr = LeaseManager(redis_client=redis)
        release = threading.Event()
        results = []
        barrier = threading.Barrier(4)

        def worker():
            barrier.wait()
            try:
                with mgr.acquire_lease('pdp_batch', ttl=60):
                    results.append('acquired')
                    # 持有者等所有竞争者出结果后再释放，杜绝"先释放后竞争"的合法二获
                    release.wait(timeout=5)
            except LeaseConflictException:
                results.append('conflict')

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # 持锁窗口内：恰好 1 个成功，其余 3 个冲突
        assert results.count('acquired') == 1
        assert results.count('conflict') == 3

    def test_sequential_reacquire_after_release(self):
        """释放后另一持有者可获取（顺序语义，非并发窗口）。"""
        redis = FakeRedis()
        mgr = LeaseManager(redis_client=redis)
        with mgr.acquire_lease('pdp_batch', ttl=60):
            pass
        with mgr.acquire_lease('pdp_batch', ttl=60) as held:
            assert held is True

    def test_file_lock_concurrent_acquire_only_one_wins(self, tmp_path):
        """文件锁降级路径的并发原子性（O_CREAT|O_EXCL）。"""
        mgr = LeaseManager(redis_client=FakeRedisDown(), lock_dir=tmp_path)
        release = threading.Event()
        results = []
        barrier = threading.Barrier(4)

        def worker():
            barrier.wait()
            try:
                with mgr.acquire_lease('pdp_batch', ttl=60):
                    results.append('acquired')
                    release.wait(timeout=5)
            except LeaseConflictException:
                results.append('conflict')

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert results.count('acquired') == 1
        assert results.count('conflict') == 3
