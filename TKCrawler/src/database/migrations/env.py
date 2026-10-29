"""Alembic 迁移环境。

URL 解析优先级:
    1. alembic.ini 的 sqlalchemy.url (scripts/init_db.py 程序化注入)
    2. 环境变量 DATABASE_URL
    3. src/common/config.py 的 POSTGRES_* 变量
"""

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# 保证项目根目录可导入 (alembic 可能从任意 cwd 启动)
PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from src.common.config import config as app_config  # noqa: E402
from src.database.models import Base  # noqa: E402

# Alembic Config 对象
alembic_cfg = context.config

# 日志配置 (仅当 ini 中有日志段时生效)
if alembic_cfg.config_file_name is not None:
    fileConfig(alembic_cfg.config_file_name)

# ORM 元数据, 供 autogenerate 对比
target_metadata = Base.metadata


def _get_url() -> str:
    url = alembic_cfg.get_main_option("sqlalchemy.url")
    if url:
        return url
    return os.getenv("DATABASE_URL") or app_config.DATABASE_URL


def run_migrations_offline() -> None:
    """离线模式: 仅生成 SQL 脚本, 不连库。"""
    context.configure(
        url=_get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式: 直连数据库执行迁移。"""
    cfg_section = alembic_cfg.get_section(alembic_cfg.config_ini_section)
    cfg_section["sqlalchemy.url"] = _get_url()
    connectable = engine_from_config(
        cfg_section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
