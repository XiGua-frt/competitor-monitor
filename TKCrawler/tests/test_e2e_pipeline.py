"""CLI 端到端闭环测试 (Agent L)。

用 mock scraper (不发真实请求) + SQLite 内存库 (不连真实 PG)
走通: CLI scrape shop → SnapshotDiffEngine.save_snapshot → CLI report 指标计算。
SQLite 建表方式与 tests/test_metrics.py 一致 (Base.metadata.create_all)。
"""

import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402
from src.database.models import Base, ProductSnapshot  # noqa: E402
from src.metrics import MetricsCalculator  # noqa: E402

PRODUCT_ID = "P_E2E"


class FakeShopScraper:
    """不发请求的 ShopProductScraper 替身, 返回固定商品快照数据。"""

    def __init__(self, client=None, **kw):
        pass

    def scrape_category(self, category_url):
        return [PRODUCT_ID]

    def scrape_product(self, product_id):
        return {
            "product_id": product_id,
            "snapshot_date": date.today(),
            "title": "E2E Test Product",
            "category_l1": "Home",
            "category_l2": "Lighting",
            "category_l3": "LED Strips",
            "price_current": Decimal("19.90"),
            "sales_30d": 750,
            "gmv_30d_est": Decimal("1500.00"),
            "creator_count": 120,
            "video_count_7d": 45,
        }


@pytest.fixture
def sqlite_env():
    """SQLite 内存库 + CLI 依赖注入 (scraper/engine/calculator 全部指向内存库)。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    import src.database.connection as conn_mod
    original_create_db_engine = conn_mod.create_db_engine

    # CLI check_db / init-db 探测 → 指向 SQLite
    conn_mod.create_db_engine = lambda *a, **kw: engine
    main.check_db = lambda: None

    # scrape 路径: 真 SnapshotDiffEngine 绑定 SQLite
    from src.database import SnapshotDiffEngine as RealEngine
    main.SnapshotDiffEngine = lambda: RealEngine(session_factory=factory)
    main.ShopProductScraper = FakeShopScraper

    yield factory

    conn_mod.create_db_engine = original_create_db_engine
    engine.dispose()


class TestShopToMetricsE2E:
    def test_shop_save_report_closed_loop(self, sqlite_env, capsys):
        factory = sqlite_env

        # 1) CLI 采集 shop 并写快照
        code = main.main(["scrape", "shop", "--category-url", "http://cat",
                          "--limit", "5"])
        assert code == 0

        # 2) SQLite 中已有快照行
        with factory() as session:
            row = session.execute(
                select(ProductSnapshot).where(
                    ProductSnapshot.product_id == PRODUCT_ID)
            ).scalar_one()
            assert row.sales_30d == 750
            assert row.gmv_30d_est == Decimal("1500.00")
            assert row.category_l2 == "Lighting"

        # 3) CLI report 计算派生指标 (计算器绑定同一内存库)
        main.MetricsCalculator = lambda: MetricsCalculator(
            session_factory=factory)
        code = main.main(["report", "--product-id", PRODUCT_ID])
        assert code == 0

        # scrape 进度行与 JSON 同在 stdout, 取首个 "{" 起的 JSON 块
        out = capsys.readouterr().out
        payload = json.loads(out[out.index("{"):])
        assert payload["product_id"] == PRODUCT_ID
        # content_efficiency = 1500 / 45
        assert payload["content_efficiency"] == pytest.approx(1500.0 / 45.0)
        # creator_penetration = 120 / 750
        assert payload["creator_penetration"] == pytest.approx(120.0 / 750.0)
        # saturation_index = (120 * 45) / 1500
        assert payload["saturation_index"] == pytest.approx(
            (120.0 * 45.0) / 1500.0)
        # surge_coefficient: 单快照无历史 → 差分不可得 → None
        assert payload["surge_coefficient"] is None

    def test_upsert_idempotent(self, sqlite_env, capsys):
        """同一商品重复采集 → upsert 不产生重复行。"""
        factory = sqlite_env
        for _ in range(2):
            assert main.main(["scrape", "shop",
                              "--category-url", "http://cat"]) == 0
        with factory() as session:
            rows = session.execute(
                select(ProductSnapshot).where(
                    ProductSnapshot.product_id == PRODUCT_ID)
            ).scalars().all()
        assert len(rows) == 1
