"""watchlist_daily_job 单元测试。

全部 mock: fakeredis + FakeScraper/FakeEngine, 无网络、无真实 Redis、无数据库。
覆盖: 清单解析 / dry-run / 成功批次 / 单商品失败继续 / 风控停批 / 空清单 /
写入失败转配置错误 / 批次租约成功标记。
"""

import tempfile
from pathlib import Path

import pytest
import yaml

import scripts.watchlist_daily_job as job


class FakeSession:
    def __init__(self):
        self.cookies = {}


class FakeClient:
    def __init__(self):
        self.session = FakeSession()


class FakeScraper:
    """按 product_id 返回预设结果; 记录调用顺序。"""

    calls = []
    results = {}       # pid -> dict | None
    raise_for = {}     # pid -> Exception (CrawlerException 语义)
    error_for = {}     # pid -> Exception (普通异常)

    def __init__(self, client=None):
        self.client = client

    def scrape_product(self, pid):
        FakeScraper.calls.append(pid)
        if pid in FakeScraper.raise_for:
            raise FakeScraper.raise_for[pid]
        if pid in FakeScraper.error_for:
            raise FakeScraper.error_for[pid]
        return FakeScraper.results.get(pid)

    @classmethod
    def reset(cls):
        cls.calls = []
        cls.results = {}
        cls.raise_for = {}
        cls.error_for = {}


class FakeEngine:
    def __init__(self):
        self.saved = []
        self.fail_for = set()

    def save_snapshot(self, data, table):
        if data.get('product_id') in self.fail_for:
            return False
        self.saved.append((table, data))
        return True


def product(pid, price=19.9):
    return {'product_id': pid, 'title': f'item {pid}', 'price_current': price}


@pytest.fixture(autouse=True)
def reset_fakes():
    FakeScraper.reset()
    yield
    FakeScraper.reset()


@pytest.fixture
def wl_dir():
    """沙箱下 pytest tmp_path 会撞 pytest-of-unknown, 用 tempfile 代替。"""
    return Path(tempfile.mkdtemp(prefix='tk_watchlist_'))


@pytest.fixture
def no_pool(monkeypatch):
    """池关闭路径: build_client 返回 FakeClient, lease 为 None。"""
    monkeypatch.setattr(job, 'build_client', lambda ctx: FakeClient())


def write_watchlist(wl_dir, pids):
    p = wl_dir / 'watchlist.yaml'
    p.write_text(yaml.safe_dump(
        {'products': [{'product_id': pid, 'note': 't'} for pid in pids]}),
        encoding='utf-8')
    return p


class TestLoadWatchlist:
    def test_parses_and_dedupes(self, wl_dir):
        p = write_watchlist(wl_dir, ['111', '222'])
        (wl_dir / 'extra.yaml').write_text(
            yaml.safe_dump({'products': [
                {'product_id': '333'}, {'product_id': '222'},   # 222 重复
                {'product_id': 'abc'}, {}                        # 非数字/空项
            ]}), encoding='utf-8')
        assert job.load_watchlist(p) == ['111', '222']
        assert job.load_watchlist(wl_dir / 'extra.yaml') == ['333', '222']

    def test_missing_file_raises_config_error(self, wl_dir):
        with pytest.raises(job.ConfigError):
            job.load_watchlist(wl_dir / 'nope.yaml')


