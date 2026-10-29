"""Unit tests for CookiePoolManager (src/pools/cookie_pool.py).

- 使用 fakeredis 的 FakeRedis，不访问网络、不启动浏览器、不连真实 Redis；
- 每个测试使用独立的 FakeRedis 实例，无顺序依赖、可重复运行；
- 运行命令：
  .venv/bin/python -m pytest tests/test_cookie_pool.py -p no:cacheprovider -v
"""

import dataclasses
import json
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from fakeredis import FakeRedis

from src.pools.cookie_pool import CookiePoolManager
from src.pools.exceptions import NoAvailableAccountException
from src.pools.models import AccountSession, AccountStatus


# ---------------------------------------------------------------------------
# Helpers & fixtures
# ---------------------------------------------------------------------------

def make_cookies(prefix: str = 'sid') -> dict:
    """构造一份可区分的 cookies 字典。"""
    return {
        f'{prefix}_token': f'{prefix}_value',
        'sessionid': f'{prefix}-abc123',
    }


def set_session_fields(manager: CookiePoolManager, account_id: str, **fields):
    """直接修改内存缓存中的 session 并落盘，用于构造测试状态。"""
    session = manager.accounts[account_id]
    for key, value in fields.items():
        setattr(session, key, value)
    manager._save_account(session)


@pytest.fixture
def fake_redis():
    """每个测试独立的 FakeRedis 实例，保证测试之间零共享。"""
    return FakeRedis(decode_responses=True)


@pytest.fixture
def manager(fake_redis):
    return CookiePoolManager(redis_client=fake_redis)


# ---------------------------------------------------------------------------
# 1. add_account -> get_account 往返
# ---------------------------------------------------------------------------

class TestAddAndGetRoundtrip:

    def test_add_and_get_roundtrip_fields_consistent(self, manager):
        cookies = make_cookies('acct')
        acc_id = manager.add_account(
            username='user_one',
            cookies=cookies,
            proxy_id='proxy_1',
            fingerprint_id='fp_1',
            daily_quota=150,
        )

        assert acc_id.startswith('acc_')
        assert len(acc_id) == len('acc_') + 8  # acc_ + 8 位 hex

        session = manager.get_account()
        assert session.account_id == acc_id
        assert session.username == 'user_one'
        assert session.cookies == cookies
        assert session.proxy_id == 'proxy_1'
        assert session.fingerprint_id == 'fp_1'
        assert session.daily_quota == 150
        assert session.status == AccountStatus.ACTIVE.value

    def test_add_account_defaults(self, manager):
        acc_id = manager.add_account('user_default', make_cookies(), 'proxy_1')
        session = manager.accounts[acc_id]
        assert session.fingerprint_id == ''
        assert session.daily_quota == 200
        assert session.health_score == 1.0
        assert session.daily_request_count == 0


# ---------------------------------------------------------------------------
# 2. 三种选号策略
# ---------------------------------------------------------------------------

