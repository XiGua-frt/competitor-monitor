"""数据库层：连接 / 模型 / 快照差分引擎（Agent A）。

公共导出：
    Base, AsinSnapshot, KeywordRankSnapshot, OfferSnapshot
    SnapshotDiffEngine
    get_database_url, create_db_engine, get_session, SessionLocal(惰性)
"""

from src.database.connection import (
    SessionLocal,
    create_db_engine,
    get_database_url,
    get_session,
)
from src.database.models import (
    AsinSnapshot,
    Base,
    KeywordRankSnapshot,
    OfferSnapshot,
)
from src.database.snapshot_engine import SnapshotDiffEngine

__all__ = [
    "Base",
    "AsinSnapshot",
    "KeywordRankSnapshot",
    "OfferSnapshot",
    "SnapshotDiffEngine",
    "get_database_url",
    "create_db_engine",
    "get_session",
    "SessionLocal",
]
