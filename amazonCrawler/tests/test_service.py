"""service 层单测：watchlist 临时 yaml / scrape 全链路 mock / 异步任务轮询 /
metrics 封装 / health 探测。无真实网络、无真实 Redis。
"""

import time
from datetime import date, timedelta

import pytest

from src.service.health_service import HealthService
from src.service.metrics_service import MetricsService
from src.service.scrape_service import (
    ScrapeService,
    get_cooldowns,
    register_cooldown,
)
from src.service.watchlist_service import (
    ASIN_PATTERN,
    WatchlistService,
    WatchlistValidationError,
)


# ---------------------------------------------------------------------------
# WatchlistService
# ---------------------------------------------------------------------------

class TestWatchlistService:
    def test_asin_pattern(self):
        assert ASIN_PATTERN.match("B0DLQ8MVF7")
        assert not ASIN_PATTERN.match("B0DLQ8MVF")        # 9 位
        assert not ASIN_PATTERN.match("A0DLQ8MVF7")       # 非 B0 开头
        assert not ASIN_PATTERN.match("b0dlq8mvf7")       # 小写

    def test_add_list_remove_roundtrip(self, tmp_path):
        svc = WatchlistService(path=tmp_path / "watchlist.yaml")
        entry = svc.add_item("B0DLQ8MVF7", note="测试", variant_asin=None)
        assert entry == {"asin": "B0DLQ8MVF7", "note": "测试",
                         "variant_asin": None}
        svc.add_item("B0GWVTMR4Q")
        items = svc.list_items()
        assert [it["asin"] for it in items] == ["B0DLQ8MVF7", "B0GWVTMR4Q"]
        assert svc.remove_item("B0GWVTMR4Q") is True
        assert svc.remove_item("B0GWVTMR4Q") is False
        assert [it["asin"] for it in svc.list_items()] == ["B0DLQ8MVF7"]

    def test_add_invalid_asin_raises(self, tmp_path):
        svc = WatchlistService(path=tmp_path / "watchlist.yaml")
        with pytest.raises(WatchlistValidationError):
            svc.add_item("NOT_AN_ASIN")

    def test_add_idempotent_updates_note(self, tmp_path):
        svc = WatchlistService(path=tmp_path / "watchlist.yaml")
        svc.add_item("B0DLQ8MVF7", note="a")
        entry = svc.add_item("B0DLQ8MVF7", note="b")
        assert entry["note"] == "b"
        assert len(svc.list_items()) == 1

    def test_missing_file_empty_list(self, tmp_path):
        svc = WatchlistService(path=tmp_path / "nope.yaml")
        assert svc.list_items() == []


# ---------------------------------------------------------------------------
# ScrapeService（mock scraper 全链路）
# ---------------------------------------------------------------------------

class _FakePdp:
    def __init__(self, fail_asins=(), block_asins=()):
        self.fail_asins = set(fail_asins)
        self.block_asins = set(block_asins)
        self.calls = []
        self.cooldown_remaining = 0.0

    def get_cooldown_remaining(self):
        return self.cooldown_remaining

    def scrape_product(self, asin, marketplace="US"):
        self.calls.append(asin)
        if asin in self.block_asins:
            from src.common.exceptions import BotBlockedException
            raise BotBlockedException(url=f"dp/{asin}", status_code=503)
        if asin in self.fail_asins:
            raise RuntimeError("boom")
        return {"asin": asin, "title": f"T {asin}", "price_current": 19.99,
                "snapshot_date": date.today().isoformat()}


class _FakeSearch:
    def __init__(self, fail_keywords=()):
        self.fail_keywords = set(fail_keywords)

    def search(self, keyword, page=1, marketplace="US"):
        if keyword in self.fail_keywords:
            from src.common.exceptions import BotBlockedException
            raise BotBlockedException(url=f"s?k={keyword}", status_code=503)
        return [{"keyword": keyword, "asin": "B0DLQ8MVF7",
                 "snapshot_date": date.today().isoformat(),
                 "natural_rank": 3, "ad_rank": None, "page": page}]


class _FakeOffer:
    def scrape_offers(self, asin, marketplace="US"):
        return [{"asin": asin, "seller_id": "S1",
                 "snapshot_date": date.today().isoformat(),
                 "is_buybox": True}]


@pytest.fixture()
def scrape_svc(session_factory, engine):
    return ScrapeService(
        engine=engine, pdp_scraper=_FakePdp(),
        search_scraper=_FakeSearch(), offer_scraper=_FakeOffer(),
        redis_client=False,  # False = 明确禁用 Redis，走内存降级
    )


