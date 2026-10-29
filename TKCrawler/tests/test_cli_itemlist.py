"""main.py CLI itemlist 子命令测试 (Wave 8 接入)。

全部 mock, 不发真实请求、不连真实 DB、不触碰 data/session.json:
    - 参数解析: scrape itemlist 必填/默认值/全局 --dry-run;
    - --dry-run 不构造 scraper、不发请求、不连库;
    - 调度 handler: 缺 sec_uid 抛 ValueError / payload 透传 / 失败抛 RuntimeError;
    - 成功路径: mock scraper + engine, 验证入库行映射
      (author_id=author_id、views=play_count、publish_time=create_time UTC naive);
    - 退出码: 0 成功 / 1 采集失败 / 2 配置或依赖错误。
"""

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]

SEC_UID = "MS4wLjABAAAAtest_sec_uid_0123456789abcdefghij"
HANDLE = "khaby.lame"

# 模拟 ItemListScraper.scrape_user_videos 的输出行 (ITEM_LIST_FIELDS 契约)
FAKE_ROW = {
    "video_id": "7412345678901234567",
    "author_id": "1234567890123456789",
    "sec_uid": SEC_UID,
    "unique_id": HANDLE,
    "desc": "hello world",
    "create_time": 1728384000,
    "play_count": 1000,
    "digg_count": 100,
    "collect_count": 20,
    "comment_count": 10,
    "share_count": 5,
}


def _fake_scraper_cls(rows):
    class FakeItemListScraper:
        def __init__(self, client=None, **kw):
            self.client = client

        def scrape_user_videos(self, sec_uid, unique_id=None,
                               count=15, max_pages=1):
            self.sec_uid = sec_uid
            self.unique_id = unique_id
            self.count = count
            self.max_pages = max_pages
            return rows

    return FakeItemListScraper


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------
class TestParser:
    def test_itemlist_args_and_defaults(self):
        args = main.build_parser().parse_args(
            ["scrape", "itemlist", "--sec-uid", SEC_UID]
        )
        assert args.command == "scrape"
        assert args.scrape_target == "itemlist"
        assert args.sec_uid == SEC_UID
        assert args.handle is None
        assert args.count == 35
        assert args.pages == 3
        assert args.dry_run is False

    def test_itemlist_full_args(self):
        args = main.build_parser().parse_args(
            ["scrape", "itemlist", "--sec-uid", SEC_UID,
             "--handle", HANDLE, "--count", "20", "--pages", "1"]
        )
        assert args.handle == HANDLE
        assert args.count == 20
        assert args.pages == 1

    def test_itemlist_requires_sec_uid(self):
        with pytest.raises(SystemExit) as exc:
            main.build_parser().parse_args(["scrape", "itemlist"])
        assert exc.value.code == 2

    def test_global_dry_run_before_subcommand(self):
        args = main.build_parser().parse_args(
            ["--dry-run", "scrape", "itemlist", "--sec-uid", SEC_UID]
        )
        assert args.dry_run is True


# ---------------------------------------------------------------------------
# dry-run: 不发请求、不连库
# ---------------------------------------------------------------------------
class TestDryRun:
    def test_dry_run_never_constructs_scraper_or_db(self, capsys):
        def _boom(*a, **kw):
            raise AssertionError("dry-run 不应构造 scraper")

        class _BoomEngine:
            def __init__(self, *a, **kw):
                raise AssertionError("dry-run 不应连库")

        original = (main.ItemListScraper, main.SnapshotDiffEngine, main.check_db)
        main.ItemListScraper = _boom
        main.SnapshotDiffEngine = _BoomEngine
        try:
            code = main.main(["--dry-run", "scrape", "itemlist",
                              "--sec-uid", SEC_UID, "--handle", HANDLE])
        finally:
            main.ItemListScraper, main.SnapshotDiffEngine, main.check_db = original
        assert code == 0
        out = capsys.readouterr().out
        assert "[dry-run]" in out
        assert SEC_UID in out
        assert "video_snapshots" in out


