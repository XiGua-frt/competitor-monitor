"""src/database 模块测试。

测试策略 (不依赖真实 PostgreSQL):
    - growth 计算逻辑纯函数化 (compute_growth_values), 直接单测;
    - save_snapshot 的 upsert SQL 语义用 SQLite 内存库验证
      (SQLite 方言的 insert.on_conflict_do_update 与 PG 语义一致);
    - product_snapshots 的 RANGE 分区 raw DDL 为 PostgreSQL 专属,
      SQLite 无法验证 — 相关断言仅做迁移文件内容检查,
      真实分区行为【待 PG 实测】;
    - 如需连真实 PG 验证, 设置环境变量 TEST_POSTGRES_URL 后运行,
      未设置时自动 skip (pytest.mark.skipif)。
"""

import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.database.connection import ensure_month_partition, get_database_url
from src.database.models import ProductSnapshot
from src.database.snapshot_engine import (
    SnapshotDiffEngine,
    compute_growth_values,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_001 = (
    PROJECT_ROOT / "src" / "database" / "migrations" / "versions" / "001_initial_schema.py"
)

TEST_PG_URL = None  # 由环境变量注入, 见 test_postgres_partition_ddl


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def sqlite_session_factory():
    """SQLite 内存库会话工厂 (StaticPool 保证同一连接, 供 on_conflict 测试)。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # 4 张表均为通用类型, SQLite 可直接建表;
    # product_snapshots 的 PG RANGE 分区只存在于迁移 raw DDL 中,
    # 此处建的是同构普通表 — 分区行为不可在 SQLite 上验证。
    from src.database.models import Base

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    yield factory
    engine.dispose()


@pytest.fixture
def engine_obj(sqlite_session_factory):
    return SnapshotDiffEngine(session_factory=sqlite_session_factory)


def _product_data(product_id="P001", day=None, gmv="1000.00", price="19.90", **extra):
    data = {
        "product_id": product_id,
        "snapshot_date": day or date(2026, 10, 1),
        "title": "LED Strip Light RGB",
        "category_l1": "Home",
        "category_l2": "Lighting",
        "category_l3": "LED Strips",
        "price_current": Decimal(price),
        "price_original": Decimal("29.90"),
        "sales_30d": 750,
        "gmv_30d_est": Decimal(gmv),
        "listed_date": date(2026, 1, 15),
        "shop_id": "S001",
        "shop_name": "BrightHome Store",
        "creator_count": 120,
        "video_count_7d": 45,
        "review_count": 300,
        "rating": Decimal("4.8"),
    }
    data.update(extra)
    return data


# ---------------------------------------------------------------------------
# 纯函数: growth 计算 (NULLIF 语义)
# ---------------------------------------------------------------------------
class TestComputeGrowthValues:
    def test_normal_growth(self):
        growth, rate = compute_growth_values(1500.0, 1000.0)
        assert growth == pytest.approx(500.0)
        assert rate == pytest.approx(0.5)

    def test_negative_growth(self):
        growth, rate = compute_growth_values(800.0, 1000.0)
        assert growth == pytest.approx(-200.0)
        assert rate == pytest.approx(-0.2)

    def test_past_zero_returns_none_rate(self):
        """NULLIF 语义: past=0 → growth_rate=None, 绝对增量保留。"""
        growth, rate = compute_growth_values(500.0, 0)
        assert growth == pytest.approx(500.0)
        assert rate is None

    def test_past_none_returns_all_none(self):
        assert compute_growth_values(500.0, None) == (None, None)

    def test_latest_none_returns_all_none(self):
        assert compute_growth_values(None, 1000.0) == (None, None)

    def test_decimal_inputs(self):
        growth, rate = compute_growth_values(Decimal("1500.00"), Decimal("1000.00"))
        assert growth == pytest.approx(500.0)
        assert rate == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# save_snapshot: upsert 语义 (SQLite 内存库)
# ---------------------------------------------------------------------------
class TestSaveSnapshot:
    def test_insert_new_snapshot(self, engine_obj, sqlite_session_factory):
        ok = engine_obj.save_snapshot(_product_data(), "product_snapshots")
        assert ok is True
        with sqlite_session_factory() as s:
            count = s.scalar(select(func.count()).select_from(ProductSnapshot))
        assert count == 1

    def test_upsert_updates_on_conflict(self, engine_obj, sqlite_session_factory):
        """同主键二次写入应更新而非新增/报错。"""
        assert engine_obj.save_snapshot(_product_data(gmv="1000.00"), "product_snapshots")
        assert engine_obj.save_snapshot(
            _product_data(gmv="1200.00", price="21.50", sales_30d=800),
            "product_snapshots",
        )
        with sqlite_session_factory() as s:
            rows = s.scalars(select(ProductSnapshot)).all()
        assert len(rows) == 1
        assert float(rows[0].gmv_30d_est) == pytest.approx(1200.0)
        assert float(rows[0].price_current) == pytest.approx(21.5)
        assert rows[0].sales_30d == 800

    def test_extra_keys_ignored(self, engine_obj):
        ok = engine_obj.save_snapshot(
            _product_data(unknown_field="whatever"), "product_snapshots"
        )
        assert ok is True

    def test_string_dates_coerced(self, engine_obj, sqlite_session_factory):
        ok = engine_obj.save_snapshot(
            _product_data(snapshot_date="2026-10-01", listed_date="2026-01-15"),
            "product_snapshots",
        )
        assert ok is True
        snap = engine_obj.get_latest_snapshot("P001")
        assert snap["snapshot_date"] == date(2026, 10, 1)

    def test_invalid_table_returns_false(self, engine_obj):
        assert engine_obj.save_snapshot(_product_data(), "users") is False
        assert engine_obj.save_snapshot(_product_data(), "") is False
        # 防注入: 任意非白名单字符串一律拒绝
        assert engine_obj.save_snapshot(_product_data(), "product_snapshots; DROP TABLE x") is False

    def test_missing_primary_key_returns_false(self, engine_obj):
        assert engine_obj.save_snapshot({"title": "no pk"}, "product_snapshots") is False

    def test_empty_data_returns_false(self, engine_obj):
        assert engine_obj.save_snapshot({}, "product_snapshots") is False

    def test_video_snapshot_upsert(self, engine_obj, sqlite_session_factory):
        from src.database.models import VideoSnapshot

        base = {
            "video_id": "V001",
            "snapshot_date": "2026-10-01",
            "product_id": "P001",
            "views": 1000,
            "likes": 100,
            "is_paid": True,
        }
        assert engine_obj.save_snapshot(base, "video_snapshots")
        assert engine_obj.save_snapshot(
            {**base, "views": 2000, "snapshot_date": "2026-10-02"}, "video_snapshots"
        )
        with sqlite_session_factory() as s:
            rows = s.scalars(select(VideoSnapshot).order_by(VideoSnapshot.snapshot_date)).all()
        assert len(rows) == 2  # 不同日期是两条快照
        assert rows[0].views == 1000 and rows[1].views == 2000

    def test_hashtag_snapshot(self, engine_obj):
        assert engine_obj.save_snapshot(
            {"tag_name": "tiktokmademebuyit", "snapshot_date": "2026-10-01",
             "views_total": 98_000_000_000},
            "hashtag_snapshots",
        )
        # hashtag 表不在 get_latest_snapshot 的商品语义内, 仅验证写入

    def test_creator_snapshot(self, engine_obj, sqlite_session_factory):
        from src.database.models import CreatorSnapshot

        assert engine_obj.save_snapshot(
            {"creator_id": "C001", "snapshot_date": "2026-10-01",
             "nickname": "creator_a", "follower_count": 50_000,
             "avg_views_30d": 12_000, "category_tags": "Home,LED",
             "product_count": 30},
            "creator_snapshots",
        )
        with sqlite_session_factory() as s:
            assert s.scalar(select(func.count()).select_from(CreatorSnapshot)) == 1


# ---------------------------------------------------------------------------
# get_latest_snapshot
# ---------------------------------------------------------------------------
class TestGetLatestSnapshot:
    def test_returns_latest(self, engine_obj):
        engine_obj.save_snapshot(_product_data(gmv="900.00", day=date(2026, 9, 28)),
                                 "product_snapshots")
        engine_obj.save_snapshot(_product_data(gmv="1100.00", day=date(2026, 10, 1)),
                                 "product_snapshots")
        snap = engine_obj.get_latest_snapshot("P001")
        assert snap["snapshot_date"] == date(2026, 10, 1)
        assert snap["gmv_30d_est"] == pytest.approx(1100.0)
        assert snap["title"] == "LED Strip Light RGB"

    def test_missing_product_returns_empty(self, engine_obj):
        assert engine_obj.get_latest_snapshot("NO_SUCH") == {}


# ---------------------------------------------------------------------------
# calculate_growth (SQLite 内存库)
# ---------------------------------------------------------------------------
class TestCalculateGrowth:
    def test_growth_over_7d(self, engine_obj):
        engine_obj.save_snapshot(_product_data(gmv="1000.00", day=date(2026, 9, 24)),
                                 "product_snapshots")
        engine_obj.save_snapshot(_product_data(gmv="1500.00", day=date(2026, 10, 1)),
                                 "product_snapshots")
        result = engine_obj.calculate_growth("P001", days=7)
        assert result["gmv_growth"] == pytest.approx(500.0)
        assert result["growth_rate"] == pytest.approx(0.5)

    def test_growth_uses_closest_snapshot_before_cutoff(self, engine_obj):
        """采集周期 2-3 天, 回溯点取 <= cutoff 的最新快照。"""
        engine_obj.save_snapshot(_product_data(gmv="800.00", day=date(2026, 9, 20)),
                                 "product_snapshots")
        engine_obj.save_snapshot(_product_data(gmv="1000.00", day=date(2026, 9, 23)),
                                 "product_snapshots")
        engine_obj.save_snapshot(_product_data(gmv="1300.00", day=date(2026, 10, 1)),
                                 "product_snapshots")
        result = engine_obj.calculate_growth("P001", days=7)
        # cutoff = 09-24, 应取 09-23 的 1000
        assert result["gmv_growth"] == pytest.approx(300.0)
        assert result["growth_rate"] == pytest.approx(0.3)

    def test_past_gmv_zero_rate_is_none(self, engine_obj):
        engine_obj.save_snapshot(_product_data(gmv="0.00", day=date(2026, 9, 24)),
                                 "product_snapshots")
        engine_obj.save_snapshot(_product_data(gmv="1500.00", day=date(2026, 10, 1)),
                                 "product_snapshots")
        result = engine_obj.calculate_growth("P001", days=7)
        assert result["gmv_growth"] == pytest.approx(1500.0)
        assert result["growth_rate"] is None  # NULLIF 语义

    def test_single_snapshot_returns_none(self, engine_obj):
        engine_obj.save_snapshot(_product_data(day=date(2026, 10, 1)), "product_snapshots")
        assert engine_obj.calculate_growth("P001", 7) == {
            "gmv_growth": None, "growth_rate": None,
        }

    def test_no_snapshot_returns_none(self, engine_obj):
        assert engine_obj.calculate_growth("NO_SUCH", 7) == {
            "gmv_growth": None, "growth_rate": None,
        }

    def test_invalid_days_returns_none(self, engine_obj):
        for bad in (0, -7):
            assert engine_obj.calculate_growth("P001", bad) == {
                "gmv_growth": None, "growth_rate": None,
            }


# ---------------------------------------------------------------------------
# 迁移文件内容检查 (分区 DDL 无法在 SQLite 验证, 【待 PG 实测】)
# ---------------------------------------------------------------------------
class TestMigrationContent:
    def test_migration_contains_partition_ddl(self):
        sql = MIGRATION_001.read_text(encoding="utf-8")
        assert "PARTITION BY RANGE (snapshot_date)" in sql
        assert "PARTITION OF product_snapshots" in sql
        assert "DEFAULT" in sql  # default 分区兜底
        assert 'PRIMARY KEY (product_id, snapshot_date)' in sql

    def test_migration_revisions(self):
        text = MIGRATION_001.read_text(encoding="utf-8")
        assert 'revision = "001"' in text
        assert "down_revision = None" in text


class TestConnectionHelpers:
    def test_database_url_env_override(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db:5432/testdb")
        assert get_database_url() == "postgresql://u:p@db:5432/testdb"

    def test_ensure_partition_skips_non_pg(self, sqlite_session_factory):
        """非 PG 方言应安全跳过, 不抛异常。"""
        engine = sqlite_session_factory.kw["bind"]
        assert ensure_month_partition(date(2026, 10, 7), engine) is False

    def test_ensure_partition_default_engine_no_nameerror(
            self, monkeypatch, sqlite_session_factory):
        """省略 engine 参数时应取模块内惰性默认 engine (_get_engine),
        而不是引用未定义变量抛 NameError (2026-10-08 E2E 踩坑)。
        这里预置缓存的 SQLite engine 验证: 走的是缓存实例且安全跳过,
        全程不创建真实 PG 连接。"""
        import src.database.connection as conn_mod

        sqlite_engine = sqlite_session_factory.kw["bind"]
        monkeypatch.setattr(conn_mod, "_engine_instance", sqlite_engine)

        assert ensure_month_partition(date(2026, 10, 7)) is False
        # 默认 engine 来自模块缓存 (_get_engine 命中), 未被重建
        assert conn_mod._engine_instance is sqlite_engine


# ---------------------------------------------------------------------------
# PostgreSQL 专属测试 (默认跳过; 设置 TEST_POSTGRES_URL 后执行)
# ---------------------------------------------------------------------------
PG_URL = __import__("os").environ.get("TEST_POSTGRES_URL")


@pytest.mark.skipif(
    PG_URL is None,
    reason="需要真实 PostgreSQL (设置 TEST_POSTGRES_URL); 分区 DDL【待 PG 实测】",
)
class TestPostgresPartition:
    """连真实 PG 验证分区 DDL — 本地无 PG 时自动跳过。"""

    @pytest.fixture(scope="class")
    def pg_engine(self):
        from src.database.models import Base

        engine = create_engine(PG_URL)
        # 防御: 先清掉可能存在的派生视图(如 metrics 门控测试创建的),
        # 再清 alembic_version, 否则 drop_all 后 upgrade 会被版本表判定为 no-op
        from sqlalchemy import text

        with engine.begin() as conn:
            conn.execute(text("DROP VIEW IF EXISTS views_growth CASCADE"))
            conn.execute(text("DROP VIEW IF EXISTS product_metrics CASCADE"))
        Base.metadata.drop_all(engine)
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        yield engine
        # 退场清理: 门控测试结束后把库恢复到"全空"状态(含 alembic_version),
        # 否则残留 version=001 + 空表会让 scripts/init_db.py 的 upgrade 变 no-op,
        # 真实快照落库时才会发现表不存在(2026-10-08 Wave 6 实测踩坑)
        try:
            Base.metadata.drop_all(engine)
            with engine.begin() as conn:
                conn.execute(text("DROP VIEW IF EXISTS views_growth CASCADE"))
                conn.execute(text("DROP VIEW IF EXISTS product_metrics CASCADE"))
                conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        except Exception:
            pass  # 清理失败不掩盖测试结果
        engine.dispose()

    def test_migration_creates_partitions(self, pg_engine):
        from alembic import command
        from alembic.config import Config

        cfg = Config(str(PROJECT_ROOT / "src/database/migrations/alembic.ini"))
        cfg.set_main_option("sqlalchemy.url", PG_URL)
        command.upgrade(cfg, "head")

        from sqlalchemy import text

        with pg_engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT relname FROM pg_class WHERE relispartition "
                "AND relname LIKE 'product_snapshots_%' ORDER BY relname"
            )).scalars().all()
        assert "product_snapshots_default" in rows
        assert "product_snapshots_202610" in rows

    def test_upsert_on_partitioned_table(self, pg_engine):
        from datetime import date as d

        eng = SnapshotDiffEngine()
        assert eng.save_snapshot(
            _product_data(day=d(2026, 10, 7)), "product_snapshots"
        )
        # 重复写入走分区表 upsert
        assert eng.save_snapshot(
            _product_data(day=d(2026, 10, 7), gmv="2000.00"), "product_snapshots"
        )
        snap = eng.get_latest_snapshot("P001")
        assert snap["gmv_30d_est"] == pytest.approx(2000.0)