class TestSelectionStrategies:

    def test_round_robin_picks_earliest_last_request(self, manager):
        id_a = manager.add_account('user_a', make_cookies('a'), 'proxy_1')
        id_b = manager.add_account('user_b', make_cookies('b'), 'proxy_1')
        id_c = manager.add_account('user_c', make_cookies('c'), 'proxy_1')

        now = datetime.now()
        set_session_fields(manager, id_a, last_request_time=now - timedelta(hours=3))
        set_session_fields(manager, id_b, last_request_time=now - timedelta(hours=2))
        set_session_fields(manager, id_c, last_request_time=now - timedelta(hours=1))

        # last_request_time 最早（None 视为 datetime.min）的先被选中
        assert manager.get_account(strategy='round_robin').account_id == id_a

        # 轮转：把 a 的 last_request_time 更新为最新后，轮到 b
        set_session_fields(manager, id_a, last_request_time=now)
        assert manager.get_account(strategy='round_robin').account_id == id_b

    def test_least_loaded_picks_min_daily_count(self, manager):
        id_a = manager.add_account('user_a', make_cookies('a'), 'proxy_1')
        id_b = manager.add_account('user_b', make_cookies('b'), 'proxy_1')
        id_c = manager.add_account('user_c', make_cookies('c'), 'proxy_1')

        set_session_fields(manager, id_b, daily_request_count=5)
        set_session_fields(manager, id_c, daily_request_count=10)

        assert manager.get_account(strategy='least_loaded').account_id == id_a

        # a 负载升高后，选剩余负载最小的 b
        set_session_fields(manager, id_a, daily_request_count=15)
        assert manager.get_account(strategy='least_loaded').account_id == id_b

    def test_weighted_uses_health_and_remaining_quota(self, manager):
        """权重 = health_score * 剩余配额比例，验证传入 random.choices 的权重值。"""
        id_a = manager.add_account('user_a', make_cookies('a'), 'proxy_1')
        id_b = manager.add_account('user_b', make_cookies('b'), 'proxy_1')

        set_session_fields(manager, id_b, health_score=0.5, daily_request_count=50)

        with patch('src.pools.cookie_pool.random.choices',
                   return_value=[manager.accounts[id_b]]) as mock_choices:
            selected = manager.get_account(strategy='weighted')

        assert selected.account_id == id_b
        candidates = mock_choices.call_args.args[0]
        weights = mock_choices.call_args.kwargs['weights']
        weights_by_id = {
            c.account_id: w for c, w in zip(candidates, weights)
        }
        # a: health 1.0, 用量 0/200 -> weight 1.0
        assert weights_by_id[id_a] == pytest.approx(1.0 * (1 - 0 / 200))
        # b: health 0.5, 用量 50/200 -> weight 0.375
        assert weights_by_id[id_b] == pytest.approx(0.5 * (1 - 50 / 200))

    def test_weighted_zero_total_weight_falls_back_to_random_choice(self, manager):
        """所有候选权重为 0 时回退到 random.choice，仍能返回可用账号。"""
        id_a = manager.add_account('user_a', make_cookies('a'), 'proxy_1')
        id_b = manager.add_account('user_b', make_cookies('b'), 'proxy_1')

        set_session_fields(manager, id_a, health_score=0.0)
        set_session_fields(manager, id_b, health_score=0.0)

        selected = manager.get_account(strategy='weighted')
        assert selected.account_id in {id_a, id_b}

    def test_unknown_strategy_falls_back_to_random(self, manager):
        id_a = manager.add_account('user_a', make_cookies('a'), 'proxy_1')
        id_b = manager.add_account('user_b', make_cookies('b'), 'proxy_1')
        selected = manager.get_account(strategy='no_such_strategy')
        assert selected.account_id in {id_a, id_b}


# ---------------------------------------------------------------------------
# 3. 空池 / 配额用尽 -> NoAvailableAccountException
# ---------------------------------------------------------------------------

class TestNoAvailableAccount:

    def test_empty_pool_raises(self, manager):
        with pytest.raises(NoAvailableAccountException):
            manager.get_account()

    def test_all_quota_exhausted_raises(self, manager):
        acc_id = manager.add_account(
            'user_a', make_cookies(), 'proxy_1', daily_quota=1
        )
        assert manager.get_account().account_id == acc_id
        manager.release_account(acc_id, success=True)  # count=1 >= quota=1

        with pytest.raises(NoAvailableAccountException):
            manager.get_account()

    def test_all_accounts_cooling_raises(self, manager):
        acc_id = manager.add_account('user_a', make_cookies(), 'proxy_1')
        manager.trigger_cooling(acc_id, duration_hours=2)
        with pytest.raises(NoAvailableAccountException):
            manager.get_account()

    def test_all_accounts_suspended_raises(self, manager):
        acc_id = manager.add_account('user_a', make_cookies(), 'proxy_1')
        manager.suspend_account(acc_id, reason='风控')
        with pytest.raises(NoAvailableAccountException):
            manager.get_account()


