"""初始化数据库表结构。

用法:
    cd /Users/wuwenbo/Documents/competitor-monitor/amazonCrawler
    /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 scripts/init_db.py

连接目标（优先级）:
    1. 环境变量 DATABASE_URL（完整连接串）
    2. POSTGRES_* 拼接（.env / 环境）
    3. 备用 SQLite（config.DB_PATH，默认 data/amazon_crawler.sqlite3）

PostgreSQL 分区说明:
    asin_snapshots 生产可按 snapshot_date 做 RANGE 月分区（asin_snapshots_YYYYMM）。
    PG 要求分区键必须包含在主键/唯一约束中，本表主键 (asin, snapshot_date)
    已满足。本脚本用 create_all() 建普通表即可直接运行；若启用分区，建表改走
    迁移脚本 raw DDL:

        CREATE TABLE asin_snapshots (
            ... 同 models.py 列定义 ...
            PRIMARY KEY (asin, snapshot_date)
        ) PARTITION BY RANGE (snapshot_date);

        CREATE TABLE IF NOT EXISTS asin_snapshots_202511
        PARTITION OF asin_snapshots
        FOR VALUES FROM ('2025-11-01') TO ('2025-12-01');

    ORM 读写对分区表完全透明（INSERT/SELECT 自动路由到对应分区），
    snapshot_engine 无需改动。
"""

import sys
from pathlib import Path

# 保证从任意目录运行都能 import src.*
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine

from src.common.logger import get_logger
from src.database import connection as db_connection
from src.database.models import Base

logger = get_logger("scripts.init_db")


def main() -> int:
    """建表入口：create_all 建三张快照表（幂等，已存在则跳过）。"""
    url = db_connection.get_database_url()
    engine = db_connection.create_db_engine(url)
    logger.info("目标数据库: {}", url.split("@")[-1] if "@" in url else url)
    Base.metadata.create_all(engine)

    from sqlalchemy import inspect as sa_inspect
    tables = sa_inspect(engine).get_table_names()
    logger.info("当前表: {}", sorted(tables))

    expected = {"asin_snapshots", "keyword_rank_snapshots", "offer_snapshots"}
    missing = expected - set(tables)
    if missing:
        logger.error("建表后仍缺表: {}", sorted(missing))
        return 1
    logger.info("数据库初始化完成 ✓")
    return 0


if __name__ == "__main__":
    # 显式创建 engine 后释放；create_all 用临时 engine 亦可，这里统一走连接模块
    from src.database import connection as _c  # noqa: F401  触发惰性初始化
    raise SystemExit(main())
