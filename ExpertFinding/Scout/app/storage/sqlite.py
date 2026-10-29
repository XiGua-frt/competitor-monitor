"""SQLite 连接、schema 和迁移版本。

只用标准库 ``sqlite3``，不引入 ORM。三条硬约束体现在 DDL 里：

* 外键真实生效（连接层打开 ``PRAGMA foreign_keys``）；
* 每张业务表都有 ``created_at``，需要失效语义的表额外有
  ``deleted_at`` / ``revoked_at``；
* 列只有业务字段，**没有任何** Cookie、storage state、msToken 或 raw token 列，
  会话只以不可逆的 ``session_id_hash`` 出现。

``dump_table_names_and_columns`` 是给测试和巡检用的自检方法。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

SESSION_STATUS_ACTIVE = "active"

_TABLE_CREATOR = """
CREATE TABLE IF NOT EXISTS creator (
    id                TEXT PRIMARY KEY,
    platform          TEXT NOT NULL,
    platform_user_id  TEXT,
    username          TEXT NOT NULL,
    display_name      TEXT,
    bio               TEXT,
    profile_url       TEXT NOT NULL,
    avatar_url        TEXT,
    region_code       TEXT,
    language          TEXT,
    is_verified       INTEGER,
    follower_count    INTEGER,
    following_count   INTEGER,
    likes_count       INTEGER,
    video_count       INTEGER,
    first_seen_at     TEXT NOT NULL,
    last_seen_at      TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    deleted_at        TEXT,
    UNIQUE (platform, username)
)
"""

_TABLE_VIDEO = """
CREATE TABLE IF NOT EXISTS video (
    id                TEXT PRIMARY KEY,
    platform_video_id TEXT NOT NULL UNIQUE,
    creator_id        TEXT NOT NULL REFERENCES creator(id) ON DELETE CASCADE,
    description       TEXT,
    hashtags_json     TEXT NOT NULL DEFAULT '[]',
    create_time       TEXT,
    region_code       TEXT,
    view_count        INTEGER,
    like_count        INTEGER,
    comment_count     INTEGER,
    share_count       INTEGER,
    video_url         TEXT,
    collected_at      TEXT NOT NULL,
    source            TEXT NOT NULL,
    source_scope      TEXT NOT NULL,
    session_id_hash   TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    deleted_at        TEXT
)
"""

_TABLE_PROVENANCE = """
CREATE TABLE IF NOT EXISTS provenance (
    id               TEXT PRIMARY KEY,
    entity_type      TEXT NOT NULL,
    entity_id        TEXT NOT NULL,
    source           TEXT NOT NULL,
    source_scope     TEXT NOT NULL,
    keywords_json    TEXT NOT NULL DEFAULT '[]',
    collected_at     TEXT NOT NULL,
    session_id_hash  TEXT NOT NULL,
    raw_payload_hash TEXT NOT NULL,
    signer_version   TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    revoked_at       TEXT,
    deleted_at       TEXT
)
"""

_TABLE_SEARCH_RESULT = """
CREATE TABLE IF NOT EXISTS search_result (
    id            TEXT PRIMARY KEY,
    request_id    TEXT NOT NULL,
    creator_id    TEXT NOT NULL REFERENCES creator(id) ON DELETE CASCADE,
    keywords_json TEXT NOT NULL DEFAULT '[]',
    score         REAL,
    score_version TEXT NOT NULL,
    rank          INTEGER,
    created_at    TEXT NOT NULL,
    UNIQUE (request_id, creator_id)
)
"""

_TABLE_REQUEST_CACHE = """
CREATE TABLE IF NOT EXISTS request_cache (
    cache_key       TEXT PRIMARY KEY,
    response_json   TEXT NOT NULL,
    collected_at    TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    request_id      TEXT,
    session_id_hash TEXT,
    created_at      TEXT NOT NULL
)
"""

_TABLE_SESSION_META = """
CREATE TABLE IF NOT EXISTS session_meta (
    session_id      TEXT PRIMARY KEY,
    session_id_hash TEXT NOT NULL UNIQUE,
    source          TEXT,
    status          TEXT NOT NULL DEFAULT 'active',
    imported_at     TEXT,
    expires_at      TEXT,
    revoked_at      TEXT,
    created_at      TEXT NOT NULL
)
"""

_TABLE_API_AUDIT = """
CREATE TABLE IF NOT EXISTS api_audit (
    request_id TEXT PRIMARY KEY,
    operation  TEXT NOT NULL,
    outcome    TEXT NOT NULL,
    detail     TEXT,
    actor      TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_video_creator ON video(creator_id)",
    "CREATE INDEX IF NOT EXISTS idx_video_session ON video(session_id_hash)",
    "CREATE INDEX IF NOT EXISTS idx_provenance_entity ON provenance(entity_type, entity_id)",
    "CREATE INDEX IF NOT EXISTS idx_provenance_session ON provenance(session_id_hash)",
    "CREATE INDEX IF NOT EXISTS idx_search_result_request ON search_result(request_id)",
    "CREATE INDEX IF NOT EXISTS idx_request_cache_expiry ON request_cache(expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_api_audit_created ON api_audit(created_at)",
)

SCHEMA_STATEMENTS = (
    _TABLE_CREATOR,
    _TABLE_VIDEO,
    _TABLE_PROVENANCE,
    _TABLE_SEARCH_RESULT,
    _TABLE_REQUEST_CACHE,
    _TABLE_SESSION_META,
    _TABLE_API_AUDIT,
) + _INDEXES

EXPECTED_TABLES = frozenset({
    "creator",
    "video",
    "provenance",
    "search_result",
    "request_cache",
    "session_meta",
    "api_audit",
})


class SchemaError(RuntimeError):
    """数据库版本高于当前代码，拒绝继续，避免写坏数据。"""


def connect(path: str | Path) -> sqlite3.Connection:
    """打开（必要时创建）数据库并完成建表/迁移。

    返回的连接已开启 ``foreign_keys``、``row_factory`` 为 ``sqlite3.Row``。
    """
    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(str(target))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    """幂等建表 + 迁移版本推进。"""
    current = schema_version(connection)
    if current > SCHEMA_VERSION:
        raise SchemaError(
            f"database schema version {current} is newer than supported {SCHEMA_VERSION}"
        )

    with connection:
        for statement in SCHEMA_STATEMENTS:
            connection.execute(statement)
        if current < SCHEMA_VERSION:
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0


def table_names(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
        " AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [row["name"] for row in rows]


def column_names(connection: sqlite3.Connection, table: str) -> list[str]:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return [row["name"] for row in rows]


def dump_table_names_and_columns(connection: sqlite3.Connection, *, database: str = "scout") -> str:
    """自检：返回 "库名/表名/列名" 的拼接串，供敏感列名断言使用。"""
    chunks: list[str] = []
    for table in table_names(connection):
        chunks.append(f"{database}/{table}/" + ",".join(column_names(connection, table)))
    return " | ".join(chunks)


def dump_schema_summary(connection: sqlite3.Connection, *, database: str = "scout") -> dict[str, Any]:
    return {table: tuple(column_names(connection, table)) for table in table_names(connection)}


__all__ = [
    "EXPECTED_TABLES",
    "SCHEMA_STATEMENTS",
    "SCHEMA_VERSION",
    "SchemaError",
    "column_names",
    "connect",
    "dump_schema_summary",
    "dump_table_names_and_columns",
    "initialize",
    "schema_version",
    "table_names",
]