# ---------------------------------------------------------------------------
# 4. release_account 对 health_score 的影响 + 自动 cooling
# ---------------------------------------------------------------------------

class TestReleaseAccount:

    def test_release_failure_then_success_updates_health(self, manager):
        acc_id = manager.add_account('user_a', make_cookies(), 'proxy_1')

        manager.release_account(acc_id, success=False, response_time=1.0)
        assert manager.accounts[acc_id].health_score == pytest.approx(0.95)

        manager.release_account(acc_id, success=True, response_time=2.0)
        assert manager.accounts[acc_id].health_score == pytest.approx(0.96)

    def test_release_success_caps_health_at_one(self, manager):
        acc_id = manager.add_account('user_a', make_cookies(), 'proxy_1')
        manager.release_account(acc_id, success=True)
        assert manager.accounts[acc_id].health_score == 1.0

    def test_release_failure_floors_health_at_zero(self, manager):
        acc_id = manager.add_account('user_a', make_cookies(), 'proxy_1')
        set_session_fields(manager, acc_id, health_score=0.02)
        manager.release_account(acc_id, success=False)
        assert manager.accounts[acc_id].health_score == 0.0

    def test_release_updates_request_counts_and_metrics(self, manager):
        acc_id = manager.add_account('user_a', make_cookies(), 'proxy_1')

        manager.release_account(acc_id, success=True, response_time=1.0)
        session = manager.accounts[acc_id]
        assert session.daily_request_count == 1
        assert session.total_requests == 1
        assert session.success_requests == 1
        assert session.success_rate == pytest.approx(1.0)
        assert session.avg_response_time == pytest.approx(1.0)
        assert session.last_request_time is not None

        # 指数移动平均：alpha = 0.3
        manager.release_account(acc_id, success=False, response_time=3.0)
        session = manager.accounts[acc_id]
        assert session.daily_request_count == 2
        assert session.total_requests == 2
        expected_ema = 0.3 * 3.0 + 0.7 * 1.0
        assert session.avg_response_time == pytest.approx(expected_ema)

    def test_consecutive_failures_trigger_auto_cooling(self, manager):
        """连续失败使 health < 0.3 时自动进入 cooling（2h）。"""
        acc_id = manager.add_account('user_a', make_cookies(), 'proxy_1')

        attempts = 0
        while not manager.redis.sismember('accounts:cooling', acc_id):
            manager.release_account(acc_id, success=False)
            attempts += 1
            assert attempts <= 25  # 防御性上限，正常 14~15 次即触发

        session = manager.accounts[acc_id]
        assert session.health_score < 0.3
        assert session.status == AccountStatus.COOLING.value
        assert not manager.redis.sismember('accounts:active', acc_id)
        assert not session.is_available()

        # cooling 中的账号不可被选中
        with pytest.raises(NoAvailableAccountException):
            manager.get_account()


# ---------------------------------------------------------------------------
# 5. trigger_cooling / resume_cooling_accounts
# ---------------------------------------------------------------------------

class TestCoolingLifecycle:

    def test_trigger_cooling_moves_account_to_cooling_set(self, manager):
        acc_id = manager.add_account('user_a', make_cookies(), 'proxy_1')

        manager.trigger_cooling(acc_id, duration_hours=2)

        assert manager.redis.sismember('accounts:cooling', acc_id)
        assert not manager.redis.sismember('accounts:active', acc_id)
        session = manager.accounts[acc_id]
        assert session.status == AccountStatus.COOLING.value
        assert session.cooling_until > datetime.now()
        assert session.cooling_until <= datetime.now() + timedelta(hours=2, minutes=1)
        assert not session.is_available()

    def test_resume_cooling_accounts_restores_expired_cooling(self, manager):
        acc_id = manager.add_account('user_a', make_cookies(), 'proxy_1')
        manager.trigger_cooling(acc_id, duration_hours=2)

        # 构造 cooling_until 为过去时间
        set_session_fields(
            manager, acc_id, cooling_until=datetime.now() - timedelta(hours=1)
        )

        manager.resume_cooling_accounts()

        assert manager.redis.sismember('accounts:active', acc_id)
        assert not manager.redis.sismember('accounts:cooling', acc_id)
        session = manager.accounts[acc_id]
        assert session.status == AccountStatus.ACTIVE.value
        assert session.cooling_until is None
        assert session.is_available()
        assert manager.get_account().account_id == acc_id

    def test_resume_skips_accounts_still_cooling(self, manager):
        acc_id = manager.add_account('user_a', make_cookies(), 'proxy_1')
        manager.trigger_cooling(acc_id, duration_hours=2)

        manager.resume_cooling_accounts()  # cooling_until 在未来，不应恢复

        assert manager.redis.sismember('accounts:cooling', acc_id)
        with pytest.raises(NoAvailableAccountException):
            manager.get_account()

    def test_release_on_unknown_account_is_noop(self, manager):
        # 不应抛异常
        manager.release_account('acc_nonexistent', success=True)


