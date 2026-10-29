#!/usr/bin/env python
"""数据库初始化脚本。

用法:
    /path/to/python scripts/init_db.py            # 跑 Alembic 迁移建表
    /path/to/python scripts/init_db.py --check    # 只检查连接与建表结果

流程:
    1. 读 .env / 环境变量 (POSTGRES_*, 或 DATABASE_URL 整串覆盖)
    2. 通过 Alembic 程序化执行 migrations/versions/001_initial_schema.py
       (product_snapshots 的 RANGE 分区 + default 分区由迁移内 raw DDL 创建)
    3. 用 SQLAlchemy inspector 校验并打印建表结果

依赖: src/common/config.py 自动加载项目根目录 .env。
"""

import sys
from argparse import ArgumentParser
from pathlib import Path

# 保证可从任意 cwd 运行
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alembic import command  # noqa: E402
from alembic.config import Config as AlembicConfig  # noqa: E402
from sqlalchemy import inspect  # noqa: E402

from src.common.config import config  # noqa: E402  (加载 .env 并确保目录存在)
from src.common.logger import get_logger  # noqa: E402
from src.database.connection import create_db_engine, get_database_url  # noqa: E402

logger = get_logger("scripts.init_db")

MIGRATIONS_DIR = PROJECT_ROOT / "src" / "database" / "migrations"
EXPECTED_TABLES = [
    "product_snapshots",
    "video_snapshots",
    "creator_snapshots",
    "hashtag_snapshots",
]


def run_migrations() -> None:
    """程序化执行 Alembic 迁移到 head。"""
    ini_path = MIGRATIONS_DIR / "alembic.ini"
    alembic_cfg = AlembicConfig(str(ini_path))
    # 注入连接 URL (优先 DATABASE_URL 环境变量, 其次 .env 的 POSTGRES_*)
    alembic_cfg.set_main_option("sqlalchemy.url", get_database_url())
    logger.info("执行 Alembic 迁移: {} -> head", ini_path)
    command.upgrade(alembic_cfg, "head")


def verify_tables(engine) -> bool:
    """校验 4 张快照表是否全部存在, 打印明细。"""
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    all_ok = True
    print("\n========== 建表结果 ==========")
    for table in EXPECTED_TABLES:
        if table in existing:
            cols = [c["name"] for c in inspector.get_columns(table)]
            print(f"[OK]   {table} ({len(cols)} 列): {', '.join(cols)}")
        else:
            print(f"[MISS] {table} 不存在!")
            all_ok = False
    print("==============================")
    return all_ok


def main() -> int:
    parser = ArgumentParser(description="TKCrawler 数据库初始化")
    parser.add_argument("--check", action="store_true", help="只检查连接与表结构")
    args = parser.parse_args()

    url = get_database_url()
    masked = url.split("@")[-1]  # 不打印密码
    print(f"目标数据库: {masked}")

    engine = create_db_engine()
    try:
        if not args.check:
            run_migrations()
        else:
            print("--check 模式: 跳过迁移, 仅校验")
        ok = verify_tables(engine)
        if ok:
            print("\n数据库初始化完成 ✓")
            return 0
        print("\n部分表缺失, 请检查迁移日志 ✗")
        return 1
    except Exception as exc:
        logger.exception("数据库初始化失败")
        print(f"\n初始化失败: {exc}")
        print("提示: 确认 PostgreSQL 已启动 (docker-compose up -d postgres),")
        print("并核对 .env 中 POSTGRES_HOST/PORT/DB/USER/PASSWORD 配置。")
        return 2
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
