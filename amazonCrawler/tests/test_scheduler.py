"""scheduler 单测：interval 解析 / 状态持久化 / job 注册 / dry-run（mock ScrapeService）。
不真实请求、不启动真实 APScheduler（auto_start=False）。
"""

import json
import time
from pathlib import Path

import pytest
import yaml

from src.scheduler.jobs import (
    DEFAULT_KEYWORDS_YAML,
    job_scrape_keywords,
    job_scrape_offers,
    job_scrape_watchlist,
    reset_services,
    set_services,
    start_with_handlers,
)
from src.scheduler.task_scheduler import (
    RetryPolicy,
    TaskScheduler,
    parse_interval,
)


# ---------------------------------------------------------------------------
# parse_interval
# ---------------------------------------------------------------------------

class TestParseInterval:
    def test_units(self):
        assert parse_interval("2d") == 2 * 86400
        assert parse_interval("12h") == 12 * 3600
        assert parse_interval("30m") == 30 * 60
        assert parse_interval("45s") == 45

    def test_numeric_seconds(self):
        assert parse_interval(90) == 90
        assert parse_interval("120") == 120

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            parse_interval("2x")
        with pytest.raises(ValueError):
            parse_interval("d")


# ---------------------------------------------------------------------------
# 配置加载与 handler 注册
# ---------------------------------------------------------------------------

class TestLoadConfig:
    def test_load_real_tasks_yaml(self, tmp_path):
        state_file = tmp_path / "state.json"
        sched = TaskScheduler(state_file=str(state_file), auto_start=False)
        task_ids = sched.load_config(
            Path(__file__).resolve().parent.parent / "src" / "scheduler"
            / "config" / "tasks.yaml")
        assert set(task_ids) == {"scrape_watchlist", "scrape_keyword_ranks",
                                 "scrape_offers"}
        cfg = sched._task_configs["scrape_watchlist"]
        assert cfg["interval_seconds"] == 86400
        assert cfg["priority"] == 1
        assert isinstance(cfg["retry_policy"], RetryPolicy)
        assert cfg["retry_policy"].max_attempts == 3
        assert cfg["retry_policy"].base_delay == 60
        sched.stop()

    def test_register_handler_and_status(self, tmp_path):
        sched = TaskScheduler(state_file=str(tmp_path / "state.json"))
        sched.register_handler("scrape_watchlist", lambda **kw: None)
        status = sched.status()
        assert "scrape_watchlist" in status["registered_handlers"]
        assert status["scheduler_running"] is False
        sched.stop()


# ---------------------------------------------------------------------------
# 队列执行与指数退避重试（dry-run，mock handler）
# ---------------------------------------------------------------------------