# ---------------------------------------------------------------------------
# 6. suspend_account / reset_daily_counts
# ---------------------------------------------------------------------------

class TestSuspendAndReset:

    def test_suspend_account_not_selectable(self, manager):
        acc_id = manager.add_account('user_a', make_cookies(), 'proxy_1')

        manager.suspend_account(acc_id, reason='risk control')

        assert manager.redis.sismember('accounts:suspended', acc_id)
        assert not manager.redis.sismember('accounts:active', acc_id)
        session = manager.accounts[acc_id]
        assert session.status == AccountStatus.SUSPENDED.value
        assert not session.is_available()

        with pytest.raises(NoAvailableAccountException):
            manager.get_account()

    def test_reset_daily_counts_restores_quota(self, manager):
        acc_id = manager.add_account(
            'user_a', make_cookies(), 'proxy_1', daily_quota=1
        )
        assert manager.get_account().account_id == acc_id
        manager.release_account(acc_id, success=True)  # 配额耗尽

        with pytest.raises(NoAvailableAccountException):
            manager.get_account()

        manager.reset_daily_counts()

        assert manager.accounts[acc_id].daily_request_count == 0
        assert manager.get_account().account_id == acc_id

    def test_reset_daily_counts_covers_cooling_accounts(self, manager):
        acc_id = manager.add_account('user_a', make_cookies(), 'proxy_1')
        set_session_fields(manager, acc_id, daily_request_count=10)
        manager.trigger_cooling(acc_id, duration_hours=2)

        manager.reset_daily_counts()

        assert manager.accounts[acc_id].daily_request_count == 0


# ---------------------------------------------------------------------------
# 7. remove_account
# ---------------------------------------------------------------------------

class TestRemoveAccount:

    def test_remove_account(self, manager):
        id_a = manager.add_account('user_a', make_cookies('a'), 'proxy_1')
        id_b = manager.add_account('user_b', make_cookies('b'), 'proxy_1')

        assert manager.remove_account(id_a) is True
        assert not manager.redis.exists(f'account:{id_a}')
        assert not manager.redis.sismember('accounts:active', id_a)
        assert id_a not in manager.accounts

        # 剩余账号仍可选
        assert manager.get_account().account_id == id_b

    def test_remove_nonexistent_account_returns_false(self, manager):
        id_a = manager.add_account('user_a', make_cookies('a'), 'proxy_1')

        assert manager.remove_account('acc_nonexistent') is False

        # 删除一次成功，重复删除已删除的账号返回 False
        assert manager.remove_account(id_a) is True
        assert manager.remove_account(id_a) is False


# ---------------------------------------------------------------------------
# 8. get_pool_stats
# ---------------------------------------------------------------------------

