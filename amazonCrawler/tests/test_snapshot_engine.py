"""SnapshotDiffEngine 单测（SQLite 内存库，全离线）。

覆盖: upsert 幂等 / calculate_growth 正常·除零·缺快照 None 语义 /
非法表名拒绝 / 字段类型收敛。
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from src.database.models import AsinSnapshot, Base, OfferSnapshot
from src.database.snapshot_engine import TABLE_MODELS, SnapshotDiffEngine

ASIN = "B0DLQ8MVF7"


def _mk_engine(session_factory):
    return SnapshotDiffEngine(session_factory=session_factory)


def _row(session, asin, snap_date):
    return session.execute(
        select(AsinSnapshot).where(
            AsinSnapshot.asin == asin, AsinSnapshot.snapshot_date == snap_date
        )
    ).scalar_one_or_none()


class TestSaveSnapshotUpsert:
    """save_snapshot: upsert 语义 + 幂等。"""

    def test_insert_then_update_same_pk(self, engine, session_factory):
        d = date(2025, 11, 1)
        ok1 = engine.save_snapshot(
            {"asin": ASIN, "snapshot_date": d, "title": "v1",
             "price_current": "19.99", "marketplace": "US"},
            "asin_snapshots",
        )
        assert ok1 is True

        # 同主键第二次写入 → 更新非主键字段，不产生新行
        ok2 = engine.save_snapshot(
            {"asin": ASIN, "snapshot_date": d, "title": "v2",
             "price_current": 17.99},
            "asin_snapshots",
        )
        assert ok2 is True

        with session_factory() as session:
            row = _row(session, ASIN, d)
            assert row is not None
            assert row.title == "v2"
            assert row.price_current == Decimal("17.99")
            # 未在第二次提供的第一批字段保留 upsert 语义（未被清空）
            assert row.marketplace == "US"
            assert session.execute(select(AsinSnapshot)).scalars().all().__len__() == 1

    def test_repeated_write_is_idempotent(self, engine, session_factory):
        d = date(2025, 11, 2)
        payload = {"asin": ASIN, "snapshot_date": d, "title": "same",
                   "price_current": 10.00}
        for _ in range(3):
            assert engine.save_snapshot(payload, "asin_snapshots") is True
        with session_factory() as session:
            assert len(session.execute(select(AsinSnapshot)).scalars().all()) == 1

    def test_missing_pk_rejected(self, engine):
        assert engine.save_snapshot({"title": "no pk"}, "asin_snapshots") is False
        assert engine.save_snapshot({"asin": ASIN}, "asin_snapshots") is False

    def test_unknown_field_ignored(self, engine, session_factory):
        d = date(2025, 11, 3)
        ok = engine.save_snapshot(
            {"asin": ASIN, "snapshot_date": d, "title": "ok",
             "scraper_meta": "noise"},  # 非列字段应忽略
            "asin_snapshots",
        )
        assert ok is True
        with session_factory() as session:
            row = _row(session, ASIN, d)
            assert row.title == "ok"

    def test_offer_table_upsert(self, engine, session_factory):
        d = date(2025, 11, 4)
        ok = engine.save_snapshot(
            {"asin": ASIN, "seller_id": "A1SELLER", "snapshot_date": d,
             "seller_name": "SellerA", "price": 25.5, "is_buybox": True},
            "offer_snapshots",
        )
        assert ok is True
        with session_factory() as session:
            row = session.execute(
                select(OfferSnapshot).where(OfferSnapshot.seller_id == "A1SELLER")
            ).scalar_one()
            assert row.price == Decimal("25.5")
            assert row.is_buybox is True

    def test_empty_data_rejected(self, engine):
        assert engine.save_snapshot({}, "asin_snapshots") is False
        assert engine.save_snapshot("not a dict", "asin_snapshots") is False


class TestIllegalTable:
    """非法表名拒绝（白名单外）。"""

    def test_unknown_table(self, engine):
        assert engine.save_snapshot({"asin": ASIN}, "users") is False
        assert engine.save_snapshot({"asin": ASIN}, "asin_snapshots; DROP TABLE") is False

    def test_get_latest_unknown_table(self, engine):
        assert engine.get_latest_snapshot(ASIN, table="nope") is None

    def test_whitelist_contents(self):
        assert set(TABLE_MODELS) == {
            "asin_snapshots", "keyword_rank_snapshots", "offer_snapshots"
        }


class TestGetLatestSnapshot:
    def test_returns_latest_by_date(self, engine):
        engine.save_snapshot({"asin": ASIN, "snapshot_date": "2025-11-01",
                              "title": "old"}, "asin_snapshots")
        engine.save_snapshot({"asin": ASIN, "snapshot_date": "2025-11-05",
                              "title": "new"}, "asin_snapshots")
        snap = engine.get_latest_snapshot(ASIN)
        assert snap is not None
        assert snap["title"] == "new"
        assert snap["snapshot_date"] == date(2025, 11, 5)

    def test_no_record_returns_none(self, engine):
        assert engine.get_latest_snapshot("B000000000") is None


class TestCalculateGrowth:
    """calculate_growth 契约语义：None = 不可算，不兜底 0。"""

    def _seed(self, engine, price_by_date: dict):
        for d, price in price_by_date.items():
            assert engine.save_snapshot(
                {"asin": ASIN, "snapshot_date": d, "price_current": price},
                "asin_snapshots",
            )

    def test_normal_growth(self, engine):
        self._seed(engine, {"2025-11-01": 100.0, "2025-11-08": 110.0})
        r = engine.calculate_growth(ASIN, days=7)
        assert r["latest"] == 110.0
        assert r["past"] == 100.0
        assert r["change"] == pytest.approx(10.0)
        assert r["change_rate"] == pytest.approx(0.1)

    def test_no_snapshots_returns_none(self, engine):
        r = engine.calculate_growth("B000000000", days=7)
        assert r == {"latest": None, "past": None, "change": None, "change_rate": None}

    def test_no_past_snapshot_returns_none(self, engine):
        # 只有最新一条，无 7 天前可比快照
        self._seed(engine, {"2025-11-08": 100.0})
        r = engine.calculate_growth(ASIN, days=7)
        assert r == {"latest": None, "past": None, "change": None, "change_rate": None}

    def test_past_too_recent_returns_none(self, engine):
        # past 快照只差 1 天，不在 7 天窗口内
        self._seed(engine, {"2025-11-07": 100.0, "2025-11-08": 110.0})
        r = engine.calculate_growth(ASIN, days=7)
        assert r["past"] is None

    def test_division_by_zero_change_none(self, engine):
        # past == 0 → 除零 → change/change_rate 为 None（NULLIF 语义）
        self._seed(engine, {"2025-11-01": 0.0, "2025-11-08": 50.0})
        r = engine.calculate_growth(ASIN, days=7)
        assert r["latest"] == 50.0
        assert r["past"] == 0.0
        assert r["change"] is None
        assert r["change_rate"] is None

    def test_missing_field_returns_none(self, engine):
        # latest 有值但 past 快照缺字段 → None，不兜底
        engine.save_snapshot({"asin": ASIN, "snapshot_date": "2025-11-01",
                              "title": "no price"}, "asin_snapshots")
        engine.save_snapshot({"asin": ASIN, "snapshot_date": "2025-11-08",
                              "price_current": 42.0}, "asin_snapshots")
        r = engine.calculate_growth(ASIN, days=7)
        assert r == {"latest": None, "past": None, "change": None, "change_rate": None}

    def test_invalid_params_return_none_semantics(self, engine):
        for args in [("", 7), (ASIN, 0), (ASIN, -3)]:
            r = engine.calculate_growth(*args)
            assert r["change_rate"] is None

    def test_alternative_field(self, engine):
        engine.save_snapshot({"asin": ASIN, "snapshot_date": "2025-11-01",
                              "rating_count": 100}, "asin_snapshots")
        engine.save_snapshot({"asin": ASIN, "snapshot_date": "2025-11-08",
                              "rating_count": 150}, "asin_snapshots")
        r = engine.calculate_growth(ASIN, days=7, field="rating_count")
        assert r["change"] == pytest.approx(50.0)
        assert r["change_rate"] == pytest.approx(0.5)

    def test_unknown_field_returns_none_semantics(self, engine):
        r = engine.calculate_growth(ASIN, days=7, field="not_a_column")
        assert r == {"latest": None, "past": None, "change": None, "change_rate": None}


class TestTypeCoercion:
    """字段类型收敛：字符串 → date/Decimal/int/bool。"""

    def test_str_date_and_price_coerce(self, engine, session_factory):
        ok = engine.save_snapshot(
            {"asin": ASIN, "snapshot_date": "2025-11-10",  # 字符串日期
             "price_current": "19.99",                      # 字符串价格
             "rating_count": "104",                         # 字符串整数
             "comparison_ready": "true",                    # 字符串布尔
             "bsr_ranks": [{"category": "Clothing", "rank": 729415}],
             "rating": "4.6"},
            "asin_snapshots",
        )
        assert ok is True
        with session_factory() as session:
            row = _row(session, ASIN, date(2025, 11, 10))
            assert row.snapshot_date == date(2025, 11, 10)
            assert row.price_current == Decimal("19.99")
            assert row.rating_count == 104
            assert row.comparison_ready is True
            assert row.bsr_ranks == [{"category": "Clothing", "rank": 729415}]
            assert row.rating == Decimal("4.6")

    def test_empty_string_becomes_none(self, engine, session_factory):
        ok = engine.save_snapshot(
            {"asin": ASIN, "snapshot_date": "2025-11-11", "price_current": ""},
            "asin_snapshots",
        )
        assert ok is True
        with session_factory() as session:
            row = _row(session, ASIN, date(2025, 11, 11))
            assert row.price_current is None

    def test_json_column_roundtrip_sqlite(self, engine, session_factory):
        ranks = [{"category": "Clothing, Shoes & Jewelry", "rank": 729415},
                 {"category": "Men's T-Shirts", "rank": 1204}]
        engine.save_snapshot(
            {"asin": ASIN, "snapshot_date": "2025-11-12", "bsr_ranks": ranks},
            "asin_snapshots",
        )
        snap = engine.get_latest_snapshot(ASIN)
        assert snap["bsr_ranks"] == ranks


class TestDefaultSessionFactoryPath:
    """注入路径之外：非法 session_factory 数据应走异常分支返回 False 而非抛出。"""

    def test_db_error_returns_false(self):
        broken = sessionmaker(bind=create_engine("sqlite://"))  # 未建表
        eng = SnapshotDiffEngine(session_factory=broken)
        assert eng.save_snapshot(
            {"asin": ASIN, "snapshot_date": "2025-11-01"}, "asin_snapshots"
        ) is False
