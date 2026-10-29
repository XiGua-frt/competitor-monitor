"""main.py CLI 测试 (Agent L)。

全部 mock, 不发真实请求、不连真实 PG/Redis:
    - 参数解析: 子命令/必填参数/全局 --dry-run/互斥组;
    - 子命令分发: 各 scrape 目标 / scheduler / init-db / report;
    - --dry-run 不构造 scraper、不发请求、不连库;
    - 退出码: 0 成功 / 1 采集失败 / 2 配置或依赖错误。
"""

import json
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402
from src.common.exceptions import WAFBlockedException  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------
class TestParser:
    def test_scrape_shop_full(self):
        args = main.build_parser().parse_args(
            ["scrape", "shop", "--category-url", "http://x", "--limit", "5"]
        )
        assert args.command == "scrape"
        assert args.scrape_target == "shop"
        assert args.category_url == "http://x"
        assert args.limit == 5
        assert args.dry_run is False

    def test_global_dry_run(self):
        args = main.build_parser().parse_args(
            ["--dry-run", "scrape", "hashtag", "--tag", "summer"]
        )
        assert args.dry_run is True
        assert args.tag == "summer"

    def test_scrape_video_and_creator(self):
        args = main.build_parser().parse_args(
            ["scrape", "video", "--product-id", "P1", "--limit", "3"]
        )
        assert args.product_id == "P1"
        args = main.build_parser().parse_args(
            ["scrape", "creator", "--creator-id", "C1"]
        )
        assert args.creator_id == "C1"

    def test_scheduler_flags(self):
        args = main.build_parser().parse_args(["scheduler", "--start"])
        assert args.start is True and args.status is False and args.pause is None
        args = main.build_parser().parse_args(["scheduler", "--pause", "t1"])
        assert args.pause == "t1"

    def test_scheduler_requires_one_flag(self):
        with pytest.raises(SystemExit) as exc:
            main.build_parser().parse_args(["scheduler"])
        assert exc.value.code == 2

    def test_missing_required_arg(self):
        with pytest.raises(SystemExit) as exc:
            main.build_parser().parse_args(["scrape", "shop"])
        assert exc.value.code == 2

    def test_report_and_initdb(self):
        args = main.build_parser().parse_args(
            ["report", "--product-id", "P9"]
        )
        assert args.product_id == "P9"
        args = main.build_parser().parse_args(["init-db"])
        assert args.command == "init-db"