class TestPoolStats:

    def test_pool_stats_counts(self, manager):
        id_a = manager.add_account('user_a', make_cookies('a'), 'proxy_1')
        id_b = manager.add_account('user_b', make_cookies('b'), 'proxy_1')
        id_c = manager.add_account('user_c', make_cookies('c'), 'proxy_1')
        id_d = manager.add_account('user_d', make_cookies('d'), 'proxy_1')
        id_e = manager.add_account('user_e', make_cookies('e'), 'proxy_1')

        manager.trigger_cooling(id_d)
        manager.suspend_account(id_e)

        stats = manager.get_pool_stats()
        assert stats['total_accounts'] == 5
        assert stats['active_accounts'] == 3
        assert stats['cooling_accounts'] == 1
        assert stats['suspended_accounts'] == 1
        # avg_health 只统计 active 集合，3 个账号均 health 1.0
        assert stats['avg_health_score'] == pytest.approx(1.0)
        assert 'timestamp' in stats

    def test_pool_stats_avg_health_reflects_active_only(self, manager):
        id_a = manager.add_account('user_a', make_cookies('a'), 'proxy_1')
        id_b = manager.add_account('user_b', make_cookies('b'), 'proxy_1')

        set_session_fields(manager, id_a, health_score=0.8)
        # id_b 进入 cooling 后不参与 avg_health 计算
        manager.trigger_cooling(id_b)

        stats = manager.get_pool_stats()
        assert stats['active_accounts'] == 1
        assert stats['avg_health_score'] == pytest.approx(0.8)

    def test_pool_stats_empty_pool(self, manager):
        stats = manager.get_pool_stats()
        assert stats['total_accounts'] == 0
        assert stats['active_accounts'] == 0
        assert stats['avg_health_score'] == 0


# ---------------------------------------------------------------------------
# 9. 序列化持久化：新实例从同一 Redis 读回
# ---------------------------------------------------------------------------

class TestPersistence:

    def test_cookies_stored_as_json_string(self, manager, fake_redis):
        cookies = make_cookies('json')
        acc_id = manager.add_account('json_user', cookies, 'proxy_1')

        raw = fake_redis.hget(f'account:{acc_id}', 'cookies')
        assert isinstance(raw, str)
        assert json.loads(raw) == cookies

    def test_new_manager_reads_back_from_same_redis(self, manager, fake_redis):
        cookies = make_cookies('persist')
        acc_id = manager.add_account(
            username='persist_user',
            cookies=cookies,
            proxy_id='proxy_9',
            fingerprint_id='fp_9',
            daily_quota=77,
        )

        # 同一 FakeRedis 新建实例，构造时应从 Redis 加载
        manager2 = CookiePoolManager(redis_client=fake_redis)
        assert acc_id in manager2.accounts

        session = manager2.get_account()
        assert session.account_id == acc_id
        assert session.username == 'persist_user'
        assert session.cookies == cookies
        assert session.proxy_id == 'proxy_9'
        assert session.fingerprint_id == 'fp_9'
        assert session.daily_quota == 77

    def test_new_manager_sees_cooling_and_suspended_state(self, manager, fake_redis):
        id_a = manager.add_account('user_a', make_cookies('a'), 'proxy_1')
        id_b = manager.add_account('user_b', make_cookies('b'), 'proxy_1')
        manager.trigger_cooling(id_a)
        manager.suspend_account(id_b)

        manager2 = CookiePoolManager(redis_client=fake_redis)
        assert manager2.accounts[id_a].status == AccountStatus.COOLING.value
        assert manager2.accounts[id_b].status == AccountStatus.SUSPENDED.value

        stats = manager2.get_pool_stats()
        assert stats['cooling_accounts'] == 1
        assert stats['suspended_accounts'] == 1


# ---------------------------------------------------------------------------
# 10. to_dict / from_dict 序列化边界
# ---------------------------------------------------------------------------

