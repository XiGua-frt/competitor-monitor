"""tests for FingerprintPool — rotate 轮询 / ban 冷却 / fixed 模式。"""

import pytest

from src.pools.fingerprint_pool import (
    DEFAULT_FINGERPRINT,
    FINGERPRINTS,
    FingerprintPool,
)


class FakeClock:
    def __init__(self, start=0.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TestFixedMode:
    def test_fixed_returns_constant(self):
        pool = FingerprintPool(strategy='fixed')
        assert pool.acquire() == 'chrome131'
        assert pool.acquire() == 'chrome131'

    def test_default_strategy_is_fixed(self):
        assert FingerprintPool().strategy == 'fixed'
        assert FingerprintPool().acquire() == DEFAULT_FINGERPRINT

    def test_fixed_ignores_ban(self):
        pool = FingerprintPool(strategy='fixed')
        pool.report_ban('chrome131')
        assert pool.acquire() == 'chrome131'  # fixed 模式不受 ban 影响


class TestRotateMode:
    def test_rotate_cycles_through_all(self):
        pool = FingerprintPool(strategy='rotate')
        seen = [pool.acquire() for _ in range(len(FINGERPRINTS))]
        assert seen == FINGERPRINTS  # 严格轮询一遍

    def test_rotate_wraps_around(self):
        pool = FingerprintPool(strategy='rotate')
        n = len(FINGERPRINTS)
        first = pool.acquire()
        for _ in range(n - 1):
            pool.acquire()
        assert pool.acquire() == first

    def test_ban_skips_fingerprint(self):
        pool = FingerprintPool(strategy='rotate', clock=FakeClock())
        pool.report_ban('chrome131')
        seen = [pool.acquire() for _ in range(len(FINGERPRINTS) - 1)]
        assert 'chrome131' not in seen

    def test_ban_expires_after_1h(self):
        clock = FakeClock()
        pool = FingerprintPool(strategy='rotate', clock=clock)
        pool.report_ban('chrome131')
        clock.advance(60 * 60 + 1)
        assert pool.healthy_count() == len(FINGERPRINTS)
        # 恢复后 chrome131 重新参与轮询
        seen = [pool.acquire() for _ in range(len(FINGERPRINTS))]
        assert 'chrome131' in seen

    def test_all_banned_falls_back_to_default(self):
        clock = FakeClock()
        pool = FingerprintPool(strategy='rotate', clock=clock)
        for fp in FINGERPRINTS:
            pool.report_ban(fp)
        assert pool.acquire() == DEFAULT_FINGERPRINT

    def test_repeated_ban_resets_cooldown(self):
        clock = FakeClock()
        pool = FingerprintPool(strategy='rotate', clock=clock)
        pool.report_ban('chrome131')
        clock.advance(30 * 60)
        pool.report_ban('chrome131')  # 滑动重置
        clock.advance(31 * 60)  # 距第一次 61min，距第二次 31min
        assert pool.healthy_count() == len(FINGERPRINTS) - 1

    def test_report_success_clears_ban(self):
        pool = FingerprintPool(strategy='rotate')
        pool.report_ban('chrome124')
        pool.report_success('chrome124')
        assert pool.healthy_count() == len(FINGERPRINTS)

    def test_ban_unknown_fingerprint_no_crash(self):
        pool = FingerprintPool(strategy='rotate')
        pool.report_ban('netscape4')  # 仅告警
        assert pool.healthy_count() == len(FINGERPRINTS)


class TestConfig:
    def test_fingerprints_are_known_curl_cffi_values(self):
        # curl_cffi 0.16.3 BrowserTypeLiteral 实测子集（与交付报告一致）
        assert set(FINGERPRINTS) == {
            'chrome131', 'chrome124', 'chrome120', 'chrome116',
            'edge101', 'safari17_0'}

    def test_custom_list(self):
        pool = FingerprintPool(fingerprints=['chrome120', 'edge101'],
                               strategy='rotate')
        assert [pool.acquire(), pool.acquire()] == ['chrome120', 'edge101']

    def test_empty_list_rejected(self):
        with pytest.raises(ValueError):
            FingerprintPool(fingerprints=[])