class TestScrapeService:
    def test_scrape_pdp_now_success_and_save(self, scrape_svc, engine):
        results = scrape_svc.scrape_pdp_now(["B0DLQ8MVF7"])
        assert results[0]["ok"] is True
        assert results[0]["saved"] is True
        snap = engine.get_latest_snapshot("B0DLQ8MVF7", "asin_snapshots")
        assert snap is not None and snap["title"] == "T B0DLQ8MVF7"

    def test_scrape_pdp_now_bot_blocked_continues(self, engine):
        fake_pdp = _FakePdp(block_asins={"B0AAAAAAAA"})
        fake_pdp.cooldown_remaining = 3600.0  # 模拟 BaseScraper 已进入软冷却
        svc = ScrapeService(
            engine=engine,
            pdp_scraper=fake_pdp,
            search_scraper=_FakeSearch(), offer_scraper=_FakeOffer(),
            redis_client=False)
        results = svc.scrape_pdp_now(["B0AAAAAAAA", "B0BBBBBBBB"])
        assert results[0]["ok"] is False
        assert "bot_blocked" in results[0]["error"]
        assert results[1]["ok"] is True
        # bot block 登记进全局冷却表
        assert "pdp" in get_cooldowns()

    def test_scrape_pdp_now_generic_error(self, engine):
        svc = ScrapeService(
            engine=engine, pdp_scraper=_FakePdp(fail_asins={"B0CCCCCCCC"}),
            search_scraper=_FakeSearch(), offer_scraper=_FakeOffer(),
            redis_client=False)
        results = svc.scrape_pdp_now(["B0CCCCCCCC"])
        assert results[0]["ok"] is False
        assert "RuntimeError" in results[0]["error"]

    def test_scrape_keyword_now_ok(self, scrape_svc, engine):
        ok, items = scrape_svc.scrape_keyword_now("dog shirt")
        assert ok is True
        assert items[0]["keyword"] == "dog shirt"
        snap = engine.get_latest_snapshot("B0DLQ8MVF7",
                                          "keyword_rank_snapshots")
        assert snap is not None and snap["natural_rank"] == 3

    def test_scrape_keyword_now_blocked(self, engine):
        svc = ScrapeService(
            engine=engine, pdp_scraper=_FakePdp(),
            search_scraper=_FakeSearch(fail_keywords={"bad kw"}),
            offer_scraper=_FakeOffer(), redis_client=False)
        ok, items = svc.scrape_keyword_now("bad kw")
        assert ok is False
        assert "bot_blocked" in items[0]

    def test_scrape_offers_now_ok(self, scrape_svc, engine):
        result = scrape_svc.scrape_offers_now("B0DLQ8MVF7")
        assert result["ok"] is True
        assert result["offers"][0]["seller_id"] == "S1"
        snap = engine.get_latest_snapshot("B0DLQ8MVF7", "offer_snapshots")
        assert snap is not None and snap["is_buybox"] is True


class TestSubmitScrape:
    def test_submit_and_poll_to_completion(self, engine):
        svc = ScrapeService(
            engine=engine, pdp_scraper=_FakePdp(),
            search_scraper=_FakeSearch(), offer_scraper=_FakeOffer(),
            redis_client=False)
        task_id = svc.submit_scrape(["B0DDDDDDDD", "B0EEEEEEEE"],
                                    channels=["pdp"])
        assert isinstance(task_id, str) and task_id
        # 轮询到终态（后台线程执行很快）
        deadline = time.time() + 5
        status = None
        while time.time() < deadline:
            status = svc.get_task_status(task_id)
            if status and status["state"] in (
                    "completed", "completed_with_errors", "failed"):
                break
            time.sleep(0.05)
        assert status is not None
        assert status["state"] == "completed"
        assert status["progress"] == 2
        assert status["total"] == 2
        assert status["errors"] == []

    def test_submit_records_errors(self, engine):
        svc = ScrapeService(
            engine=engine,
            pdp_scraper=_FakePdp(fail_asins={"B0FFFFFFFF"}),
            search_scraper=_FakeSearch(), offer_scraper=_FakeOffer(),
            redis_client=False)
        task_id = svc.submit_scrape(["B0FFFFFFFF"], channels=["pdp"])
        deadline = time.time() + 5
        status = None
        while time.time() < deadline:
            status = svc.get_task_status(task_id)
            if status and status["state"] in (
                    "completed", "completed_with_errors", "failed"):
                break
            time.sleep(0.05)
        assert status["state"] == "completed_with_errors"
        assert any("RuntimeError" in e for e in status["errors"])

    def test_get_task_status_unknown_id_none(self, scrape_svc):
        assert scrape_svc.get_task_status("deadbeef") is None

    def test_submit_offers_channel(self, engine):
        svc = ScrapeService(
            engine=engine, pdp_scraper=_FakePdp(),
            search_scraper=_FakeSearch(), offer_scraper=_FakeOffer(),
            redis_client=False)
        task_id = svc.submit_scrape(["B0GGGGGGGG"], channels=["offers"])
        deadline = time.time() + 5
        status = None
        while time.time() < deadline:
            status = svc.get_task_status(task_id)
            if status and status["state"] in (
                    "completed", "completed_with_errors", "failed"):
                break
            time.sleep(0.05)
        assert status["state"] == "completed"
        assert engine.get_latest_snapshot("B0GGGGGGGG",
                                          "offer_snapshots") is not None


