"""数据库连接池模块（Agent A 正式实现）。

基于 SQLAlchemy 2.0 的 engine + sessionmaker，惰性创建：
连接优先级：
    1. 环境变量 DATABASE_URL（完整连接串，便于测试/覆盖）
    2. src/common/config.py 中 POSTGRES_* 拼接（.env 加载）
    3. 备用 SQLite（config.DB_PATH，本地无 PG 时跑通链路）
"""

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from src.common.config import config
from src.common.logger import get_logger

logger = get_logger("database.connection")

_engine_instance: Optional[Engine] = None
_session_factory: Optional[sessionmaker] = None


def get_database_url() -> str:
    """返回数据库连接 URL。

    优先级：环境变量 DATABASE_URL > POSTGRES_* 拼接 > 备用 SQLite。
    SQLite 路径不存在时自动建父目录。
    """
    url = os.getenv("DATABASE_URL") or config.DATABASE_URL
    if url:
        return url
    # 备用 SQLite
    db_path = Path(config.DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{db_path}"


def create_db_engine(url: Optional[str] = None, **kwargs) -> Engine:
    """创建 SQLAlchemy engine；连接池参数可被 kwargs 覆盖（测试传 SQLite 配置用）。"""
    url = url or get_database_url()
    pool_kwargs = {
        # 池参数仅对真实连接池方言有意义，SQLite 不适用
        "pool_pre_ping": True,   # 取连接前探活，避免拿到失效连接
        "pool_recycle": 1800,    # 30 分钟回收，规避 PG 空闲断连
        "pool_size": 5,
        "max_overflow": 10,
    }
    if url.startswith("sqlite"):
        # SQLite（尤其内存库）不使用连接池参数
        pool_kwargs = {"connect_args": {"check_same_thread": False}}
    pool_kwargs.update(kwargs)
    # 日志不打印密码段
    logger.info("创建数据库 engine: {}", url.split("@")[-1] if "@" in url else url)
    return create_engine(url, **pool_kwargs)


def _get_engine() -> Engine:
    """惰性单例 engine：首次访问才创建（PEP 562，见 __getattr__）。"""
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = create_db_engine()
    return _engine_instance


def _get_session_factory() -> sessionmaker:
    """惰性单例 sessionmaker。"""
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
    """模块级惰性属性：connection.engine / connection.SessionLocal 首次访问时创建。"""
    if name == "engine":
        return _get_engine()
    if name == "SessionLocal":
        return _get_session_factory()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


@contextmanager
def get_session() -> Iterator[Session]:
    """会话上下文管理器：用完自动 commit / rollback / close。"""
    session = _get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
