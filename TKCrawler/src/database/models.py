"""SQLAlchemy 2.0 风格 ORM 模型 — 4 张快照表。

分区映射策略说明:
    product_snapshots 在 PostgreSQL 中按 snapshot_date 做 RANGE 分区
    (分区 DDL 见 migrations/versions/001_initial_schema.py 的 raw SQL)。
    本模块的 ORM 模型 **只做映射, 不负责建分区表**:
    - 模型声明与分区 DDL 保持同构 (同表名/同列/同主键),
      ORM 的 CRUD 在分区表上完全透明;
    - Base.metadata.create_all() 在 PostgreSQL 上**不应**对
      product_snapshots 生效 (建表走迁移脚本的 raw DDL),
      初始化请使用 scripts/init_db.py;
    - 其余 3 张表 (video/creator/hashtag) 无分区, 由普通建表语句创建。

    ⚠️ 主键设计说明: PostgreSQL 要求分区键必须包含在主键/唯一约束中,
    因此所有快照表主键均为 (实体ID, snapshot_date), 同一天内重复写入
    走 upsert (见 snapshot_engine.SnapshotDiffEngine.save_snapshot)。
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import Date, DateTime, Index, Integer, Numeric, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """所有快照表模型的公共基类。"""


class ProductSnapshot(Base):
    """商品每日快照 (PostgreSQL 按 snapshot_date RANGE 分区, ORM 仅映射)。

    字段对应 Agent D 交付的 15 核心字段 + created_at。
    """

    __tablename__ = "product_snapshots"

    # 主键 (product_id, snapshot_date) — 分区键必须含于主键
    product_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    snapshot_date: Mapped[date] = mapped_column(Date, primary_key=True)

    title: Mapped[Optional[str]] = mapped_column(String(500))
    category_l1: Mapped[Optional[str]] = mapped_column(String(100))
    category_l2: Mapped[Optional[str]] = mapped_column(String(100))
    category_l3: Mapped[Optional[str]] = mapped_column(String(100))
    price_current: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    price_original: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    sales_30d: Mapped[Optional[int]] = mapped_column(Integer)
    gmv_30d_est: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 2))
    listed_date: Mapped[Optional[date]] = mapped_column(Date)
    shop_id: Mapped[Optional[str]] = mapped_column(String(50))
    shop_name: Mapped[Optional[str]] = mapped_column(String(200))
    creator_count: Mapped[Optional[int]] = mapped_column(Integer)
    video_count_7d: Mapped[Optional[int]] = mapped_column(Integer)
    review_count: Mapped[Optional[int]] = mapped_column(Integer)
    rating: Mapped[Optional[Decimal]] = mapped_column(Numeric(3, 2))

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("idx_product_snapshots_shop", "shop_id"),
        Index("idx_product_snapshots_category", "category_l1", "category_l2"),
        {"comment": "商品快照表 (PG RANGE 分区, 分区 DDL 由迁移脚本管理)"},
    )


class VideoSnapshot(Base):
    """商品关联视频每日快照。

    主键 (video_id, snapshot_date): 同一视频每天一条快照,
    支持按差分计算播放/点赞增速。
    """

    __tablename__ = "video_snapshots"

    video_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    snapshot_date: Mapped[date] = mapped_column(Date, primary_key=True)

    product_id: Mapped[Optional[str]] = mapped_column(String(50))
    author_id: Mapped[Optional[str]] = mapped_column(String(50))
    publish_time: Mapped[Optional[datetime]] = mapped_column(DateTime)
    views: Mapped[Optional[int]] = mapped_column(Integer)
    likes: Mapped[Optional[int]] = mapped_column(Integer)
    comments: Mapped[Optional[int]] = mapped_column(Integer)
    shares: Mapped[Optional[int]] = mapped_column(Integer)
    duration: Mapped[Optional[int]] = mapped_column(Integer)  # 秒
    is_paid: Mapped[Optional[bool]] = mapped_column()  # Spark Ads 投流标识

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("idx_video_snapshots_product", "product_id", "snapshot_date"),
        {"comment": "视频快照表"},
    )


class CreatorSnapshot(Base):
    """达人主页每日快照。"""

    __tablename__ = "creator_snapshots"

    creator_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    snapshot_date: Mapped[date] = mapped_column(Date, primary_key=True)

    nickname: Mapped[Optional[str]] = mapped_column(String(200))
    follower_count: Mapped[Optional[int]] = mapped_column(Integer)
    avg_views_30d: Mapped[Optional[int]] = mapped_column(Integer)
    category_tags: Mapped[Optional[str]] = mapped_column(String(500))  # 逗号分隔标签
    product_count: Mapped[Optional[int]] = mapped_column(Integer)  # 橱窗商品数

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("idx_creator_snapshots_followers", "follower_count"),
        {"comment": "达人快照表"},
    )


class HashtagSnapshot(Base):
    """话题/标签每日快照。"""

    __tablename__ = "hashtag_snapshots"

    tag_name: Mapped[str] = mapped_column(String(200), primary_key=True)
    snapshot_date: Mapped[date] = mapped_column(Date, primary_key=True)

    views_total: Mapped[Optional[int]] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        {"comment": "话题快照表"},
    )
