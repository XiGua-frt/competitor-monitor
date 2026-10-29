"""持久化层：SQLite 连接/schema（app.storage.sqlite）与仓储（app.storage.repositories）。"""

from app.storage.repositories import Repository
from app.storage.sqlite import SCHEMA_VERSION, connect

__all__ = ["SCHEMA_VERSION", "Repository", "connect"]
