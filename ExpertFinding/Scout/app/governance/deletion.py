"""Creator 级联删除与 session 撤回清理。

所有操作都在调用方提供的事务里执行（``with connection:``），保证清理要么
全部生效、要么全部回滚。这里只负责删除/撤回动作本身，不负责业务编排。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

SESSION_STATUS_ACTIVE = "active"
SESSION_STATUS_REVOKED = "revoked"

# 撤回未登记会话时补的 tombstone 行：用合成主键占位，真正的身份是 session_id_hash。
SESSION_TOMBSTONE_PREFIX = "revoked:"


class SessionRevokedError(RuntimeError):
    """会话已撤回：禁止继续采集，也禁止为它写缓存或来源记录。"""


@dataclass(frozen=True)
class DeletionReport:
    """一次清理动作影响的行数，便于审计和断言。"""

    creators: int = 0
    videos: int = 0
    provenance_rows: int = 0
    search_results: int = 0
    cache_rows: int = 0
    sessions: int = 0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def delete_creator(connection: Any, creator_id: str) -> DeletionReport:
    """硬删除一个 Creator，并级联清理其视频、来源和搜索结果引用。

    删除顺序：来源 → 搜索结果引用 → 视频 → 作者本身。``video`` 与
    ``search_result`` 还有外键 ``ON DELETE CASCADE`` 兜底，避免任何孤儿行。
    """
    video_ids = [
        row[0]
        for row in connection.execute("SELECT id FROM video WHERE creator_id = ?", (creator_id,))
    ]
    placeholders = ",".join("?" * len(video_ids))

    with connection:
        provenance = _execute(
            connection,
            f"DELETE FROM provenance WHERE entity_id = ?"
            f"{' OR entity_id IN (%s)' % placeholders if placeholders else ''}",
            (creator_id, *video_ids),
        )
        search_results = _execute(
            connection, "DELETE FROM search_result WHERE creator_id = ?", (creator_id,)
        )
        videos = _execute(connection, "DELETE FROM video WHERE creator_id = ?", (creator_id,))
        creators = _execute(connection, "DELETE FROM creator WHERE id = ?", (creator_id,))

    return DeletionReport(
        creators=creators,
        videos=videos,
        provenance_rows=provenance,
        search_results=search_results,
    )


def purge_session(connection: Any, session_id_hash: str, *, revoked_at: str | None = None) -> DeletionReport:
    """按会话指纹清掉该会话产生的缓存与来源记录，并把会话标记为已撤回。

    视频本身也带 ``session_id_hash``（它们就是这次会话抓到的），一并删除；
    ``creator`` 是自然人记录、可能来自多次会话，保留不动。

    撤回标记对**未登记**的指纹同样生效：``session_meta`` 里没有对应行时补一条
    tombstone（status=revoked）。否则撤回一个从没登记过的会话后闸门仍是开着的，
    同一指纹可以继续写入缓存和来源记录。
    """
    revoked_at = revoked_at or utc_now()

    with connection:
        cache_rows = _execute(
            connection, "DELETE FROM request_cache WHERE session_id_hash = ?", (session_id_hash,)
        )
        provenance_rows = _execute(
            connection, "DELETE FROM provenance WHERE session_id_hash = ?", (session_id_hash,)
        )
        videos = _execute(
            connection, "DELETE FROM video WHERE session_id_hash = ?", (session_id_hash,)
        )
        sessions = _execute(
            connection,
            "UPDATE session_meta SET status = ?, revoked_at = ?"
            " WHERE session_id_hash = ? AND revoked_at IS NULL",
            (SESSION_STATUS_REVOKED, revoked_at, session_id_hash),
        )
        sessions += _revoke_unregistered_session(connection, session_id_hash, revoked_at)

    return DeletionReport(
        videos=videos,
        provenance_rows=provenance_rows,
        cache_rows=cache_rows,
        sessions=sessions,
    )


def _revoke_unregistered_session(connection: Any, session_id_hash: str, revoked_at: str) -> int:
    """给未登记的指纹补一条 revoked tombstone；已登记的返回 0（上面 UPDATE 已处理）。"""
    known = connection.execute(
        "SELECT 1 FROM session_meta WHERE session_id_hash = ?", (session_id_hash,)
    ).fetchone()
    if known:
        return 0
    cursor = connection.execute(
        "INSERT OR REPLACE INTO session_meta (session_id, session_id_hash, source, status,"
        " imported_at, expires_at, revoked_at, created_at) VALUES (?, ?, NULL, ?, NULL, NULL, ?, ?)",
        (f"{SESSION_TOMBSTONE_PREFIX}{session_id_hash}", session_id_hash,
         SESSION_STATUS_REVOKED, revoked_at, revoked_at),
    )
    return max(cursor.rowcount or 0, 0)


def session_is_revoked(store: Any, session_id_hash: str) -> bool:
    """会话是否已被撤回。未知会话视为可用（登记与否由 vault 与 purge 的 tombstone 决定）。"""
    row = _connection(store).execute(
        "SELECT status, revoked_at FROM session_meta WHERE session_id_hash = ?",
        (session_id_hash,),
    ).fetchone()
    if row is None:
        return False
    return bool(row["revoked_at"]) or row["status"] == SESSION_STATUS_REVOKED


def assert_session_usable(store: Any, session_id_hash: str) -> None:
    """发新请求前的闸门：已撤回的会话直接拒绝。"""
    if session_is_revoked(store, session_id_hash):
        raise SessionRevokedError(f"session already revoked, refusing new requests: {session_id_hash}")


def _execute(connection: Any, sql: str, params: tuple[Any, ...]) -> int:
    cursor = connection.execute(sql, params)
    return max(cursor.rowcount or 0, 0)


def _connection(store: Any) -> Any:
    """接受裸连接或任何暴露 ``.connection`` 的仓储对象。"""
    return getattr(store, "connection", store)


__all__ = [
    "SESSION_STATUS_ACTIVE",
    "SESSION_STATUS_REVOKED",
    "DeletionReport",
    "SessionRevokedError",
    "assert_session_usable",
    "delete_creator",
    "purge_session",
    "session_is_revoked",
    "utc_now",
]