class TestSerializationCompat:

    def test_to_dict_none_datetime_becomes_empty_string(self):
        """None 的 datetime 字段落库为 ''（Redis HSET 不接受 None）。"""
        session = AccountSession(
            account_id='acc_x', username='u', cookies={'k': 'v'}, proxy_id='p'
        )
        data = session.to_dict()
        for key in ('last_request_time', 'last_waf_time', 'cooling_until'):
            assert data[key] == ''

    def test_roundtrip_preserves_all_fields(self):
        session = AccountSession(
            account_id='acc_x', username='u', cookies={'k': 'v'}, proxy_id='p',
            fingerprint_id='fp_1', daily_quota=150,
        )
        data = session.to_dict()

        # 不丢字段：to_dict 输出覆盖全部 dataclass 字段
        assert set(data.keys()) == {
            f.name for f in dataclasses.fields(AccountSession)
        }

        restored = AccountSession.from_dict(dict(data))  # 传副本防 mutate
        assert restored.account_id == 'acc_x'
        assert restored.username == 'u'
        assert restored.cookies == {'k': 'v'}
        assert restored.proxy_id == 'p'
        assert restored.fingerprint_id == 'fp_1'
        assert restored.daily_quota == 150
        assert restored.status == AccountStatus.ACTIVE.value

        # None datetime 字段往返后保持 falsy（当前实现读回 ''，见报告说明）
        assert not restored.last_request_time
        assert not restored.last_waf_time
        assert not restored.cooling_until

    def test_from_dict_parses_datetime_strings(self):
        now = datetime.now().replace(microsecond=0)  # isoformat 秒级精度往返
        session = AccountSession(
            account_id='acc_x', username='u', cookies={}, proxy_id='p',
            last_request_time=now,
        )
        data = session.to_dict()
        restored = AccountSession.from_dict(dict(data))
        assert isinstance(restored.last_request_time, datetime)
        assert restored.last_request_time == now

    def test_full_pool_roundtrip_through_redis(self, manager, fake_redis):
        """经 Redis hash 的完整往返：int/float/datetime 字段类型还原正确。"""
        cookies = make_cookies('rt')
        acc_id = manager.add_account('rt_user', cookies, 'proxy_1')
        manager.release_account(acc_id, success=True, response_time=1.5)

        manager2 = CookiePoolManager(redis_client=fake_redis)
        session = manager2.accounts[acc_id]
        assert isinstance(session.daily_request_count, int)
        assert session.daily_request_count == 1
        assert isinstance(session.health_score, float)
        assert session.health_score == pytest.approx(1.0)
        assert isinstance(session.last_request_time, datetime)
        assert session.cookies == cookies
        assert session.avg_response_time == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# 11. 并发语义现状（文档化，不修产品代码）
# ---------------------------------------------------------------------------

class TestNoLeaseSemantics:

    def test_get_account_does_not_mark_occupancy(self, manager, fake_redis):
        """文档化现状：get_account 不做占用标记（无租约）。

        现状（产品代码不修改，仅固化行为）：
        - get_account 选号后不写入任何 in-use/lease 标记，也不递增计数；
          请求方须在用完后调用 release_account 上报结果；
        - 因此两次 get_account 之间账号状态完全不变，并发调用方
          完全可能拿到同一个账号；
        - weighted 策略在所有账号权重相同时等价于随机选择，
          不保证两次调用返回不同账号。
        """
        id_a = manager.add_account('user_a', make_cookies('a'), 'proxy_1')
        id_b = manager.add_account('user_b', make_cookies('b'), 'proxy_1')

        first = manager.get_account()
        snapshot_after_first = {
            s.account_id: (s.daily_request_count, s.total_requests, s.status)
            for s in manager.accounts.values()
        }

        second = manager.get_account()
        snapshot_after_second = {
            s.account_id: (s.daily_request_count, s.total_requests, s.status)
            for s in manager.accounts.values()
        }

        # 无占用标记：第二次选号不改变任何账号的计数或状态
        assert snapshot_after_first == snapshot_after_second
        assert first.daily_request_count == 0
        assert second.daily_request_count == 0

        # Redis 中不存在任何 lease/in-use 类型的 key
        assert fake_redis.keys('*lease*') == []
        assert fake_redis.keys('*in_use*') == []
        assert fake_redis.keys('*occup*') == []

        # 两次返回的都是有效的可用账号（同一账号是合法结果）
        assert first.account_id in {id_a, id_b}
        assert second.account_id in {id_a, id_b}
