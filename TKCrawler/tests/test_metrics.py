"""src/metrics 模块测试 (Agent I)。

测试策略 (不依赖真实 PostgreSQL):
    - SQLite 内存库 + Base.metadata.create_all() 建表
      (product_snapshots 的 PG RANGE 分区仅存在于迁移 raw DDL,
      SQLite 下按同构普通表建立, 分区行为不在本文件验证范围);
    - 样本数据覆盖: 正常值 / 零值分母 / 零值分子 / None 字段 / 无快照 /
      无历史快照 (无法差分) / 跨类目隔离;
    - 除零约定全模块断言: 分母 0/缺失 → None, 分子 0 → 0.0;
    - 飙升系数 = 商品 30d growth_rate ÷ 类目平均 growth_rate (SQL 聚合),
      用手工计算的期望值做 approx 断言;
    - sql_views.sql 的 PG DDL 为 PostgreSQL 专属 (DISTINCT ON /
      ::numeric / INTERVAL), SQLite 无法执行 — 仅做内容存在性检查;
      真实 PG 验证用 TEST_POSTGRES_URL 环境变量门控, 默认 skip。
"""

import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.database.models import Base, ProductSnapshot, VideoSnapshot
from src.metrics import MetricsCalculator

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SQL_VIEWS_PATH = PROJECT_ROOT / "src" / "metrics" / "sql_views.sql"
TEST_PG_URL = os.getenv("TEST_POSTGRES_URL")

# 样本基准日期: d1 为最新快照日, d0 与 d1 相距 35 天 (> 30 天窗口)
D0 = date(2026, 9, 1)
D1 = date(2026, 10, 6)


# ---------------------------------------------------------------------------
# 样本数据
# ---------------------------------------------------------------------------
def _snapshot(product_id, day, gmv=None, category_l1="Home",
              category_l2="Lighting", category_l3="LED Strips",
              video=45, creators=120, sales=750, **extra):
    """构造一条 product_snapshots 记录 (字段可按用例覆盖)。"""
    return ProductSnapshot(
        product_id=product_id,
        snapshot_date=day,
        title=f"Test Product {product_id}",
        category_l1=category_l1,
        category_l2=category_l2,
        category_l3=category_l3,
        price_current=Decimal("19.90"),
        sales_30d=sales,
        gmv_30d_est=Decimal(gmv) if gmv is not None else None,
        creator_count=creators,
        video_count_7d=video,
        **extra,
    )


def _seed_snapshots(session):
    """插入覆盖各除零/缺数分支的商品快照。

    类目 Lighting (category_l2) 内各商品 30d growth_rate:
        P_NORMAL      (1000 → 1500)  = 0.5
        P_GROW2       (2000 → 2600)  = 0.3
        P_ZERO_VIDEO  ( 800 →  960)  = 0.2
        P_ZERO_SALES  ( 500 →  550)  = 0.1
        P_ZERO_GMV    (    0 →    0)  past=0 → 剔除
        P_NO_PAST     无历史快照       → 剔除
        P_MISSING     ( 100 →  100)  = 0.0
    类目平均 = (0.5 + 0.3 + 0.2 + 0.1 + 0.0) / 5 = 0.22
    """
    rows = [
        # 双快照正常商品
        _snapshot("P_NORMAL", D0, gmv="1000.00"),
        _snapshot("P_NORMAL", D1, gmv="1500.00"),
        _snapshot("P_GROW2", D0, gmv="2000.00", video=10, creators=30, sales=500),
        _snapshot("P_GROW2", D1, gmv="2600.00", video=10, creators=30, sales=500),
        # 零值分母: video_count_7d = 0 → content_efficiency 除零
        _snapshot("P_ZERO_VIDEO", D0, gmv="800.00", video=0, creators=10, sales=200),
        _snapshot("P_ZERO_VIDEO", D1, gmv="960.00", video=0, creators=10, sales=200),
        # 零值分母: sales_30d = 0 → creator_penetration 除零
        _snapshot("P_ZERO_SALES", D0, gmv="500.00", video=5, creators=8, sales=0),
        _snapshot("P_ZERO_SALES", D1, gmv="550.00", video=5, creators=8, sales=0),
        # 零值分母: gmv_30d_est = 0 → saturation 除零; 且 past=0 → surge 剔除
        _snapshot("P_ZERO_GMV", D0, gmv="0.00", video=10, creators=5, sales=100),
        _snapshot("P_ZERO_GMV", D1, gmv="0.00", video=10, creators=5, sales=100),
        # 仅最新快照 (无可比历史) → 差分不可得
        _snapshot("P_NO_PAST", D1, gmv="300.00", video=3, creators=4, sales=50),
        # 数值型字段为 None (字段缺失 ≠ 零值)
        _snapshot("P_MISSING", D0, gmv="100.00", video=None, creators=None, sales=None),
        _snapshot("P_MISSING", D1, gmv="100.00", video=None, creators=None, sales=None),
        # 跨类目隔离: Kitchen 类目
        _snapshot("P_OTHER", D0, gmv="500.00", category_l1="Electronics",
                  category_l2="Kitchen", category_l3="Cookware"),
        _snapshot("P_OTHER", D1, gmv="1000.00", category_l1="Electronics",
                  category_l2="Kitchen", category_l3="Cookware"),
        # 全部类目字段为 None
        _snapshot("P_NOCAT", D1, gmv="400.00", category_l1=None, category_l2=None,
                  category_l3=None, video=8, creators=6, sales=60),
    ]
    session.add_all(rows)
    # 一条视频快照 (仅供 PG 门控测试的 views_growth 视图用; SQLite 不执行视图)
    session.add(VideoSnapshot(
        video_id="V001", snapshot_date=D1, product_id="P_NORMAL", views=1000,
    ))
    session.commit()


