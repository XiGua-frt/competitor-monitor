"""hashtag 快照管线 e2e 测试 (Agent S)。

覆盖:
    - scrape_hashtag_api (mock) → save_snapshot 两期写入 → views_total
      差分计算 (SQLite 内存库, 参考 tests/test_metrics.py 模式);
    - scripts/hashtag_snapshot_job.py 的参数解析 / 标签间随机间隔 /
      dry-run 路径 / 退出码 0/1/2;
    - tasks.yaml 新任务 scrape_hashtag_snapshots 定义解析
      (interval/priority/payload);
    - main.py scheduler handler 与 job 入口函数的接线。

纪律: 全 mock, 不发真实 TikTok 请求、不依赖真实 PostgreSQL;
真实 PG 门控用例 (TEST_POSTGRES_URL, 默认 skip) 单列。
"""

import os
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

import pytest
import yaml
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402
import scripts.hashtag_snapshot_job as job_mod  # noqa: E402
from scripts.hashtag_snapshot_job import (  # noqa: E402
    EXIT_CONFIG_ERROR,
    EXIT_OK,
    EXIT_SCRAPE_FAILED,
    get_previous_snapshot,
    parse_tags,
    run_job,
)
from src.common.exceptions import RateLimitException  # noqa: E402
from src.database import HashtagSnapshot, SnapshotDiffEngine  # noqa: E402

TASKS_YAML = Path(__file__).resolve().parents[1] / "src" / "scheduler" / "config" / "tasks.yaml"
TEST_PG_URL = os.getenv("TEST_POSTGRES_URL")

D0 = date(2026, 10, 1)
D1 = date(2026, 10, 7)


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------
class FakeScraper:
    """按预置脚本依次返回快照 dict / None / 抛异常 (不真实请求)。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def scrape_hashtag_api(self, tag_name):
        self.calls.append(tag_name)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def hashtag_data(tag, day, views):
    """构造 scrape_hashtag_api 返回契约的快照 dict。"""
    return {"tag_name": tag, "views_total": views, "snapshot_date": day}


@pytest.fixture
def no_check_db(monkeypatch):
    """SQLite 测试不探测真实 PG: 置空 job 的 check_db。"""
    monkeypatch.setattr(job_mod, "check_db", lambda: None)


@pytest.fixture
def sqlite_factory():
    """SQLite 内存库 session 工厂 + 已建 4 张快照表。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from src.database.models import Base
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    yield factory
    engine.dispose()


