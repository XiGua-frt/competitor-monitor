"""Unit tests for task-level account lease lifecycle (src/pools/lease.py).

fakeredis only, no network, repeatable.
"""

import json
import time
from datetime import datetime, timedelta

import pytest
import fakeredis

from src.pools.cookie_pool import CookiePoolManager
from src.pools.lease import LeaseManager, AccountLease, LeaseExhaustedException
from src.pools.exceptions import NoAvailableAccountException
from src.common.logger import logger as loguru_logger

SECRET_COOKIE_VALUE = 'SECRET_COOKIE_VALUE_XYZ9'


@pytest.fixture
def redis_client():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def pool(redis_client):
    return CookiePoolManager(redis_client=redis_client)


@pytest.fixture
def mgr(pool):
    return LeaseManager(pool, lease_ttl_seconds=60, max_acquire_attempts=5)


def add_account(pool, username):
    return pool.add_account(
        username=username,
        cookies={'sessionid': SECRET_COOKIE_VALUE},
        proxy_id='proxy_test',
    )


class TestAcquire:
    def test_acquire_success_fields_and_redis_keys(self, pool, mgr, redis_client):
        account_id = add_account(pool, 'user_a')

        lease = mgr.acquire(scraper_type='scrape_video', task_id='task_001')

        assert lease.lease_id
        assert lease.account_id == account_id
        assert lease.scraper_type == 'scrape_video'
        assert lease.task_id == 'task_001'
        assert isinstance(lease.acquired_at, datetime)
        assert isinstance(lease.expires_at, datetime)
        assert lease.expires_at > lease.acquired_at
        assert lease.released is False
        # account 引用可用，但 repr 不泄露 cookies
        assert lease.account is not None
        assert 'cookies' not in repr(lease)
        assert SECRET_COOKIE_VALUE not in repr(lease)

        # Redis 两个 key 存在
        raw = redis_client.get(f'lease:{lease.lease_id}')
        assert raw is not None
        assert redis_client.get(f'lease:account:{account_id}') == lease.lease_id
        # TTL 已设置
        assert redis_client.ttl(f'lease:{lease.lease_id}') > 0
        assert redis_client.ttl(f'lease:account:{account_id}') > 0
        # 存储载荷不含 cookie
        payload = json.loads(raw)
        assert payload['account_id'] == account_id
        assert 'cookies' not in payload

    def test_single_account_exclusive_second_acquire_raises(self, pool, mgr):
        add_account(pool, 'only_account')

        first = mgr.acquire('scrape_video', 'task_001')
        assert first is not None

        with pytest.raises(LeaseExhaustedException):
            mgr.acquire('scrape_video', 'task_002')

    def test_empty_pool_propagates_no_available(self, pool, mgr):
        # 池空：直接传播 NoAvailableAccountException，与 LeaseExhaustedException 可区分
        with pytest.raises(NoAvailableAccountException):
            mgr.acquire('scrape_video', 'task_001')

        # 两个异常互不为子类，调用层可区分"空池"与"全被租约占用"
        assert not issubclass(LeaseExhaustedException, NoAvailableAccountException)
        assert not issubclass(NoAvailableAccountException, LeaseExhaustedException)

    def test_multi_account_skips_occupied(self, pool, mgr, monkeypatch):
        acc_a = add_account(pool, 'user_a')
        acc_b = add_account(pool, 'user_b')

        lease_a = mgr.acquire('scrape_video', 'task_001')
        occupied_id = lease_a.account_id
        assert occupied_id in (acc_a, acc_b)

        # 固定选号策略：强制跳到未被占用的账号（绕开 weighted 的随机性）
        def pick_other(candidates):
            others = [c for c in candidates if c.account_id != occupied_id]
            return others[0] if others else candidates[0]

        monkeypatch.setattr(pool, '_select_weighted', pick_other)

        lease_b = mgr.acquire('scrape_video', 'task_002')
        assert lease_b.account_id != occupied_id
        assert {lease_a.account_id, lease_b.account_id} == {acc_a, acc_b}
        assert len(mgr.active_leases()) == 2