@pytest.fixture
def calc():
    """SQLite 内存库 + 已播种样本数据的 MetricsCalculator。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        _seed_snapshots(session)
    yield MetricsCalculator(session_factory=factory)
    engine.dispose()


# 期望值 (由样本数据手工推得)
LIGHTING_AVG_GROWTH = (0.5 + 0.3 + 0.2 + 0.1 + 0.0) / 5  # 0.22


# ---------------------------------------------------------------------------
# 指标 1: content_efficiency = gmv_30d_est / video_count_7d
# ---------------------------------------------------------------------------
class TestContentEfficiency:
    def test_normal(self, calc):
        assert calc.calculate_content_efficiency("P_NORMAL") == pytest.approx(1500.0 / 45.0)

    def test_zero_video_returns_none(self, calc):
        """除零策略: 分母为 0 → None (不是 0, 不抛异常)。"""
        assert calc.calculate_content_efficiency("P_ZERO_VIDEO") is None

    def test_zero_gmv_is_valid_zero(self, calc):
        """分子为 0 而分母正常 → 0.0 是合法计算结果。"""
        assert calc.calculate_content_efficiency("P_ZERO_GMV") == 0.0

    def test_missing_field_returns_none(self, calc):
        assert calc.calculate_content_efficiency("P_MISSING") is None

    def test_nonexistent_product_returns_none(self, calc):
        assert calc.calculate_content_efficiency("P_GHOST") is None


# ---------------------------------------------------------------------------
# 指标 2: creator_penetration = creator_count / sales_30d
# ---------------------------------------------------------------------------
class TestCreatorPenetration:
    def test_normal(self, calc):
        assert calc.calculate_creator_penetration("P_NORMAL") == pytest.approx(120.0 / 750.0)

    def test_zero_sales_returns_none(self, calc):
        """除零策略: sales_30d = 0 → None。"""
        assert calc.calculate_creator_penetration("P_ZERO_SALES") is None

    def test_missing_field_returns_none(self, calc):
        assert calc.calculate_creator_penetration("P_MISSING") is None

    def test_nonexistent_product_returns_none(self, calc):
        assert calc.calculate_creator_penetration("P_GHOST") is None


# ---------------------------------------------------------------------------
# 指标 3: saturation_index = (creator_count * video_count_7d) / gmv_30d_est
# ---------------------------------------------------------------------------
class TestSaturationIndex:
    def test_normal(self, calc):
        assert calc.calculate_saturation_index("P_NORMAL") == pytest.approx(120.0 * 45.0 / 1500.0)

    def test_zero_gmv_returns_none(self, calc):
        """除零策略: 分母 gmv_30d_est = 0 → None。"""
        assert calc.calculate_saturation_index("P_ZERO_GMV") is None

    def test_zero_numerator_is_valid_zero(self, calc):
        """video_count_7d = 0 但 gmv 正常 → 0.0 (无内容供给, 合法结果)。"""
        assert calc.calculate_saturation_index("P_ZERO_VIDEO") == 0.0

    def test_missing_field_returns_none(self, calc):
        assert calc.calculate_saturation_index("P_MISSING") is None

    def test_nonexistent_product_returns_none(self, calc):
        assert calc.calculate_saturation_index("P_GHOST") is None


# ---------------------------------------------------------------------------
# 指标 4: surge_coefficient = 商品 30d 增速 / 类目平均 30d 增速
# ---------------------------------------------------------------------------
class TestSurgeCoefficient:
    def test_ratio_to_category_average(self, calc):
        """P_NORMAL: 0.5 / 0.22 ≈ 2.2727 (类目平均含 5 个可比商品)。"""
        assert calc.calculate_surge_coefficient("P_NORMAL", "Lighting") == pytest.approx(
            0.5 / LIGHTING_AVG_GROWTH
        )

    def test_second_product_same_category(self, calc):
        assert calc.calculate_surge_coefficient("P_GROW2", "Lighting") == pytest.approx(
            0.3 / LIGHTING_AVG_GROWTH
        )

    def test_category_average_excludes_indivisible_products(self, calc):
        """类目平均应剔除 past=0 (P_ZERO_GMV) 与无历史快照 (P_NO_PAST) 的商品。"""
        assert calc._category_avg_growth("Lighting") == pytest.approx(LIGHTING_AVG_GROWTH)

    def test_category_isolation(self, calc):
        """Kitchen 类目独立计算, 不受 Lighting 商品影响。"""
        # P_OTHER: 500 → 1000, 类目内唯一可比商品 → 系数 = 1.0 / 1.0
        assert calc.calculate_surge_coefficient("P_OTHER", "Kitchen") == pytest.approx(1.0)
        assert calc._category_avg_growth("Kitchen") == pytest.approx(1.0)

    def test_match_on_any_category_level(self, calc):
        """category 参数与 l1/l2/l3 任一列相等即匹配。"""
        by_l1 = calc.calculate_surge_coefficient("P_NORMAL", "Home")
        by_l3 = calc.calculate_surge_coefficient("P_NORMAL", "LED Strips")
        assert by_l1 == pytest.approx(by_l3)

    def test_zero_growth_product_returns_zero(self, calc):
        """商品增速 0.0 而类目平均正常 → 0.0 (合法结果, 非除零)。"""
        assert calc.calculate_surge_coefficient("P_MISSING", "Lighting") == 0.0

    def test_no_past_snapshot_returns_none(self, calc):
        """商品无历史快照 → 差分不可得 → None。"""
        assert calc.calculate_surge_coefficient("P_NO_PAST", "Lighting") is None

    def test_zero_past_gmv_returns_none(self, calc):
        """past GMV = 0 → growth_rate 为 None (NULLIF 语义) → None。"""
        assert calc.calculate_surge_coefficient("P_ZERO_GMV", "Lighting") is None

    def test_unknown_category_returns_none(self, calc):
        assert calc.calculate_surge_coefficient("P_NORMAL", "NoSuchCategory") is None

    def test_nonexistent_product_returns_none(self, calc):
        assert calc.calculate_surge_coefficient("P_GHOST", "Lighting") is None

    def test_empty_args_return_none(self, calc):
        assert calc.calculate_surge_coefficient("", "Lighting") is None
        assert calc.calculate_surge_coefficient("P_NORMAL", "") is None


# ---------------------------------------------------------------------------
# 批量: calculate_all
# ---------------------------------------------------------------------------
class TestCalculateAll:
    def test_all_four_metrics(self, calc):
        result = calc.calculate_all("P_NORMAL", category="Lighting")
        assert set(result) == {
            "content_efficiency",
            "creator_penetration",
            "saturation_index",
            "surge_coefficient",
        }
        assert result["content_efficiency"] == pytest.approx(1500.0 / 45.0)
        assert result["creator_penetration"] == pytest.approx(120.0 / 750.0)
        assert result["saturation_index"] == pytest.approx(3.6)
        assert result["surge_coefficient"] == pytest.approx(0.5 / LIGHTING_AVG_GROWTH)

    def test_category_inferred_from_snapshot(self, calc):
        """不传 category 时从最新快照 category_l2 推断, 结果与显式传参一致。"""
        inferred = calc.calculate_all("P_NORMAL")
        explicit = calc.calculate_all("P_NORMAL", category="Lighting")
        assert inferred["surge_coefficient"] == pytest.approx(
            explicit["surge_coefficient"]
        )

    def test_no_category_fields_surge_none(self, calc):
        result = calc.calculate_all("P_NOCAT")
        assert result["surge_coefficient"] is None
        assert result["content_efficiency"] == pytest.approx(400.0 / 8.0)

    def test_nonexistent_product_all_none(self, calc):
        result = calc.calculate_all("P_GHOST")
        assert all(v is None for v in result.values())


# ---------------------------------------------------------------------------
# sql_views.sql 内容检查 (SQLite 无法执行 PG 专属 DDL)
# ---------------------------------------------------------------------------
class TestSqlViewsFile:
    def test_file_exists_with_required_views(self):
        content = SQL_VIEWS_PATH.read_text(encoding="utf-8")
        assert "CREATE OR REPLACE VIEW product_metrics" in content
        assert "CREATE OR REPLACE VIEW views_growth" in content
        # 除零语义与 Python 侧 None 策略对齐
        assert "NULLIF(" in content
        # 未验证声明必须保留
        assert "未经真实 PostgreSQL" in content or "待 PG 实测" in content or "未验证" in content


# ---------------------------------------------------------------------------
# PostgreSQL 门控测试 (默认 skip, 设置 TEST_POSTGRES_URL 后运行)
# 验证 sql_views.sql 的 DDL 可在真实 PG 上执行并正确查询。
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    not TEST_PG_URL,
    reason="需要真实 PostgreSQL: 设置环境变量 TEST_POSTGRES_URL 后运行 (默认 skip)",
)
def test_sql_views_on_postgres():
    """在真实 PG 上执行 sql_views.sql 并查询两个视图。

    注意: 此处 create_all 建的是非分区普通表 (分区 raw DDL 由迁移脚本
    管理, 不在本测试范围); 视图 DDL 对分区/普通表语义一致。
    """
    engine = create_engine(TEST_PG_URL)
    Base.metadata.create_all(engine)
    try:
        factory = sessionmaker(bind=engine)
        with factory() as session:
            session.add(_snapshot("P_PG1", D0, gmv="1000.00"))
            session.add(_snapshot("P_PG1", D1, gmv="1500.00"))
            session.add(VideoSnapshot(
                video_id="V_PG1", snapshot_date=D1, product_id="P_PG1", views=800,
            ))
            session.add(VideoSnapshot(
                video_id="V_PG1", snapshot_date=date(2026, 9, 29),
                product_id="P_PG1", views=500,
            ))
            session.commit()

        statements = [
            s.strip() for s in SQL_VIEWS_PATH.read_text(encoding="utf-8").split(";")
            if s.strip() and not all(
                line.strip().startswith("--") or not line.strip()
                for line in s.strip().splitlines()
            )
        ]
        with engine.begin() as conn:
            for stmt in statements:
                conn.execute(text(stmt))

        with engine.connect() as conn:
            metrics = conn.execute(
                text("SELECT * FROM product_metrics WHERE product_id = 'P_PG1'")
            ).mappings().one()
            assert float(metrics["content_efficiency"]) == pytest.approx(1500.0 / 45.0)
            assert float(metrics["creator_penetration"]) == pytest.approx(120.0 / 750.0)
            assert float(metrics["saturation_index"]) == pytest.approx(3.6)

            growth = conn.execute(
                # 视图为 LEFT JOIN 语义: 无 7 天前快照的 curr 行也输出(增量为 NULL),
                # 此处断言的是有历史快照的那一行
                text("SELECT * FROM views_growth WHERE video_id = 'V_PG1' "
                     "AND views_growth_7d IS NOT NULL")
            ).mappings().one()
            assert growth["views_growth_7d"] == 300
            assert float(growth["views_growth_rate_7d"]) == pytest.approx(0.6)
    finally:
        # 先清视图再清表: views_growth/product_metrics 依赖快照表,
        # 不先 DROP VIEW 会导致 drop_all 报 DependentObjectsStillExist
        with engine.begin() as conn:
            conn.execute(text("DROP VIEW IF EXISTS views_growth CASCADE"))
            conn.execute(text("DROP VIEW IF EXISTS product_metrics CASCADE"))
        Base.metadata.drop_all(engine)
        engine.dispose()
