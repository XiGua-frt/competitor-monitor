"""tests for CookiePool — profile_key 稳定性 / acquire 轮询 / 冷却 / 持久化容错。"""

import json

import pytest

from src.pools.cookie_pool import (
    CookiePool,
    SessionProfile,
    compute_profile_key,
)


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make_profiles(n=3, marketplace='www.amazon.com'):
    return [SessionProfile(marketplace=marketplace,
                           delivery_postcode=f'1000{i}',
                           currency='USD', language='en-US')
            for i in range(n)]


class TestProfileKey:
    def test_stable_across_calls(self):
        k1 = compute_profile_key('www.amazon.com', '10001', 'USD', 'en-US')
        k2 = compute_profile_key('www.amazon.com', '10001', 'USD', 'en-US')
        assert k1 == k2
        assert len(k1) == 24

    def test_different_inputs_different_keys(self):
        assert (compute_profile_key('www.amazon.com', '10001', 'USD', 'en-US')
                != compute_profile_key('www.amazon.com', '10002', 'USD',
                                       'en-US'))

    def test_normalization_insensitive_to_case(self):
        assert (compute_profile_key('Amazon.COM', '10001', 'usd', 'en-US')
                == compute_profile_key('amazon.com', '10001', 'USD',
                                       'en-US'))

    def test_dataclass_auto_computes_key(self):
        p = SessionProfile(marketplace='www.amazon.com')
        assert p.profile_key == compute_profile_key(
            'www.amazon.com', '', 'USD', 'en-US')

    def test_explicit_key_preserved(self):
        p = SessionProfile(marketplace='m', profile_key='custom123')
        assert p.profile_key == 'custom123'


class TestAcquire:
    def test_round_robin(self):
        pool = CookiePool(make_profiles(), storage_path='/tmp/x.json',
                          clock=FakeClock())
        keys = [pool.acquire().profile.profile_key for _ in range(3)]
        assert len(set(keys)) == 3  # 三个不同 profile 各一次

    def test_acquire_specific_key(self):
        profiles = make_profiles()
        pool = CookiePool(profiles, storage_path='/tmp/x.json',
                          clock=FakeClock())
        entry = pool.acquire(profiles[1].profile_key)
        assert entry.profile.profile_key == profiles[1].profile_key

    def test_acquire_unknown_key_returns_none(self):
        pool = CookiePool(make_profiles(), storage_path='/tmp/x.json',
                          clock=FakeClock())
        assert pool.acquire('nonexistent') is None

    def test_single_profile_repeats(self):
        pool = CookiePool(make_profiles(1), storage_path='/tmp/x.json',
                          clock=FakeClock())
        e1 = pool.acquire()
        e2 = pool.acquire()
        assert e1.profile.profile_key == e2.profile.profile_key

    def test_empty_profiles_rejected(self):
        with pytest.raises(ValueError):
            CookiePool([], storage_path='/tmp/x.json')

    def test_lease_returns_cookies_copy(self):
        profiles = make_profiles(1)
        pool = CookiePool(profiles, storage_path='/tmp/x.json',
                          clock=FakeClock())
        pool.set_cookies(profiles[0].profile_key, {'session-id': 'abc'})
        entry = pool.acquire()
        assert entry.cookies == {'session-id': 'abc'}
        # 返回的是副本，改动不影响池内状态
        entry.cookies['x'] = 'y'
        assert pool.acquire().cookies == {'session-id': 'abc'}