class TestQueueExecution:
    def test_submit_and_process_success(self, tmp_path):
        sched = TaskScheduler(state_file=str(tmp_path / "state.json"))
        calls = []
        sched.register_handler("demo", lambda **kw: calls.append(kw))
        task_id = sched.submit("demo", asin="B0DLQ8MVF7")
        assert sched.process_next() is True
        assert calls == [{"asin": "B0DLQ8MVF7"}]
        state = sched._states[task_id]["status"]
        assert state == "completed"
        sched.stop()

    def test_retry_exponential_backoff(self, tmp_path):
        sched = TaskScheduler(state_file=str(tmp_path / "state.json"))
        attempts = []

        def flaky(**kw):
            attempts.append(1)
            raise RuntimeError("always fails")

        # tasks.yaml 之外直接构造带 retry 策略的任务
        sched.schedule_periodic("flaky_job", interval=3600, priority=5)
        sched._task_configs["flaky_job"]["retry_policy"] = RetryPolicy(
            max_attempts=3, base_delay=10, mode="exponential")
        sched.register_handler("flaky_job", flaky)

        task_id = sched.submit("flaky_job")
        # 第 1 次失败 → 重试入队（delay=10）
        sched.process_next()
        assert sched._states[task_id]["status"] == "retrying"
        assert sched._states[task_id]["attempts"] == 1
        assert sched._states[task_id]["next_retry_in"] == 10
        # 跳过退避等待：直接清除 not_before 让任务立即可执行
        for t in list(sched._queue.queue):
            t.not_before = 0.0
        # 第 2 次失败 → delay=20
        sched.process_next()
        assert sched._states[task_id]["next_retry_in"] == 20
        for t in list(sched._queue.queue):
            t.not_before = 0.0
        # 第 3 次失败 → 重试耗尽 → failed
        sched.process_next()
        assert sched._states[task_id]["status"] == "failed"
        assert len(attempts) == 3
        sched.stop()

    def test_retry_expires_then_success(self, tmp_path):
        sched = TaskScheduler(state_file=str(tmp_path / "state.json"))
        sched.schedule_periodic("recovers", interval=3600, priority=5)
        sched._task_configs["recovers"]["retry_policy"] = RetryPolicy(
            max_attempts=2, base_delay=0.05, mode="fixed")
        state_box = {"n": 0}

        def flaky_then_ok(**kw):
            state_box["n"] += 1
            if state_box["n"] == 1:
                raise RuntimeError("first fails")

        sched.register_handler("recovers", flaky_then_ok)
        task_id = sched.submit("recovers")
        sched.process_next()  # 失败，退避 0.05s 重入队
        assert sched._states[task_id]["status"] == "retrying"
        time.sleep(0.06)
        sched.process_next()  # not_before 已过 → 执行成功
        assert sched._states[task_id]["status"] == "completed"
        sched.stop()

    def test_unregistered_handler_skipped(self, tmp_path):
        sched = TaskScheduler(state_file=str(tmp_path / "state.json"))
        sched.schedule_periodic("no_handler", interval=3600, priority=9)
        task_id = sched.submit("no_handler")
        sched.process_next()
        assert sched._states[task_id]["status"] == "skipped"
        sched.stop()

    def test_priority_order(self, tmp_path):
        sched = TaskScheduler(state_file=str(tmp_path / "state.json"))
        order = []
        sched.register_handler("low", lambda **kw: order.append("low"))
        sched.register_handler("high", lambda **kw: order.append("high"))
        sched.schedule_periodic("low", interval=3600, priority=9)
        sched.schedule_periodic("high", interval=3600, priority=1)
        sched.submit("low")
        sched.submit("high")
        sched.process_next()
        sched.process_next()
        assert order == ["high", "low"]
        sched.stop()


# ---------------------------------------------------------------------------
# 状态持久化
# ---------------------------------------------------------------------------

class TestStatePersistence:
    def test_state_file_written_and_reloaded(self, tmp_path):
        state_file = tmp_path / "sched_state.json"
        sched = TaskScheduler(state_file=str(state_file))
        sched.schedule_periodic("persisted_job", interval=7200, priority=2)
        assert state_file.exists()
        saved = json.loads(state_file.read_text(encoding="utf-8"))
        assert "persisted_job" in saved
        assert saved["persisted_job"]["status"] == "scheduled"

        # 新实例从状态文件恢复
        sched2 = TaskScheduler(state_file=str(state_file))
        assert "persisted_job" in sched2._states
        restored = sched2.restore_periodic_tasks()
        assert "persisted_job" in restored
        assert "persisted_job" in sched2._task_configs
        sched.stop()
        sched2.stop()

    def test_corrupt_state_file_recovered(self, tmp_path):
        state_file = tmp_path / "broken.json"
        state_file.write_text("{not valid json", encoding="utf-8")
        sched = TaskScheduler(state_file=str(state_file))
        assert sched._states == {}
        sched.stop()


# ---------------------------------------------------------------------------
# jobs.py：三个 job 的 dry-run（注入 mock service）
# ---------------------------------------------------------------------------

class _MockWatchlist:
    def __init__(self, asins):
        self._asins = asins

    def list_items(self):
        return [{"asin": a, "note": "", "variant_asin": None}
                for a in self._asins]