# ---------------------------------------------------------------------------
# 调度 handler
# ---------------------------------------------------------------------------
class TestTaskHandler:
    def test_missing_sec_uid_raises_value_error(self):
        with pytest.raises(ValueError) as exc:
            main._task_handler_itemlist(handle=HANDLE)
        assert "sec_uid" in str(exc.value)
        assert "tasks.yaml" in str(exc.value)

    def test_payload_passthrough(self, monkeypatch):
        seen = {}

        def fake_run(sec_uid, handle=None, count=35, pages=3, dry_run=False):
            seen.update(sec_uid=sec_uid, handle=handle, count=count,
                        pages=pages, dry_run=dry_run)
            return main.EXIT_OK

        monkeypatch.setattr(main, "run_itemlist_scrape", fake_run)
        main._task_handler_itemlist(sec_uid=SEC_UID, handle=HANDLE,
                                    count=10, pages=2)
        assert seen == {"sec_uid": SEC_UID, "handle": HANDLE, "count": 10,
                        "pages": 2, "dry_run": False}

    def test_payload_defaults(self, monkeypatch):
        seen = {}

        def fake_run(sec_uid, handle=None, count=35, pages=3, dry_run=False):
            seen.update(sec_uid=sec_uid, handle=handle, count=count,
                        pages=pages)
            return main.EXIT_OK

        monkeypatch.setattr(main, "run_itemlist_scrape", fake_run)
        main._task_handler_itemlist(sec_uid=SEC_UID)
        assert seen["handle"] is None
        assert seen["count"] == 35
        assert seen["pages"] == 1

    def test_handler_raises_runtime_error_on_failure(self, monkeypatch):
        monkeypatch.setattr(main, "run_itemlist_scrape",
                            lambda *a, **kw: main.EXIT_SCRAPE_FAILED)
        with pytest.raises(RuntimeError):
            main._task_handler_itemlist(sec_uid=SEC_UID)

    def test_scheduler_start_registers_handler(self, monkeypatch):
        """cmd_scheduler_start 注册 scrape_itemlist_snapshots handler。"""
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
        assert registered["scrape_itemlist_snapshots"] is main._task_handler_itemlist