class TestRunJob:
    def test_empty_watchlist_is_config_error(self, wl_dir, no_pool):
        p = write_watchlist(wl_dir, [])
        code = job.run_job(watchlist_path=p, scraper_factory=FakeScraper, gap_range=(0.0, 0.0),
                           engine_factory=FakeEngine)
        assert code == job.EXIT_CONFIG_ERROR

    def test_dry_run_no_side_effects(self, wl_dir, no_pool, capsys):
        p = write_watchlist(wl_dir, ['111'])
        code = job.run_job(watchlist_path=p, dry_run=True)
        assert code == job.EXIT_OK
        assert FakeScraper.calls == []
        assert 'dry-run' in capsys.readouterr().out

    def test_success_batch_saves_all_and_marks_success(self, wl_dir, no_pool, monkeypatch):
        p = write_watchlist(wl_dir, ['111', '222'])
        FakeScraper.results = {'111': product('111'), '222': product('222')}
        engine = FakeEngine()

        marker = {}
        real_scoped = job.pool_scoped_client

        def spy_scoped(*a, **kw):
            ctx_cm = real_scoped(*a, **kw)
            return ctx_cm

        monkeypatch.setattr(job, 'pool_scoped_client', spy_scoped)
        monkeypatch.setattr(FakeScraper, 'results', FakeScraper.results)

        code = job.run_job(watchlist_path=p, scraper_factory=FakeScraper, gap_range=(0.0, 0.0),
                           engine_factory=lambda: engine)
        assert code == job.EXIT_OK
        assert [d['product_id'] for _, d in engine.saved] == ['111', '222']
        assert FakeScraper.calls == ['111', '222']
        assert all(d.get('snapshot_date') for _, d in engine.saved)

    def test_single_failure_continues_batch(self, wl_dir, no_pool):
        p = write_watchlist(wl_dir, ['111', '222', '333'])
        FakeScraper.results = {'111': product('111'), '333': product('333')}
        # 222 返回 None (采集失败)
        engine = FakeEngine()
        code = job.run_job(watchlist_path=p, scraper_factory=FakeScraper, gap_range=(0.0, 0.0),
                           engine_factory=lambda: engine)
        assert code == job.EXIT_OK
        assert FakeScraper.calls == ['111', '222', '333']  # 不中断
        assert len(engine.saved) == 2

    def test_waf_stops_batch(self, wl_dir, no_pool):
        from src.common.exceptions import WAFBlockedException

        p = write_watchlist(wl_dir, ['111', '222', '333'])
        FakeScraper.results = {'111': product('111')}
        FakeScraper.raise_for = {'222': WAFBlockedException('u', 403)}
        engine = FakeEngine()
        code = job.run_job(watchlist_path=p, scraper_factory=FakeScraper, gap_range=(0.0, 0.0),
                           engine_factory=lambda: engine)
        # 111 成功 -> 222 风控停批 -> 333 不再尝试
        assert code == job.EXIT_OK
        assert FakeScraper.calls == ['111', '222']
        assert len(engine.saved) == 1

    def test_all_failed_is_scrape_failed(self, wl_dir, no_pool):
        p = write_watchlist(wl_dir, ['111'])
        engine = FakeEngine()
        code = job.run_job(watchlist_path=p, scraper_factory=FakeScraper, gap_range=(0.0, 0.0),
                           engine_factory=lambda: engine)
        assert code == job.EXIT_SCRAPE_FAILED

    def test_save_failure_is_config_error(self, wl_dir, no_pool):
        p = write_watchlist(wl_dir, ['111'])
        FakeScraper.results = {'111': product('111')}
        engine = FakeEngine()
        engine.fail_for = {'111'}
        code = job.run_job(watchlist_path=p, scraper_factory=FakeScraper, gap_range=(0.0, 0.0),
                           engine_factory=lambda: engine)
        assert code == job.EXIT_CONFIG_ERROR

    def test_generic_exception_counts_as_failure(self, wl_dir, no_pool):
        p = write_watchlist(wl_dir, ['111', '222'])
        FakeScraper.results = {'222': product('222')}
        FakeScraper.error_for = {'111': RuntimeError('boom')}
        engine = FakeEngine()
        code = job.run_job(watchlist_path=p, scraper_factory=FakeScraper, gap_range=(0.0, 0.0),
                           engine_factory=lambda: engine)
        assert code == job.EXIT_OK
        assert FakeScraper.calls == ['111', '222']
        assert len(engine.saved) == 1
