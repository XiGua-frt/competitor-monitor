"""MetricsCalculator 单测：sqlite 内存库构造两三天快照数据。

覆盖点（CONTRACT_V2）:
    price_change 复用 calculate_growth / bsr_growth JSON 首条 rank /
    review_velocity 每日均值与除零 None / availability_downtime 不可买天数 /
    buybox_flips 归属变化计数 / asin_report 汇总 / surging_products 排序。
"""

from datetime import date, timedelta

import pytest

from src.metrics.calculator import MetricsCalculator


def _mk(asin, snap_date, price=None, rating_count=None, bsr_ranks=None,
        availability=None):
    data = {"asin": asin, "snapshot_date": snap_date}
    if price is not None:
        data["price_current"] = price
    if rating_count is not None:
        data["rating_count"] = rating_count
    if bsr_ranks is not None:
        data["bsr_ranks"] = bsr_ranks
    if availability is not None:
        data["availability"] = availability
    return data


@pytest.fixture()
def calc(session_factory, engine):
    return MetricsCalculator(session_factory=session_factory, engine=engine)


class TestPriceChange:
    def test_price_change_normal(self, calc, engine):
        today = date.today()
        engine.save_snapshot(_mk("B0AAAAAAAA", today, price=110.0), "asin_snapshots")
        engine.save_snapshot(
            _mk("B0AAAAAAAA", today - timedelta(days=31), price=100.0),
            "asin_snapshots")
        result = calc.price_change("B0AAAAAAAA", days=30)
        assert result["latest"] == 110.0
        assert result["past"] == 100.0
        assert result["change"] == pytest.approx(10.0)
        assert result["change_rate"] == pytest.approx(0.1)

    def test_price_change_no_past_snapshot_none(self, calc, engine):
        engine.save_snapshot(
            _mk("B0BBBBBBBB", date.today(), price=50.0), "asin_snapshots")
        result = calc.price_change("B0BBBBBBBB", days=30)
        assert result == {"latest": None, "past": None,
                          "change": None, "change_rate": None}

    def test_price_change_zero_division(self, calc, engine):
        today = date.today()
        engine.save_snapshot(_mk("B0CCCCCCCC", today, price=80.0), "asin_snapshots")
        engine.save_snapshot(
            _mk("B0CCCCCCCC", today - timedelta(days=31), price=0),
            "asin_snapshots")
        result = calc.price_change("B0CCCCCCCC", days=30)
        assert result["latest"] == 80.0
        assert result["past"] == 0.0
        assert result["change"] is None
        assert result["change_rate"] is None

    def test_price_change_missing_field_none(self, calc, engine):
        today = date.today()
        engine.save_snapshot(_mk("B0DDDDDDDD", today), "asin_snapshots")
        engine.save_snapshot(
            _mk("B0DDDDDDDD", today - timedelta(days=31)), "asin_snapshots")
        result = calc.price_change("B0DDDDDDDD", days=30)
        assert result["change"] is None


class TestBsrGrowth:
    def test_bsr_growth_from_json_first_rank(self, calc, engine):
        today = date.today()
        engine.save_snapshot(
            _mk("B0EEEEEEEE", today, bsr_ranks=[{"category": "T-Shirts",
                                                 "rank": 500}]),
            "asin_snapshots")
        engine.save_snapshot(
            _mk("B0EEEEEEEE", today - timedelta(days=31),
                bsr_ranks=[{"category": "T-Shirts", "rank": 1000}]),
            "asin_snapshots")
        result = calc.bsr_growth("B0EEEEEEEE", days=30)
        assert result["latest"] == 500
        assert result["past"] == 1000
        assert result["change"] == pytest.approx(-500.0)
        assert result["change_rate"] == pytest.approx(-0.5)

    def test_bsr_growth_missing_bsr_none(self, calc, engine):
        today = date.today()
        engine.save_snapshot(_mk("B0FFFFFFFF", today), "asin_snapshots")
        engine.save_snapshot(
            _mk("B0FFFFFFFF", today - timedelta(days=5)), "asin_snapshots")
        assert calc.bsr_growth("B0FFFFFFFF", days=30)["change"] is None


class TestReviewVelocity:
    def test_review_velocity_daily_average(self, calc, engine):
        today = date.today()
        engine.save_snapshot(
            _mk("B0GGGGGGGG", today, rating_count=104), "asin_snapshots")
        engine.save_snapshot(
            _mk("B0GGGGGGGG", today - timedelta(days=31), rating_count=84),
            "asin_snapshots")
        assert calc.review_velocity("B0GGGGGGGG", days=30) \
            == pytest.approx(20 / 31)

    def test_review_velocity_zero_division_none(self, calc, engine):
        today = date.today()
        engine.save_snapshot(
            _mk("B0HHHHHHHH", today, rating_count=50), "asin_snapshots")
        engine.save_snapshot(
            _mk("B0HHHHHHHH", today - timedelta(days=31), rating_count=50),
            "asin_snapshots")
        assert calc.review_velocity("B0HHHHHHHH", days=30) == pytest.approx(0.0)

    def test_review_velocity_missing_data_none(self, calc):
        assert calc.review_velocity("B0IIIIIIII", days=30) is None


