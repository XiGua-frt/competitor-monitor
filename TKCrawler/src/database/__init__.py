"""TKCrawler 数据库模块。

对外导出:
    - engine / SessionLocal / get_session: 连接池 (connection.py)
    - 4 张快照表 ORM 模型 + Base (models.py)
    - SnapshotDiffEngine: 快照写入与差分引擎 (snapshot_engine.py)
"""

from src.database.connection import (
    create_db_engine,
    ensure_month_partition,
    get_database_url,
    get_session,
)
from src.database.models import (
    Base,
    CreatorSnapshot,
    HashtagSnapshot,
    ProductSnapshot,
    VideoSnapshot,
)
from src.database.snapshot_engine import SnapshotDiffEngine

# engine / SessionLocal 惰性转发 (PEP 562): 包被 import 时不创建 engine,
# 首次访问 src.database.engine / src.database.SessionLocal 时才建。
def __getattr__(name: str):
    if name in ("engine", "SessionLocal"):
        import src.database.connection as _conn

        return getattr(_conn, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "Base",
    "CreatorSnapshot",
    "HashtagSnapshot",
    "ProductSnapshot",
    "SnapshotDiffEngine",
    "SessionLocal",
    "VideoSnapshot",
    "create_db_engine",
    "engine",
    "ensure_month_partition",
    "get_database_url",
    "get_session",
]
