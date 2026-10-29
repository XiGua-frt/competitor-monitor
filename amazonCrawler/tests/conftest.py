"""pytest 共享 fixture：SQLite 内存库 + 注入式 SnapshotDiffEngine。

约定（契约）: DB 测试全部使用 sqlite 内存库，不发起真实网络请求。
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database.models import Base
from src.database.snapshot_engine import SnapshotDiffEngine


@pytest.fixture()
def sqlite_engine():
    """每测试独立的 SQLite 内存引擎 + create_all 建三张快照表。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def session_factory(sqlite_engine):
    """绑定内存库的 sessionmaker，供 SnapshotDiffEngine 注入。"""
    return sessionmaker(
        bind=sqlite_engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )


@pytest.fixture()
def engine(session_factory):
    """注入 SQLite 会话工厂的 SnapshotDiffEngine（契约的测试注入点）。"""
    return SnapshotDiffEngine(session_factory=session_factory)