class TestAvailabilityDowntime:
    def test_downtime_counts_unavailable_days(self, calc, engine):
        today = date.today()
        engine.save_snapshot(
            _mk("B0JJJJJJJJ", today, availability="In Stock"), "asin_snapshots")
        engine.save_snapshot(
            _mk("B0JJJJJJJJ", today - timedelta(days=1),
                availability="Currently unavailable"), "asin_snapshots")
        engine.save_snapshot(
            _mk("B0JJJJJJJJ", today - timedelta(days=2),
                availability="In Stock"), "asin_snapshots")
        assert calc.availability_downtime("B0JJJJJJJJ", days=30) == 1

    def test_downtime_no_data_zero(self, calc):
        assert calc.availability_downtime("B0KKKKKKKK", days=30) == 0


class TestBuyboxFlips:
    def test_flip_counting(self, calc, engine):
        today = date.today()

        def offer(d, seller, buybox):
            return {"asin": "B0LLLLLLLL", "seller_id": seller,
                    "snapshot_date": today - timedelta(days=d),
                    "is_buybox": buybox}

        # Day-2: S1 拿 box → Day-1: S2 → Day-0: S1  → 2 次翻转
        rows = [
            offer(2, "S1", True), offer(2, "S2", False),
            offer(1, "S2", True), offer(1, "S1", False),
            offer(0, "S1", True), offer(0, "S2", False),
        ]
        for r in rows:
            assert engine.save_snapshot(r, "offer_snapshots")
        assert calc.buybox_flips("B0LLLLLLLL", days=30) == 2

    def test_no_flips_same_seller(self, calc, engine):
        today = date.today()
        for d in (0, 1):
            engine.save_snapshot(
                {"asin": "B0MMMMMMMM", "seller_id": "S9",
                 "snapshot_date": today - timedelta(days=d),
                 "is_buybox": True},
                "offer_snapshots")
        assert calc.buybox_flips("B0MMMMMMMM", days=30) == 0

    def test_no_data_zero(self, calc):
        assert calc.buybox_flips("B0NNNNNNNN", days=30) == 0


class TestAsinReport:
    def test_report_shape(self, calc, engine):
        today = date.today()
        engine.save_snapshot(
            _mk("B0OOOOOOOO", today, price=120.0, rating_count=100,
                availability="In Stock"), "asin_snapshots")
        engine.save_snapshot(
            _mk("B0OOOOOOOO", today - timedelta(days=31), price=100.0,
                rating_count=90, availability="In Stock"), "asin_snapshots")
        report = calc.asin_report("B0OOOOOOOO", days=30)
        assert report["asin"] == "B0OOOOOOOO"
        assert report["window_days"] == 30
        assert report["price_change"]["change"] == pytest.approx(20.0)
        assert report["bsr_change"]["change"] is None
        assert report["review_velocity"] == pytest.approx(10 / 31)
        assert report["availability_downtime"] == 0
        assert report["buybox_flips"] == 0

    def test_report_missing_data_none_semantics(self, calc):
        report = calc.asin_report("B0PPPPPPPP", days=7)
        assert report["price_change"]["latest"] is None
        assert report["review_velocity"] is None


class TestSurgingProducts:
    def test_surging_sorted_desc(self, calc, engine):
        today = date.today()
        # A: 2/day, B: 10/day, C: 0.5/day
        engine.save_snapshot(
            _mk("B0AAAAAAAA", today, rating_count=120), "asin_snapshots")
        engine.save_snapshot(
            _mk("B0AAAAAAAA", today - timedelta(days=31), rating_count=100),
            "asin_snapshots")
        engine.save_snapshot(
            _mk("B0BBBBBBBB", today, rating_count=200), "asin_snapshots")
        engine.save_snapshot(
            _mk("B0BBBBBBBB", today - timedelta(days=31), rating_count=100),
            "asin_snapshots")
        engine.save_snapshot(
            _mk("B0CCCCCCCC", today, rating_count=105), "asin_snapshots")
        engine.save_snapshot(
            _mk("B0CCCCCCCC", today - timedelta(days=31), rating_count=100),
            "asin_snapshots")
        result = calc.surging_products(min_review_velocity=0.5, window_days=30)
        assert [r["asin"] for r in result] == ["B0BBBBBBBB", "B0AAAAAAAA"]
        assert result[0]["review_velocity"] == pytest.approx(100 / 31)

    def test_surging_threshold_filters(self, calc, engine):
        today = date.today()
        engine.save_snapshot(
            _mk("B0QQQQQQQQ", today, rating_count=103), "asin_snapshots")
        engine.save_snapshot(
            _mk("B0QQQQQQQQ", today - timedelta(days=31), rating_count=100),
            "asin_snapshots")
        assert calc.surging_products(min_review_velocity=5.0,
                                     window_days=30) == []
