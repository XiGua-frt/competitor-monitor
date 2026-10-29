"""初始 schema: 4 张快照表 (product_snapshots 为 RANGE 分区表)

Revision ID: 001
Revises:
Create Date: 2026-10-07

设计要点:
    - product_snapshots 按 snapshot_date 做 RANGE 分区, 必须 raw DDL
      (CREATE TABLE ... PARTITION BY RANGE), Alembic/ORM 均无法表达分区;
    - PostgreSQL 要求分区键必须包含在主键中, 故 PK = (product_id, snapshot_date);
    - 初始分区: 2026 年逐月分区 + default 分区兜底 (落在未建分区日期的
      数据写入 default, 后续可用 ensure_month_partition / 手动 SPLIT 补齐);
    - 其余 3 张表为普通表, 语义上同为主键 (实体ID, snapshot_date);
    - 非 PostgreSQL 方言 (如 SQLite 冒烟测试) 跳过执行。
"""
from alembic import op

revision = "001"
down_revision = None
branch_labels = None
depends_on = None

# 初始逐月分区范围: 2026-01 ~ 2027-01 (12 个月)
_INITIAL_MONTHS = [
    ("202601", "2026-01-01", "2026-02-01"),
    ("202602", "2026-02-01", "2026-03-01"),
    ("202603", "2026-03-01", "2026-04-01"),
    ("202604", "2026-04-01", "2026-05-01"),
    ("202605", "2026-05-01", "2026-06-01"),
    ("202606", "2026-06-01", "2026-07-01"),
    ("202607", "2026-07-01", "2026-08-01"),
    ("202608", "2026-08-01", "2026-09-01"),
    ("202609", "2026-09-01", "2026-10-01"),
    ("202610", "2026-10-01", "2026-11-01"),
    ("202611", "2026-11-01", "2026-12-01"),
    ("202612", "2026-12-01", "2027-01-01"),
]

_UPGRADE_DDL = [
    # 1. 商品快照表: RANGE 分区 + 初始分区 + default 分区
    """
    CREATE TABLE product_snapshots (
        product_id      VARCHAR(50) NOT NULL,
        snapshot_date   DATE NOT NULL,
        title           VARCHAR(500),
        category_l1     VARCHAR(100),
        category_l2     VARCHAR(100),
        category_l3     VARCHAR(100),
        price_current   NUMERIC(10,2),
        price_original  NUMERIC(10,2),
        sales_30d       INTEGER,
        gmv_30d_est     NUMERIC(15,2),
        listed_date     DATE,
        shop_id         VARCHAR(50),
        shop_name       VARCHAR(200),
        creator_count   INTEGER,
        video_count_7d  INTEGER,
        review_count    INTEGER,
        rating          NUMERIC(3,2),
        created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
        PRIMARY KEY (product_id, snapshot_date)
    ) PARTITION BY RANGE (snapshot_date)
    """,
] + [
    # 2. 逐月初始分区
    f"""
    CREATE TABLE product_snapshots_{suffix}
    PARTITION OF product_snapshots
    FOR VALUES FROM ('{start}') TO ('{end}')
    """
    for suffix, start, end in _INITIAL_MONTHS
] + [
    # 3. default 分区兜底, 防止越界数据写入失败
    """
    CREATE TABLE product_snapshots_default
    PARTITION OF product_snapshots DEFAULT
    """,
    # 4. 分区表上的索引 (在父表创建, PG 自动传播到所有分区)
    "CREATE INDEX idx_product_snapshots_date ON product_snapshots (snapshot_date)",
    "CREATE INDEX idx_product_snapshots_shop ON product_snapshots (shop_id)",
    "CREATE INDEX idx_product_snapshots_category ON product_snapshots (category_l1, category_l2)",
    # 5. 视频快照表
    """
    CREATE TABLE video_snapshots (
        video_id        VARCHAR(50) NOT NULL,
        snapshot_date   DATE NOT NULL,
        product_id      VARCHAR(50),
        author_id       VARCHAR(50),
        publish_time    TIMESTAMP,
        views           INTEGER,
        likes           INTEGER,
        comments        INTEGER,
        shares          INTEGER,
        duration        INTEGER,
        is_paid         BOOLEAN,
        created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
        PRIMARY KEY (video_id, snapshot_date)
    )
    """,
    "CREATE INDEX idx_video_snapshots_product ON video_snapshots (product_id, snapshot_date)",
    # 6. 达人快照表
    """
    CREATE TABLE creator_snapshots (
        creator_id      VARCHAR(50) NOT NULL,
        snapshot_date   DATE NOT NULL,
        nickname        VARCHAR(200),
        follower_count  INTEGER,
        avg_views_30d   INTEGER,
        category_tags   VARCHAR(500),
        product_count   INTEGER,
        created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
        PRIMARY KEY (creator_id, snapshot_date)
    )
    """,
    "CREATE INDEX idx_creator_snapshots_followers ON creator_snapshots (follower_count)",
    # 7. 话题快照表
    """
    CREATE TABLE hashtag_snapshots (
        tag_name        VARCHAR(200) NOT NULL,
        snapshot_date   DATE NOT NULL,
        views_total     INTEGER,
        created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
        PRIMARY KEY (tag_name, snapshot_date)
    )
    """,
    "CREATE INDEX idx_hashtag_snapshots_date ON hashtag_snapshots (snapshot_date)",
]

_DOWNGRADE_DDL = [
    "DROP TABLE IF EXISTS hashtag_snapshots",
    "DROP TABLE IF EXISTS creator_snapshots",
    "DROP TABLE IF EXISTS video_snapshots",
    "DROP TABLE IF EXISTS product_snapshots CASCADE",
]


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgresql():
        # SQLite 等方言跳过: 分区 DDL 为 PG 专属, 冒烟测试不依赖迁移
        print("[001] 非 PostgreSQL 方言, 跳过分区 DDL (仅 PG 支持分区)")
        return
    for ddl in _UPGRADE_DDL:
        op.execute(ddl)


def downgrade() -> None:
    if not _is_postgresql():
        print("[001] 非 PostgreSQL 方言, 跳过回滚")
        return
    for ddl in _DOWNGRADE_DDL:
        op.execute(ddl)