@pytest.fixture
def tmp_dir():
    """独立临时目录 (不依赖 pytest 内建 tmp_path, 规避环境权限问题)。"""
    d = Path(tempfile.mkdtemp(prefix="tk_haspipeline_test_"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def fetch_rows(factory, tag=None):
    """读取 hashtag_snapshots 全部/指定标签记录 (按标签+日期排序)。"""
    from src.database.models import HashtagSnapshot as HS
    with factory() as session:
        stmt = select(HS).order_by(HS.tag_name, HS.snapshot_date)
        if tag is not None:
            stmt = stmt.where(HS.tag_name == tag)
        return [
            {"tag_name": r.tag_name, "snapshot_date": r.snapshot_date,
             "views_total": r.views_total}
            for r in session.execute(stmt).scalars().all()
        ]


# ---------------------------------------------------------------------------
# 两期快照 → 差分 (SQLite)
# ---------------------------------------------------------------------------
class TestPipelineDiff:
    def test_two_phase_snapshot_and_growth(self, sqlite_factory, no_check_db,
                                           capsys):
        """两期快照: 写入两次 + 差分 views_growth=500 (+50%)。"""
        # 第一期: 无历史快照, 差分不可得
        scraper1 = FakeScraper([hashtag_data("taga", D0, 1000)])
        assert run_job(tags=["taga"], scraper=scraper1,
                       session_factory=sqlite_factory,
                       sleep_fn=lambda s: None, gap_range=(0, 0)) == EXIT_OK
        out1 = capsys.readouterr().out
        assert "views_total=1000" in out1
        assert "差分不可得" in out1

        # 第二期: 与上次快照差分
        scraper2 = FakeScraper([hashtag_data("taga", D1, 1500)])
        assert run_job(tags=["taga"], scraper=scraper2,
                       session_factory=sqlite_factory,
                       sleep_fn=lambda s: None, gap_range=(0, 0)) == EXIT_OK
        out2 = capsys.readouterr().out
        assert "views_total=1500" in out2
        assert "views_growth=500" in out2
        assert "(+50.00%)" in out2

        rows = fetch_rows(sqlite_factory, "taga")
        assert [(r["snapshot_date"], r["views_total"]) for r in rows] == [
            (D0, 1000), (D1, 1500),
        ]

    def test_zero_past_views_rate_none(self, sqlite_factory, no_check_db,
                                       capsys):
        """上期 views_total=0 → 增量可得, 增长率 None (NULLIF 语义)。"""
        scraper1 = FakeScraper([hashtag_data("tagz", D0, 0)])
        run_job(tags=["tagz"], scraper=scraper1,
                session_factory=sqlite_factory,
                sleep_fn=lambda s: None, gap_range=(0, 0))
        capsys.readouterr()

        scraper2 = FakeScraper([hashtag_data("tagz", D1, 300)])
        assert run_job(tags=["tagz"], scraper=scraper2,
                       session_factory=sqlite_factory,
                       sleep_fn=lambda s: None, gap_range=(0, 0)) == EXIT_OK
        out = capsys.readouterr().out
        assert "views_growth=300" in out
        assert "N/A (上期为 0)" in out

    def test_same_day_rerun_no_diff(self, sqlite_factory, no_check_db,
                                    capsys):
        """同日重跑: upsert 幂等, 差分不可得 (基准为写入前的同日记录)。"""
        data = hashtag_data("tagsame", D1, 1200)
        for _ in range(2):
            scraper = FakeScraper([dict(data)])
            assert run_job(tags=["tagsame"], scraper=scraper,
                           session_factory=sqlite_factory,
                           sleep_fn=lambda s: None, gap_range=(0, 0)) == EXIT_OK
            capsys.readouterr()
        out = capsys.readouterr().out
        assert "views_growth" not in out
        assert fetch_rows(sqlite_factory, "tagsame") and \
            len(fetch_rows(sqlite_factory, "tagsame")) == 1

    def test_upsert_same_date_overwrites(self, sqlite_factory, no_check_db):
        """同日重跑按 (tag_name, snapshot_date) upsert, 不产生重复行。"""
        for views in (100, 200):
            scraper = FakeScraper([hashtag_data("tagu", D1, views)])
            run_job(tags=["tagu"], scraper=scraper,
                    session_factory=sqlite_factory,
                    sleep_fn=lambda s: None, gap_range=(0, 0))
        rows = fetch_rows(sqlite_factory, "tagu")
        assert len(rows) == 1 and rows[0]["views_total"] == 200

    def test_get_previous_snapshot_latest_by_date(self, sqlite_factory):
        """get_previous_snapshot 按 tag_name 维度取日期最大的记录。"""
        engine_dict = {"views_total": 10, "snapshot_date": D0}
        with sqlite_factory() as session:
            session.add(HashtagSnapshot(**{"tag_name": "tp", **engine_dict}))
            session.add(HashtagSnapshot(tag_name="tp", snapshot_date=D1,
                                        views_total=20))
            session.add(HashtagSnapshot(tag_name="other", snapshot_date=D1,
                                        views_total=99))
            session.commit()
        prev = get_previous_snapshot(sqlite_factory, "tp")
        assert prev == {"views_total": 20, "snapshot_date": D1}
        assert get_previous_snapshot(sqlite_factory, "ghost") == {}


# ---------------------------------------------------------------------------
# job 脚本行为: 参数 / 间隔 / dry-run / 退出码
# ---------------------------------------------------------------------------
class TestJobBehavior:
    def test_parse_tags(self):
        assert parse_tags("#a, b ,,a") == ["a", "b"]
        assert parse_tags("x") == ["x"]
        assert parse_tags("") == []
        assert parse_tags(None) == []

    def test_dry_run_prints_plan_no_requests(self, capsys):
        """--dry-run: 只打印计划; 不构造 scraper、不连库、不 sleep。"""
        sleeps = []
        code = run_job(dry_run=True, sleep_fn=sleeps.append)
        assert code == EXIT_OK
        out = capsys.readouterr().out
        assert "[dry-run]" in out
        assert "tiktokmademebuyit" in out and "webdev" in out
        assert "hashtag_snapshots" in out
        assert sleeps == []

    def test_default_tags_when_none(self, no_check_db, sqlite_factory,
                                    capsys):
        """tags=None → 默认 tiktokmademebuyit,webdev, 按序逐标签调用。"""
        script = [
            hashtag_data("tiktokmademebuyit", D1, 111),
            hashtag_data("webdev", D1, 222),
        ]
        scraper = FakeScraper(script)
        assert run_job(scraper=scraper, session_factory=sqlite_factory,
                       sleep_fn=lambda s: None, gap_range=(0, 0)) == EXIT_OK
        assert scraper.calls == ["tiktokmademebuyit", "webdev"]

    def test_empty_tags_exit_2(self, no_check_db):
        assert run_job(tags=[], dry_run=False) == EXIT_CONFIG_ERROR

    def test_sleep_between_tags_only_with_gap_bounds(self, monkeypatch,
                                                     no_check_db,
                                                     sqlite_factory):
        """真实节奏: 标签间 sleep 两次 (最后一次不睡), 边界 8-12s。"""
        bounds = []

        def fake_uniform(lo, hi):
            bounds.append((lo, hi))
            return 0.0  # 不真等

        monkeypatch.setattr(job_mod.random, "uniform", fake_uniform)
        sleeps = []
        script = [hashtag_data(f"t{i}", D1, 10 * i) for i in range(3)]
        scraper = FakeScraper(script)
        code = run_job(tags=["t0", "t1", "t2"], scraper=scraper,
                       session_factory=sqlite_factory,
                       sleep_fn=sleeps.append)
        assert code == EXIT_OK
        assert sleeps == [0.0, 0.0]
        assert bounds == [(8.0, 12.0), (8.0, 12.0)]

    def test_all_tags_fail_exit_1(self, no_check_db, sqlite_factory):
        """全部标签返回 None → 退出码 1。"""
        scraper = FakeScraper([None, None])
        code = run_job(tags=["a", "b"], scraper=scraper,
                       session_factory=sqlite_factory,
                       sleep_fn=lambda s: None, gap_range=(0, 0))
        assert code == EXIT_SCRAPE_FAILED

    def test_partial_success_exit_0(self, no_check_db, sqlite_factory):
        """部分标签失败仍算成功 (与 main.py scrape shop 语义一致)。"""
        scraper = FakeScraper([None, hashtag_data("good", D1, 5)])
        code = run_job(tags=["bad", "good"], scraper=scraper,
                       session_factory=sqlite_factory,
                       sleep_fn=lambda s: None, gap_range=(0, 0))
        assert code == EXIT_OK

    def test_crawler_exception_per_tag_continues(self, no_check_db,
                                                 sqlite_factory):
        """单标签触发风控 (CrawlerException) 不中断批次。"""
        scraper = FakeScraper([RateLimitException("cooldown"),
                               hashtag_data("ok", D1, 7)])
        code = run_job(tags=["blocked", "ok"], scraper=scraper,
                       session_factory=sqlite_factory,
                       sleep_fn=lambda s: None, gap_range=(0, 0))
        assert code == EXIT_OK
        assert scraper.calls == ["blocked", "ok"]

    def test_save_failure_raises_config_error(self, monkeypatch, no_check_db):
        """快照写入失败 → run_job 抛 ConfigError (含操作提示)。

        前置差分查询 get_previous_snapshot 一并 mock (聚焦 save 路径)。
        """
        monkeypatch.setattr(job_mod, "get_previous_snapshot",
                            lambda sf, tag: {})
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )  # 不建表 → save_snapshot 失败
        factory = sessionmaker(bind=engine)
        try:
            with pytest.raises(job_mod.ConfigError, match="docker-compose"):
                run_job(tags=["x"], scraper=FakeScraper(
                    [hashtag_data("x", D1, 1)]), session_factory=factory,
                    sleep_fn=lambda s: None, gap_range=(0, 0))
        finally:
            engine.dispose()

    def test_check_db_unavailable_raises_config_error(self, monkeypatch):
        """check_db 探测失败 → run_job 抛 ConfigError (含可操作提示)。"""

        def broken():
            raise job_mod.ConfigError(f"{job_mod.DB_HINT}\n原始错误: refused")

        monkeypatch.setattr(job_mod, "check_db", broken)
        with pytest.raises(job_mod.ConfigError, match="docker-compose"):
            run_job(tags=["a"], scraper=FakeScraper([]),
                    sleep_fn=lambda s: None, gap_range=(0, 0))

    def test_main_converts_config_error_exit_2(self, monkeypatch, capsys):
        """CLI 入口: ConfigError → 退出码 2, 提示打到 stderr。"""
        def broken(**kw):
            raise job_mod.ConfigError(f"{job_mod.DB_HINT}\n原始错误: refused")

        monkeypatch.setattr(job_mod, "run_job", broken)
        assert job_mod.main(["--tags", "a"]) == EXIT_CONFIG_ERROR
        assert "docker-compose up -d postgres" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# tasks.yaml 新任务定义解析
# ---------------------------------------------------------------------------
class TestTasksYaml:
    NEW_TASK = "scrape_hashtag_snapshots"

    def test_yaml_raw_fields(self):
        """新任务定义: interval=86400 / priority=3 / payload.tags。"""
        data = yaml.safe_load(TASKS_YAML.read_text(encoding="utf-8"))
        tasks = {t["name"]: t for t in data["tasks"]}
        assert self.NEW_TASK in tasks
        cfg = tasks[self.NEW_TASK]
        assert cfg["type"] == "periodic"
        assert cfg["interval"] == 86400
        assert cfg["priority"] == 3
        assert "tiktokmademebuyit" in cfg["payload"]["tags"]

    def test_scheduler_load_config_registers_new_task(self, tmp_dir):
        """TaskScheduler.load_config 正确注册 interval 与 priority。"""
        from src.scheduler import TaskScheduler
        from src.scheduler.task_queue import TaskQueue

        sched = TaskScheduler(
            queue=TaskQueue(use_redis=False),
            state_file=str(tmp_dir / "state.json"),
            auto_start=False,
        )
        task_ids = sched.load_config(str(TASKS_YAML))
        assert self.NEW_TASK in task_ids
        jobs = {j.id: j for j in sched.jobs}
        assert jobs[self.NEW_TASK].trigger.interval.total_seconds() == 86400
        assert sched._task_configs[self.NEW_TASK]["priority"] == 3
        sched.shutdown()


# ---------------------------------------------------------------------------
# main.py handler 接线
# ---------------------------------------------------------------------------
class TestHandlerWiring:
    def test_handler_passes_parsed_tags(self, monkeypatch):
        """payload tags 字符串 → parse_tags 拆分后传给 run_job。"""
        seen = {}

        def fake_run_job(tags=None, **kw):
            seen["tags"] = tags
            return EXIT_OK

        monkeypatch.setattr(job_mod, "run_job", fake_run_job)
        main._task_handler_hashtag(tags="a, #b")
        assert seen["tags"] == ["a", "b"]

    def test_handler_default_tags_none(self, monkeypatch):
        """payload 无 tags → run_job(tags=None) 走 job 默认标签。"""
        seen = {}
        monkeypatch.setattr(job_mod, "run_job",
                            lambda tags=None, **kw: seen.update(tags=tags)
                            or EXIT_OK)
        main._task_handler_hashtag()
        assert seen["tags"] is None

    def test_handler_tag_list_payload(self, monkeypatch):
        """payload tags 为列表时原样透传。"""
        seen = {}
        monkeypatch.setattr(job_mod, "run_job",
                            lambda tags=None, **kw: seen.update(tags=tags)
                            or EXIT_OK)
        main._task_handler_hashtag(tags=["x", "y"])
        assert seen["tags"] == ["x", "y"]

    def test_handler_raises_on_failure(self, monkeypatch):
        """run_job 返回非 0 → handler 抛 RuntimeError (调度层按退避重试)。"""
        monkeypatch.setattr(job_mod, "run_job",
                            lambda tags=None, **kw: EXIT_SCRAPE_FAILED)
        with pytest.raises(RuntimeError):
            main._task_handler_hashtag(tags="a")

    def test_scheduler_start_registers_handler(self, monkeypatch, tmp_dir):
        """cmd_scheduler_start 注册 scrape_hashtag_snapshots handler。"""
        registered = {}

        class FakeScheduler:
            def __init__(self, **kw):
                pass

            def register_handler(self, name, func):
                registered[name] = func

            def load_config(self, path):
                return {name: name for name in registered}

            def process_next(self):
                raise KeyboardInterrupt

            def shutdown(self, wait=True):
                pass

        monkeypatch.setattr(main, "TaskScheduler", FakeScheduler)
        monkeypatch.setattr(main, "check_db", lambda: None)
        assert main.cmd_scheduler_start() == main.EXIT_OK
        assert registered["scrape_hashtag_snapshots"] is main._task_handler_hashtag


# ---------------------------------------------------------------------------
# PostgreSQL 门控测试 (默认 skip, 设置 TEST_POSTGRES_URL 后运行)
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    not TEST_PG_URL,
    reason="需要真实 PostgreSQL: 设置环境变量 TEST_POSTGRES_URL 后运行 (默认 skip)",
)
class TestPostgresGated:
    def test_two_phase_growth_on_postgres(self):
        """真实 PG 上 hashtag_snapshots 两期数据差分。"""
        engine = create_engine(TEST_PG_URL)
        from src.database.models import Base
        Base.metadata.create_all(engine)
        try:
            factory = sessionmaker(bind=engine, expire_on_commit=False)
            diff_engine = SnapshotDiffEngine(session_factory=factory)

            assert diff_engine.save_snapshot(
                hashtag_data("pg_tag", D0, 1000), "hashtag_snapshots")
            assert diff_engine.save_snapshot(
                hashtag_data("pg_tag", D1, 1500), "hashtag_snapshots")

            prev = get_previous_snapshot(factory, "pg_tag")
            growth, rate = job_mod.compute_growth_values(1500, prev["views_total"])
            assert prev["snapshot_date"] == D0
            assert growth == 500
            assert rate == pytest.approx(0.5)
        finally:
            Base.metadata.drop_all(engine)
            engine.dispose()
