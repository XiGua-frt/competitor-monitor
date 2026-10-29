"""数据库连接池模块。

基于 SQLAlchemy 2.0 的 engine + sessionmaker 连接池。
连接配置优先级:
  1. 环境变量 DATABASE_URL (完整连接串, 便于测试/覆盖)
  2. src/common/config.py 中的 POSTGRES_* 变量 (.env 加载)
"""

import os
from contextlib import contextmanager
from typing import Iterator, Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from src.common.config import config
from src.common.logger import get_logger

logger = get_logger("database.connection")


def get_database_url() -> str:
    """返回数据库连接 URL, 支持环境变量 DATABASE_URL 覆盖 .env 中的 POSTGRES_* 配置。"""
    return os.getenv("DATABASE_URL") or config.DATABASE_URL


def create_db_engine(url: Optional[str] = None, **kwargs) -> Engine:
    """创建 SQLAlchemy engine。

    连接池参数可被 kwargs 覆盖, 便于测试时传入 SQLite 内存库配置。
    """
    url = url or get_database_url()
    pool_kwargs = {
        # 池参数仅对真实连接池方言有意义, SQLite/内存库不需要
        "pool_size": 5,
        "max_overflow": 10,
        "pool_pre_ping": True,   # 取连接前探活, 避免拿到失效连接
        "pool_recycle": 1800,    # 30 分钟回收, 规避 PG 空闲断连
    }
    pool_kwargs.update(kwargs)
    logger.info("创建数据库 engine: {} (pool_pre_ping=True)", url.split("@")[-1])
    return create_engine(url, **pool_kwargs)


# ---------------------------------------------------------------------------
# 惰性 engine / SessionLocal (PEP 562 模块级 __getattr__)
# import 本模块不再立即创建 engine 对象(避免 CLI --help 等纯导入场景的
# 副作用与日志噪音); 首次访问 connection.engine / connection.SessionLocal
# 时才创建, 并缓存复用。
# ---------------------------------------------------------------------------
_engine_instance: Optional[Engine] = None
_session_factory: Optional[sessionmaker] = None


def _get_engine() -> Engine:
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = create_db_engine()
    return _engine_instance


def _get_session_factory() -> sessionmaker:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=_get_engine(),
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )
    return _session_factory


def __getattr__(name: str):
    if name == "engine":
        return _get_engine()
    if name == "SessionLocal":
        return _get_session_factory()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


@contextmanager
def get_session() -> Iterator[Session]:
    """会话上下文管理器: 用完自动 commit/rollback/close。"""
    session = _get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def ensure_month_partition(target_date, engine_: Optional[Engine] = None) -> bool:
    """确保 product_snapshots 存在覆盖 target_date 所在月份的 RANGE 分区。

    分区命名: product_snapshots_YYYYMM, 边界为 [月初, 次月初)。
    已存在则跳过 (IF NOT EXISTS), 返回是否实际创建了新分区。

    注意: 这是 PostgreSQL 专属逻辑, 对分区表的 raw DDL 见
    migrations/versions/001_initial_schema.py。
    """
    from sqlalchemy import text

    # 省参时走模块内惰性 engine (本模块为 PEP 562 惰性设计, 裸名 `engine`
    # 不在函数作用域内, 必须经 _get_engine() 入口取默认 engine)
    engine_ = engine_ or _get_engine()
    if engine_.dialect.name != "postgresql":
        logger.debug("非 PostgreSQL 方言 ({}), 跳过分区创建", engine_.dialect.name)
        return False

    start = target_date.replace(day=1)
    if start.month == 12:
        next_month = start.replace(year=start.year + 1, month=1)
    else:
        next_month = start.replace(month=start.month + 1)
    partition_name = f"product_snapshots_{start:%Y%m}"

    ddl = f"""
    CREATE TABLE IF NOT EXISTS {partition_name}
    PARTITION OF product_snapshots
    FOR VALUES FROM ('{start:%Y-%m-%d}') TO ('{next_month:%Y-%m-%d}')
    """
    with engine_.begin() as conn:
        conn.execute(text(ddl))
    logger.info("分区 {} 就绪", partition_name)
    return True