class TestCooldownRegistry:
    def test_register_and_expire(self):
        register_cooldown("pdp", time.time() + 0.2)
        assert "pdp" in get_cooldowns()
        time.sleep(0.25)
        assert "pdp" not in get_cooldowns()


# ---------------------------------------------------------------------------
# MetricsService
# ---------------------------------------------------------------------------

class TestMetricsService:
    def _seed(self, engine):
        today = date.today()
        engine.save_snapshot(
            {"asin": "B0AAAAAAAA", "snapshot_date": today,
             "price_current": 110.0, "rating_count": 120},
            "asin_snapshots")
        engine.save_snapshot(
            {"asin": "B0AAAAAAAA", "snapshot_date": today - timedelta(days=31),
             "price_current": 100.0, "rating_count": 100},
            "asin_snapshots")
        engine.save_snapshot(
            {"asin": "B0BBBBBBBB", "snapshot_date": today,
             "rating_count": 200},
            "asin_snapshots")
        engine.save_snapshot(
            {"asin": "B0BBBBBBBB",
             "snapshot_date": today - timedelta(days=31), "rating_count": 100},
            "asin_snapshots")

    def test_asin_report(self, engine):
        self._seed(engine)
        svc = MetricsService(calculator=__import__(
            "src.metrics.calculator", fromlist=["MetricsCalculator"]
        ).MetricsCalculator(engine=engine))
        report = svc.asin_report("B0AAAAAAAA", days=30)
        assert report["price_change"]["change"] == pytest.approx(10.0)
        assert report["review_velocity"] == pytest.approx(20 / 31)

    def test_list_reports_sorted_by_review_velocity(self, engine):
        self._seed(engine)
        calc = __import__("src.metrics.calculator", fromlist=[
            "MetricsCalculator"]).MetricsCalculator(engine=engine)
        svc = MetricsService(calculator=calc)
        reports = svc.list_reports(days=30, sort_by="review_velocity")
        assert [r["asin"] for r in reports] == ["B0BBBBBBBB", "B0AAAAAAAA"]

    def test_find_surging(self, engine):
        self._seed(engine)
        calc = __import__("src.metrics.calculator", fromlist=[
            "MetricsCalculator"]).MetricsCalculator(engine=engine)
        svc = MetricsService(calculator=calc)
        result = svc.find_surging(min_review_velocity=3.0, window_days=30)
        assert [r["asin"] for r in result] == ["B0BBBBBBBB"]


# ---------------------------------------------------------------------------
# HealthService
# ---------------------------------------------------------------------------

class TestHealthService:
    def test_status_all_green_with_sqlite(self, session_factory, engine):
        svc = HealthService(
            session_factory=session_factory,
            quota_limits={"asins": 10, "search": 5},
        )
        status = svc.status()
        assert status["db_ok"] is True
        assert status["quota"]["asins"] == {"used": None, "limit": 10}
        assert status["cooldown_active"] is False
        assert status["pool_healthy"] is None  # 未配置池 → 未知

    def test_cooldown_active_detected(self, session_factory):
        register_cooldown("pdp", time.time() + 60)
        svc = HealthService(session_factory=session_factory)
        assert svc.status()["cooldown_active"] is True

    def test_db_down_reports_false(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        # 指向不存在表的内存库也能 SELECT 1 → 用坏 factory 模拟故障
        def broken_factory():
            raise RuntimeError("db down")

        svc = HealthService(session_factory=broken_factory)
        assert svc.status()["db_ok"] is False

    def test_pool_healthy_via_ip_pool(self, session_factory):
        class FakePool:
            def healthy_count(self):
                return 3

        svc = HealthService(session_factory=session_factory,
                            ip_pool=FakePool())
        assert svc.status()["pool_healthy"] == 3