class TestRelease:
    def test_release_then_reacquire(self, pool, mgr):
        add_account(pool, 'user_a')

        lease = mgr.acquire('scrape_video', 'task_001')
        mgr.release(lease, success=True)

        assert lease.released is True
        assert mgr.active_leases() == []

        # release 后账号可再次 acquire
        lease2 = mgr.acquire('scrape_video', 'task_002')
        assert lease2.account_id == lease.account_id
        assert lease2.lease_id != lease.lease_id

    def test_release_idempotent_no_double_counting(self, pool, mgr, monkeypatch):
        account_id = add_account(pool, 'user_a')

        calls = []
        original_release = pool.release_account

        def spy(account_id_arg, success, response_time=0):
            calls.append((account_id_arg, success))
            return original_release(account_id_arg, success, response_time)

        monkeypatch.setattr(pool, 'release_account', spy)

        lease = mgr.acquire('scrape_video', 'task_001')

        result1 = mgr.release(lease, success=True)
        result2 = mgr.release(lease, success=True)

        assert result1 in ('success', 'failure')
        assert result2 == 'noop'
        # 底层 release_account 只触发一次
        assert len(calls) == 1
        assert calls[0] == (account_id, True)

        # daily 计数不重复
        session = pool.accounts[account_id]
        assert session.daily_request_count == 1
        assert session.total_requests == 1
        assert session.success_requests == 1

    def test_release_nonexistent_lease_safe(self, pool, mgr):
        ghost = AccountLease(
            lease_id='ls_ghost',
            account_id='acc_not_in_pool',
            scraper_type='scrape_video',
            task_id='task_001',
            acquired_at=datetime.now(),
            expires_at=datetime.now() + timedelta(seconds=60),
        )
        # 不存在于 Redis、账号也不在池中 → 不抛异常
        result = mgr.release(ghost, success=True)
        assert result in ('success', 'failure', 'noop')
        assert ghost.released is True

    def test_release_expired_lease_cleanup_only(self, pool, mgr, redis_client, monkeypatch):
        account_id = add_account(pool, 'user_a')

        calls = []
        original_release = pool.release_account

        def spy(account_id_arg, success, response_time=0):
            calls.append(account_id_arg)
            return original_release(account_id_arg, success, response_time)

        monkeypatch.setattr(pool, 'release_account', spy)

        lease = mgr.acquire('scrape_video', 'task_001')

        # 手动把 lease 改成已过期（模拟 TTL 到期但调用方仍持引用）
        lease.expires_at = datetime.now() - timedelta(seconds=1)

        result = mgr.release(lease, success=True)

        assert result == 'expired'
        # 过期 lease 的 release 不回写底层计数
        assert calls == []
        assert pool.accounts[account_id].daily_request_count == 0
        # key 已被清理
        assert redis_client.get(f'lease:{lease.lease_id}') is None
        assert redis_client.get(f'lease:account:{account_id}') is None


class TestTTLAndReclaim:
    def test_expired_lease_allows_reacquire(self, redis_client, pool):
        mgr = LeaseManager(pool, lease_ttl_seconds=1, max_acquire_attempts=5)
        account_id = add_account(pool, 'user_a')

        lease = mgr.acquire('scrape_video', 'task_001')
        assert lease.account_id == account_id

        # TTL=1 秒，等待过期（fakeredis 使用真实时钟）
        time.sleep(1.1)

        # 过期后同一账号可重新 acquire
        lease2 = mgr.acquire('scrape_video', 'task_002')
        assert lease2.account_id == account_id
        assert lease2.lease_id != lease.lease_id

    def test_reclaim_expired_residue(self, pool, mgr, redis_client):
        account_id = add_account(pool, 'user_a')

        lease = mgr.acquire('scrape_video', 'task_001')

        # 构造异常退出残留：key 无 TTL 且 expires_at 已过
        data = json.loads(redis_client.get(f'lease:{lease.lease_id}'))
        data['expires_at'] = (datetime.now() - timedelta(seconds=10)).isoformat()
        redis_client.set(f'lease:{lease.lease_id}', json.dumps(data))
        redis_client.set(f'lease:account:{account_id}', lease.lease_id)

        reclaimed = mgr.reclaim_expired()
        assert reclaimed == 1
        assert redis_client.get(f'lease:{lease.lease_id}') is None
        assert redis_client.get(f'lease:account:{account_id}') is None

        # 清理后该账号可重新 acquire
        lease2 = mgr.acquire('scrape_video', 'task_002')
        assert lease2.account_id == account_id

    def test_reclaim_nothing_when_all_fresh(self, pool, mgr):
        add_account(pool, 'user_a')
        mgr.acquire('scrape_video', 'task_001')
        assert mgr.reclaim_expired() == 0


class TestLogging:
    def test_logs_never_contain_cookies(self, pool, mgr):
        account_id = add_account(pool, 'user_a')

        messages = []
        handler_id = loguru_logger.add(messages.append, level='DEBUG')
        try:
            lease = mgr.acquire('scrape_video', 'task_001')
            mgr.release(lease, success=True)
        finally:
            loguru_logger.remove(handler_id)

        blob = ''.join(str(m) for m in messages)
        # 断言日志不含 cookie 明文/键名
        assert SECRET_COOKIE_VALUE not in blob
        assert 'sessionid' not in blob
        # 结构化字段正常输出
        assert f'account_id={account_id}' in blob
        assert f'lease_id={lease.lease_id}' in blob
        assert 'task_id=task_001' in blob
