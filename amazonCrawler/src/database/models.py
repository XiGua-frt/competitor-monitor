"""SQLAlchemy 2.0 风格 ORM 模型 — 三张快照表（Agent A 正式实现）。

主键设计（契约约定）: 主键 = 实体ID + snapshot_date，同一天重复写入走
upsert（见 snapshot_engine.SnapshotDiffEngine.save_snapshot）。

分区说明（PostgreSQL 生产环境）:
    asin_snapshots 数据量随时间线性增长，生产可按 snapshot_date 做 RANGE
    月分区（asin_snapshots_YYYYMM）。注意 PG 要求分区键必须包含在主键/唯一
    约束中，因此主键设计为 (asin, snapshot_date)，与分区方案兼容。
    建表入口 scripts/init_db.py 使用 Base.metadata.create_all() 创建普通表；
    若后续启用分区，需改用迁移脚本 raw DDL（PARTITION BY RANGE
    (snapshot_date) + 按月 CREATE TABLE ... PARTITION OF ...），ORM 映射在
    分区表上完全透明，无需改动。

JSON 列（bsr_ranks）双方言说明:
    使用 sqlalchemy.JSON 通用类型 — PG 方言渲染为 JSONB（原生 JSONB），
    SQLite 渲染为 TEXT + JSON 序列化钩子，两侧读写语义一致。
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """所有快照表模型的公共基类。"""


class AsinSnapshot(Base):
    """ASIN 每日快照（PG 生产可按 snapshot_date RANGE 分区，见模块 docstring）。"""

    __tablename__ = "asin_snapshots"

    # 主键 (asin, snapshot_date) — 分区键必须含于主键
    asin: Mapped[str] = mapped_column(String(10), primary_key=True)
    snapshot_date: Mapped[date] = mapped_column(Date, primary_key=True)

    marketplace: Mapped[Optional[str]] = mapped_column(String(20))
    title: Mapped[Optional[str]] = mapped_column(String(500))
    brand: Mapped[Optional[str]] = mapped_column(String(200))
    seller_name: Mapped[Optional[str]] = mapped_column(String(200))
    buybox_winner: Mapped[Optional[str]] = mapped_column(String(200))

    price_current: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    price_original: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    currency: Mapped[Optional[str]] = mapped_column(String(3), default="USD")
    price_scope: Mapped[Optional[str]] = mapped_column(String(30))
    availability: Mapped[Optional[str]] = mapped_column(String(100))
    coupon_text: Mapped[Optional[str]] = mapped_column(String(200))

    rating: Mapped[Optional[Decimal]] = mapped_column(Numeric(2, 1))
    rating_count: Mapped[Optional[int]] = mapped_column(Integer)

    bsr_root_category: Mapped[Optional[str]] = mapped_column(String(200))
    # [{category: str, rank: int}, ...]；用通用 JSON 保证 PG/SQLite 双方言
    bsr_ranks: Mapped[Optional[list]] = mapped_column(JSON)

    variant_status: Mapped[Optional[str]] = mapped_column(String(20))
    variant_asin: Mapped[Optional[str]] = mapped_column(String(10))

    comparison_ready: Mapped[Optional[bool]] = mapped_column(Boolean)
    region_verified: Mapped[Optional[bool]] = mapped_column(Boolean)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("idx_asin_snapshots_marketplace_date", "marketplace", "snapshot_date"),
        Index("idx_asin_snapshots_bsr_root", "bsr_root_category"),
        {"comment": "ASIN 每日快照表"},
    )


class KeywordRankSnapshot(Base):
    """关键词排名每日快照（自然位 + 广告位）。"""

    __tablename__ = "keyword_rank_snapshots"

    # 主键 (keyword, asin, snapshot_date)
    keyword: Mapped[str] = mapped_column(String(200), primary_key=True)
    asin: Mapped[str] = mapped_column(String(10), primary_key=True)
    snapshot_date: Mapped[date] = mapped_column(Date, primary_key=True)

    natural_rank: Mapped[Optional[int]] = mapped_column(Integer)
    ad_rank: Mapped[Optional[int]] = mapped_column(Integer)
    page: Mapped[Optional[int]] = mapped_column(Integer)
    result_count: Mapped[Optional[int]] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("idx_kwsnap_asin_date", "asin", "snapshot_date"),
        {"comment": "关键词排名快照表"},
    )


class OfferSnapshot(Base):
    """Offer（卖家报价）每日快照。"""

    __tablename__ = "offer_snapshots"

    # 主键 (asin, seller_id, snapshot_date)
    asin: Mapped[str] = mapped_column(String(10), primary_key=True)
    seller_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    snapshot_date: Mapped[date] = mapped_column(Date, primary_key=True)

    seller_name: Mapped[Optional[str]] = mapped_column(String(200))
    price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    is_buybox: Mapped[Optional[bool]] = mapped_column(Boolean)
    is_amazon_offered: Mapped[Optional[bool]] = mapped_column(Boolean)
    offer_count: Mapped[Optional[int]] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("idx_offer_snap_asin_date", "asin", "snapshot_date"),
        {"comment": "卖家报价快照表"},
    )