class TestCooldown:
    def test_report_failure_cooldowns_profile(self):
        clock = FakeClock()
        profiles = make_profiles(2)
        pool = CookiePool(profiles, storage_path='/tmp/x.json', clock=clock)
        pool.report_failure(profiles[0].profile_key)
        # p0 冷却，acquire 只返回 p1
        keys = [pool.acquire().profile.profile_key for _ in range(4)]
        assert all(k == profiles[1].profile_key for k in keys)

    def test_cooldown_expires(self):
        clock = FakeClock()
        profiles = make_profiles(1)
        pool = CookiePool(profiles, storage_path='/tmp/x.json', clock=clock)
        pool.report_failure(profiles[0].profile_key)
        assert pool.acquire() is None
        clock.advance(30 * 60 + 1)
        assert pool.acquire() is not None

    def test_report_success_clears_cooldown(self):
        clock = FakeClock()
        profiles = make_profiles(1)
        pool = CookiePool(profiles, storage_path='/tmp/x.json', clock=clock)
        pool.report_failure(profiles[0].profile_key)
        pool.report_success(profiles[0].profile_key)
        assert pool.acquire() is not None

    def test_default_cooldown_30min(self):
        pool = CookiePool(make_profiles(1), storage_path='/tmp/x.json',
                          clock=FakeClock())
        assert pool.cooldown_seconds == 30 * 60

    def test_all_cooldown_returns_none(self):
        clock = FakeClock()
        profiles = make_profiles(2)
        pool = CookiePool(profiles, storage_path='/tmp/x.json', clock=clock)
        for p in profiles:
            pool.report_failure(p.profile_key)
        assert pool.acquire() is None

    def test_failure_unknown_key_no_crash(self):
        pool = CookiePool(make_profiles(), storage_path='/tmp/x.json',
                          clock=FakeClock())
        pool.report_failure('bogus')  # 仅告警
        assert pool.healthy_count() == 3


class TestPersistence:
    def test_save_load_roundtrip(self, tmp_path):
        path = tmp_path / 'pool.json'
        profiles = make_profiles(2)
        clock = FakeClock()
        pool = CookiePool(profiles, storage_path=path, clock=clock)
        pool.set_cookies(profiles[0].profile_key, {'session-id': 's1',
                                                   'ubid-main': 'u1'})
        pool.set_cookies(profiles[1].profile_key, {'session-id': 's2'})
        pool.report_failure(profiles[1].profile_key)
        pool.save()

        clock.advance(10)  # 冷却剩余 30min-10s
        pool2 = CookiePool(profiles, storage_path=path, clock=clock)
        entry = pool2.acquire()
        assert entry.profile.profile_key == profiles[0].profile_key
        assert entry.cookies == {'session-id': 's1', 'ubid-main': 'u1'}
        assert pool2.healthy_count() == 1  # p1 冷却被恢复

    def test_corrupted_file_tolerated(self, tmp_path):
        path = tmp_path / 'pool.json'
        path.write_text('{not valid json!!', encoding='utf-8')
        profiles = make_profiles(1)
        pool = CookiePool(profiles, storage_path=path, clock=FakeClock())
        assert pool.healthy_count() == 1  # 冷启动，不抛异常

    def test_missing_file_cold_start(self, tmp_path):
        pool = CookiePool(make_profiles(1),
                          storage_path=tmp_path / 'nope.json',
                          clock=FakeClock())
        assert pool.acquire() is not None

    def test_unexpected_format_ignored(self, tmp_path):
        path = tmp_path / 'pool.json'
        path.write_text(json.dumps([1, 2, 3]), encoding='utf-8')
        pool = CookiePool(make_profiles(1), storage_path=path,
                          clock=FakeClock())
        assert pool.healthy_count() == 1

    def test_save_atomic(self, tmp_path):
        """save 走 tmp 文件 + replace，不留 .tmp 残留。"""
        path = tmp_path / 'pool.json'
        pool = CookiePool(make_profiles(1), storage_path=path,
                          clock=FakeClock())
        pool.save()
        assert path.exists()
        assert not path.with_suffix('.json.tmp').exists()


class TestLeaseContext:
    def test_lease_success_reports(self):
        profiles = make_profiles(1)
        pool = CookiePool(profiles, storage_path='/tmp/x.json',
                          clock=FakeClock())
        with pool.lease() as entry:
            assert entry is not None
        assert pool.healthy_count() == 1

    def test_lease_exception_reports_failure(self):
        profiles = make_profiles(1)
        pool = CookiePool(profiles, storage_path='/tmp/x.json',
                          clock=FakeClock())
        with pytest.raises(RuntimeError):
            with pool.lease():
                raise RuntimeError('scrape failed')
        assert pool.acquire() is None  # 进入冷却