# ---------------------------------------------------------------------------
# scrape: dry-run 与分发
# ---------------------------------------------------------------------------
class TestScrapeDispatch:
    def test_dry_run_shop_never_constructs_scraper(self, capsys):
        def _boom(*a, **kw):
            raise AssertionError("dry-run 不应构造 scraper")

        class _BoomEngine:
            def __init__(self, *a, **kw):
                raise AssertionError("dry-run 不应连库")

        original = (main.ShopProductScraper, main.SnapshotDiffEngine, main.check_db)
        main.ShopProductScraper = _boom
        main.SnapshotDiffEngine = _BoomEngine
        try:
            code = main.main(["--dry-run", "scrape", "shop",
                              "--category-url", "http://x"])
        finally:
            main.ShopProductScraper, main.SnapshotDiffEngine, main.check_db = original
        assert code == 0
        out = capsys.readouterr().out
        assert "[dry-run]" in out
        assert "product_snapshots" in out

    def test_shop_happy_path_saves_snapshots(self, capsys, monkeypatch):
        calls = {}

        class FakeScraper:
            def __init__(self, client=None, **kw):
                calls["client"] = client

            def scrape_category(self, url):
                calls["url"] = url
                return ["P1", "P2", "P3"]

            def scrape_product(self, pid):
                return {"product_id": pid, "title": f"t-{pid}"}

        class FakeEngine:
            def __init__(self):
                self.saved = []

            def save_snapshot(self, data, table):
                calls.setdefault("client", None)
                self.saved.append((data["product_id"], table))
                return True

        monkeypatch.setattr(main, "ShopProductScraper", FakeScraper)
        fake_engine = FakeEngine()
        monkeypatch.setattr(main, "SnapshotDiffEngine", lambda: fake_engine)
        monkeypatch.setattr(main, "check_db", lambda: None)
        monkeypatch.setattr(main, "_make_client", lambda: object())

        code = main.main(["scrape", "shop", "--category-url", "http://cat",
                          "--limit", "2"])
        assert code == 0
        assert calls["url"] == "http://cat"
        # --limit 截断为 2 个商品
        assert fake_engine.saved == [("P1", "product_snapshots"),
                                     ("P2", "product_snapshots")]

    def test_shop_no_products_exit_1(self, monkeypatch, capsys):
        class FakeScraper:
            def __init__(self, client=None, **kw):
                pass

            def scrape_category(self, url):
                return []

        monkeypatch.setattr(main, "ShopProductScraper", FakeScraper)
        monkeypatch.setattr(main, "check_db", lambda: None)
        code = main.main(["scrape", "shop", "--category-url", "http://x"])
        assert code == 1
        assert "未解析到任何商品" in capsys.readouterr().out

    def test_shop_product_scrape_fail_exit_1(self, monkeypatch):
        class FakeScraper:
            def __init__(self, client=None, **kw):
                pass

            def scrape_category(self, url):
                return ["P1"]

            def scrape_product(self, pid):
                return None

        monkeypatch.setattr(main, "ShopProductScraper", FakeScraper)
        monkeypatch.setattr(main, "check_db", lambda: None)
        assert main.main(["scrape", "shop", "--category-url", "http://x"]) == 1

    def test_db_unavailable_exit_2_with_hint(self, monkeypatch, capsys):
        def _no_db():
            raise main.ConfigError(main.DB_HINT)

        monkeypatch.setattr(main, "check_db", _no_db)
        code = main.main(["scrape", "shop", "--category-url", "http://x"])
        assert code == 2
        err = capsys.readouterr().err
        assert "docker-compose up -d postgres" in err

    def test_save_failure_exit_2_with_hint(self, monkeypatch):
        class FakeScraper:
            def __init__(self, client=None, **kw):
                pass

            def scrape_category(self, url):
                return ["P1"]

            def scrape_product(self, pid):
                return {"product_id": pid}

        class BadEngine:
            def save_snapshot(self, data, table):
                return False

        monkeypatch.setattr(main, "ShopProductScraper", FakeScraper)
        monkeypatch.setattr(main, "SnapshotDiffEngine", lambda: BadEngine())
        monkeypatch.setattr(main, "check_db", lambda: None)
        code = main.main(["scrape", "shop", "--category-url", "http://x"])
        assert code == 2

    @pytest.mark.parametrize("argv,target,table,method", [
        (["scrape", "video", "--product-id", "V0"], "video", "video_snapshots",
         "scrape_product_videos"),
        (["scrape", "creator", "--creator-id", "C0"], "creator",
         "creator_snapshots", "scrape_profile"),
        (["scrape", "hashtag", "--tag", "summer"], "hashtag",
         "hashtag_snapshots", "scrape_hashtag"),
    ])
    def test_video_creator_hashtag_dispatch(self, argv, target, table, method,
                                            monkeypatch):
        saved = {}

        fake_scraper_cls = None
        if target == "video":
            class fake_scraper_cls:
                def __init__(self, client=None, **kw):
                    pass

                def scrape_product_videos(self, pid, limit=10):
                    return [{"video_id": "v1", "product_id": pid}]
        elif target == "creator":
            class fake_scraper_cls:
                def __init__(self, client=None, **kw):
                    pass

                def scrape_profile(self, cid):
                    return {"creator_id": cid}
        else:
            class fake_scraper_cls:
                def __init__(self, client=None, **kw):
                    pass

                def scrape_hashtag(self, tag):
                    return {"tag_name": tag}

        class FakeEngine:
            def save_snapshot(self, data, tbl):
                saved["data"], saved["table"] = data, tbl
                return True

        attr = {"video": "VideoStreamScraper", "creator": "CreatorProfileScraper",
                "hashtag": "HashtagTrendScraper"}[target]
        monkeypatch.setattr(main, attr, fake_scraper_cls)
        monkeypatch.setattr(main, "SnapshotDiffEngine", lambda: FakeEngine())
        monkeypatch.setattr(main, "check_db", lambda: None)

        assert main.main(argv) == 0
        assert saved["table"] == table
        assert saved["data"]  # 有数据写入

    def test_waf_exception_exit_1(self, monkeypatch):
        def _waf(*a, **kw):
            raise WAFBlockedException("blocked")

        monkeypatch.setattr(main, "run_shop_scrape", _waf)
        assert main.main(["scrape", "shop", "--category-url", "http://x"]) == 1