class _MockScrape:
    def __init__(self):
        self.pdp_calls = []
        self.keyword_calls = []
        self.offer_calls = []

    def scrape_pdp_now(self, asins):
        self.pdp_calls.extend(asins)
        return [{"asin": a, "ok": True, "snapshot": {}} for a in asins]

    def scrape_keyword_now(self, keyword, page=1):
        self.keyword_calls.append(keyword)
        return True, [{"keyword": keyword}]

    def scrape_offers_now(self, asin):
        self.offer_calls.append(asin)
        return {"ok": True, "offers": []}


@pytest.fixture(autouse=True)
def _reset_services():
    reset_services()
    yield
    reset_services()


class TestJobs:
    def test_job_scrape_watchlist(self):
        mock_scrape = _MockScrape()
        set_services(watchlist=_MockWatchlist(["B0DLQ8MVF7", "B0GWVTMR4Q"]),
                     scrape=mock_scrape)
        result = job_scrape_watchlist()
        assert result == {"scraped": 2, "failed": 0}
        assert mock_scrape.pdp_calls == ["B0DLQ8MVF7", "B0GWVTMR4Q"]

    def test_job_scrape_watchlist_empty(self):
        set_services(watchlist=_MockWatchlist([]), scrape=_MockScrape())
        assert job_scrape_watchlist() == {"scraped": 0, "failed": 0}

    def test_job_scrape_keywords(self, tmp_path):
        kw_file = tmp_path / "keywords.yaml"
        kw_file.write_text(
            yaml.safe_dump({"keywords": ["dog shirt", "cat shirt"]}),
            encoding="utf-8")
        mock_scrape = _MockScrape()
        set_services(scrape=mock_scrape)
        result = job_scrape_keywords(keywords_path=kw_file)
        assert result == {"scraped": 2, "failed": 0}
        assert mock_scrape.keyword_calls == ["dog shirt", "cat shirt"]

    def test_job_scrape_keywords_missing_file(self):
        set_services(scrape=_MockScrape())
        assert job_scrape_keywords(
            keywords_path=tmp_path_missing()) == {"scraped": 0, "failed": 0}

    def test_job_scrape_offers(self):
        mock_scrape = _MockScrape()
        set_services(watchlist=_MockWatchlist(["B0DLQ8MVF7"]),
                     scrape=mock_scrape)
        result = job_scrape_offers()
        assert result == {"scraped": 1, "failed": 0}
        assert mock_scrape.offer_calls == ["B0DLQ8MVF7"]

    def test_start_with_handlers_registers_three_jobs(self, tmp_path):
        tasks_file = tmp_path / "tasks.yaml"
        tasks_file.write_text(
            yaml.safe_dump({"tasks": [
                {"name": "scrape_watchlist", "type": "periodic",
                 "interval": "1d", "priority": 1,
                 "retry": {"max_attempts": 3, "backoff": "exponential",
                           "base_delay": 60}},
            ]}),
            encoding="utf-8")
        sched = start_with_handlers(
            config_path=tasks_file,
            state_file=str(tmp_path / "state.json"), auto_start=False)
        status = sched.status()
        assert "scrape_watchlist" in status["registered_handlers"]
        assert "scrape_keyword_ranks" in status["registered_handlers"]
        assert "scrape_offers" in status["registered_handlers"]
        assert status["registered_tasks"] == ["scrape_watchlist"]
        sched.stop()

    def test_full_dry_run_via_queue(self, tmp_path):
        """端到端 dry-run：tasks.yaml → handler 注册 → submit → 消费（mock service）。"""
        mock_scrape = _MockScrape()
        set_services(watchlist=_MockWatchlist(["B0DLQ8MVF7"]),
                     scrape=mock_scrape)
        sched = start_with_handlers(
            state_file=str(tmp_path / "state.json"), auto_start=False)
        task_id = sched.submit("scrape_watchlist")
        sched.process_next()
        assert sched._states[task_id]["status"] == "completed"
        assert mock_scrape.pdp_calls == ["B0DLQ8MVF7"]
        sched.stop()


def tmp_path_missing():
    import tempfile
    return Path(tempfile.gettempdir()) / "definitely_missing_keywords.yaml"