# ---------------------------------------------------------------------------
# 成功路径: 入库行映射
# ---------------------------------------------------------------------------
class TestHappyPath:
    def test_row_mapping_saved_to_video_snapshots(self, monkeypatch, capsys):
        saved = {}
        scraper_calls = {}

        class FakeEngine:
            def save_snapshot(self, data, table):
                saved.setdefault("rows", []).append(dict(data))
                saved["table"] = table
                return True

        scraper_cls = _fake_scraper_cls([dict(FAKE_ROW)])
        monkeypatch.setattr(main, "ItemListScraper", scraper_cls)
        monkeypatch.setattr(main, "SnapshotDiffEngine", lambda: FakeEngine())
        monkeypatch.setattr(main, "check_db", lambda: None)
        monkeypatch.setattr(main, "_make_client", lambda: object())

        code = main.main(["scrape", "itemlist", "--sec-uid", SEC_UID,
                          "--handle", HANDLE, "--count", "35", "--pages", "1"])
        assert code == 0
        assert saved["table"] == "video_snapshots"

        # 行映射: video_snapshots 列 ← ITEM_LIST_FIELDS
        row = saved["rows"][0]
        assert row["video_id"] == str(FAKE_ROW["video_id"])
        assert row["author_id"] == FAKE_ROW["author_id"]
        assert row["views"] == FAKE_ROW["play_count"]
        assert row["likes"] == FAKE_ROW["digg_count"]
        assert row["comments"] == FAKE_ROW["comment_count"]
        assert row["shares"] == FAKE_ROW["share_count"]
        # publish_time = create_time 的 UTC naive datetime
        expected = datetime.fromtimestamp(FAKE_ROW["create_time"],
                                          tz=timezone.utc).replace(tzinfo=None)
        assert row["publish_time"] == expected
        assert row["publish_time"].tzinfo is None
        assert row["snapshot_date"] == date.today()
        # 无来源字段不入库: desc/collect_count/sec_uid/unique_id
        assert "desc" not in row
        assert "collect_count" not in row
        assert "sec_uid" not in row
        assert "unique_id" not in row
        # product_id/duration/is_paid 留 NULL (不传即 NULL)
        assert "product_id" not in row
        assert "duration" not in row
        assert "is_paid" not in row

    def test_scraper_args_passed_through(self, monkeypatch):
        holder = {}

        class HolderScraper(_fake_scraper_cls([dict(FAKE_ROW)])):
            def scrape_user_videos(self, sec_uid, unique_id=None,
                                   count=15, max_pages=1):
                holder.update(sec_uid=sec_uid, unique_id=unique_id,
                              count=count, max_pages=max_pages)
                return [dict(FAKE_ROW)]

        class FakeEngine:
            def save_snapshot(self, data, table):
                return True

        monkeypatch.setattr(main, "ItemListScraper", HolderScraper)
        monkeypatch.setattr(main, "SnapshotDiffEngine", lambda: FakeEngine())
        monkeypatch.setattr(main, "check_db", lambda: None)
        monkeypatch.setattr(main, "_make_client", lambda: object())

        code = main.main(["scrape", "itemlist", "--sec-uid", SEC_UID,
                          "--handle", HANDLE, "--count", "35", "--pages", "1"])
        assert code == 0
        assert holder == {"sec_uid": SEC_UID, "unique_id": HANDLE,
                          "count": 35, "max_pages": 1}

    def test_summary_prints_saved_count(self, monkeypatch, capsys):
        rows = [dict(FAKE_ROW), dict(FAKE_ROW, video_id="999")]

        class FakeEngine:
            def save_snapshot(self, data, table):
                return True

        monkeypatch.setattr(main, "ItemListScraper", _fake_scraper_cls(rows))
        monkeypatch.setattr(main, "SnapshotDiffEngine", lambda: FakeEngine())
        monkeypatch.setattr(main, "check_db", lambda: None)
        monkeypatch.setattr(main, "_make_client", lambda: object())

        assert main.main(["scrape", "itemlist", "--sec-uid", SEC_UID]) == 0
        out = capsys.readouterr().out
        assert "共写入 2 条视频快照" in out


# ---------------------------------------------------------------------------
# 失败路径
# ---------------------------------------------------------------------------
class TestFailurePath:
    @pytest.mark.parametrize("rows", [None, []],
                             ids=["scraper_none", "scraper_empty"])
    def test_no_videos_exit_1(self, monkeypatch, capsys, rows):
        monkeypatch.setattr(main, "ItemListScraper", _fake_scraper_cls(rows))
        monkeypatch.setattr(main, "check_db", lambda: None)
        code = main.main(["scrape", "itemlist", "--sec-uid", SEC_UID])
        assert code == 1
        assert "未采集到视频" in capsys.readouterr().out

    def test_save_failure_exit_2_with_hint(self, monkeypatch):
        class BadEngine:
            def save_snapshot(self, data, table):
                return False

        monkeypatch.setattr(main, "ItemListScraper",
                            _fake_scraper_cls([dict(FAKE_ROW)]))
        monkeypatch.setattr(main, "SnapshotDiffEngine", lambda: BadEngine())
        monkeypatch.setattr(main, "check_db", lambda: None)
        code = main.main(["scrape", "itemlist", "--sec-uid", SEC_UID])
        assert code == 2

    def test_db_unavailable_exit_2(self, monkeypatch, capsys):
        def _no_db():
            raise main.ConfigError(main.DB_HINT)

        monkeypatch.setattr(main, "check_db", _no_db)
        code = main.main(["scrape", "itemlist", "--sec-uid", SEC_UID])
        assert code == 2
        assert "docker-compose up -d postgres" in capsys.readouterr().err