# ---------------------------------------------------------------------------
# scheduler
# ---------------------------------------------------------------------------
class TestScheduler:
    # 沙箱环境 pytest tmp_path 不可用, 用项目内 data/ 下的测试专用路径
    FAKE_STATE = Path(PROJECT_ROOT) / "data" / "scheduler_state_cli_test.json"

    def test_status_lists_jobs(self, monkeypatch, capsys):
        class FakeJob:
            id = "scrape_shop_products"
            name = "scrape_shop_products"

        class FakeScheduler:
            def __init__(self, **kw):
                assert kw.get("auto_start") is False

            def load_config(self, path):
                return {"scrape_shop_products": "scrape_shop_products"}

            @property
            def jobs(self):
                return [FakeJob()]

        monkeypatch.setattr(main, "TaskScheduler", FakeScheduler)
        monkeypatch.setattr(main, "STATE_FILE", self.FAKE_STATE)
        code = main.main(["scheduler", "--status"])
        assert code == 0
        out = capsys.readouterr().out
        assert "scrape_shop_products" in out

    def test_pause_writes_state(self, monkeypatch):
        paused = {}

        class FakeScheduler:
            def __init__(self, **kw):
                pass

            def load_config(self, path):
                return {}

            def pause_task(self, task_id):
                paused["id"] = task_id

        monkeypatch.setattr(main, "TaskScheduler", FakeScheduler)
        monkeypatch.setattr(main, "STATE_FILE", self.FAKE_STATE)
        code = main.main(["scheduler", "--pause", "scrape_shop_products"])
        assert code == 0
        assert paused["id"] == "scrape_shop_products"


# ---------------------------------------------------------------------------
# init-db
# ---------------------------------------------------------------------------
class TestInitDb:
    def _install_fake_scripts(self, monkeypatch, migrations_ok=True,
                              tables_ok=True):
        fake = ModuleType("scripts.init_db")

        def run_migrations():
            if not migrations_ok:
                raise RuntimeError("connection refused")

        def verify_tables(engine):
            return tables_ok

        fake.run_migrations = run_migrations
        fake.verify_tables = verify_tables
        monkeypatch.setitem(sys.modules, "scripts.init_db", fake)

        import src.database.connection as conn_mod
        fake_engine = MagicMock()
        monkeypatch.setattr(conn_mod, "create_db_engine",
                            lambda *a, **kw: fake_engine)

    def test_success_exit_0(self, monkeypatch):
        self._install_fake_scripts(monkeypatch)
        assert main.main(["init-db"]) == 0

    def test_migration_failure_exit_2(self, monkeypatch, capsys):
        self._install_fake_scripts(monkeypatch, migrations_ok=False)
        code = main.main(["init-db"])
        assert code == 2
        assert "docker-compose up -d postgres" in capsys.readouterr().out

    def test_missing_tables_exit_1(self, monkeypatch):
        self._install_fake_scripts(monkeypatch, tables_ok=False)
        assert main.main(["init-db"]) == 1


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
class TestReport:
    def test_report_json_output(self, capsys, monkeypatch):
        class FakeCalc:
            def __init__(self):
                pass

            def calculate_all(self, product_id):
                assert product_id == "P1"
                return {"content_efficiency": 33.3,
                        "creator_penetration": 0.16,
                        "saturation_index": 3.6,
                        "surge_coefficient": None}

        monkeypatch.setattr(main, "MetricsCalculator", FakeCalc)
        monkeypatch.setattr(main, "check_db", lambda: None)
        code = main.main(["report", "--product-id", "P1"])
        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["product_id"] == "P1"
        assert payload["content_efficiency"] == 33.3
        assert set(payload) == {"product_id", "content_efficiency",
                                "creator_penetration", "saturation_index",
                                "surge_coefficient"}

    def test_report_db_down_exit_2(self, monkeypatch, capsys):
        def _no_db():
            raise main.ConfigError(main.DB_HINT)

        monkeypatch.setattr(main, "check_db", _no_db)
        code = main.main(["report", "--product-id", "P1"])
        assert code == 2
        assert "docker-compose up -d postgres" in capsys.readouterr().err

    def test_report_calc_failure_exit_2(self, monkeypatch):
        class FakeCalc:
            def __init__(self):
                pass

            def calculate_all(self, product_id):
                raise RuntimeError("boom")

        monkeypatch.setattr(main, "MetricsCalculator", FakeCalc)
        monkeypatch.setattr(main, "check_db", lambda: None)
        assert main.main(["report", "--product-id", "P1"]) == 2


# ---------------------------------------------------------------------------
# 退出码文档约定
# ---------------------------------------------------------------------------
def test_exit_code_docstring_contract():
    doc = main.__doc__ or ""
    assert "退出码" in doc
    assert "0" in doc and "1" in doc and "2" in doc
    assert main.EXIT_OK == 0
    assert main.EXIT_SCRAPE_FAILED == 1
    assert main.EXIT_CONFIG_ERROR == 2
