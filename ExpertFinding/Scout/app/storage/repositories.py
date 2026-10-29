"""Creator/Video/Provenance/SearchResult/Cache/Audit 仓储。

所有写操作都在单个事务里完成：``save_search`` 要么整批落库、要么一条不留；
删除与撤回同样如此（语句由 :mod:`app.governance.deletion` 提供）。

会话材料永远不会到达这里：上游只交给我们 ``session_id_hash``，写入前还会用
:func:`app.governance.provenance.assert_clean_record` 再拦一道。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from app.domain.models import Creator, ParsedSearch, Provenance, Video
from app.governance import deletion
from app.governance.provenance import (
    DEFAULT_SCORE_VERSION,
    DEFAULT_SIGNER_VERSION,
    assert_clean_record,
)
from app.storage import sqlite as storage

DEFAULT_CACHE_TTL_SECONDS = 1800


@dataclass(frozen=True)
class CacheEntry:
    request_id: str | None
    session_id_hash: str | None
    cache_key: str
    response_json: str
    collected_at: str
    expires_at: str


@dataclass(frozen=True)
class SearchResultRow:
    request_id: str
    creator_id: str
    keywords: tuple[str, ...]
    score: float | None
    score_version: str
    rank: int | None


@dataclass(frozen=True)
class LoadedSearch:
    """``get_search`` 的载入对象：复用领域模型，按 request_id 组织。"""

    request_id: str
    creators: tuple[Creator, ...]
    videos: tuple[Video, ...]
    provenance: tuple[Provenance, ...]
    search_results: tuple[SearchResultRow, ...]


@dataclass(frozen=True)
class AuditEntry:
    request_id: str
    operation: str
    outcome: str
    detail: str | None
    actor: str | None
    created_at: str
    updated_at: str


class Repository:
    """SQLite 仓储。构造用 :meth:`connect`，不要直接实例化裸连接。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    # ---------------------------------------------------------------- lifecycle

    @classmethod
    def connect(cls, path: str | Path) -> "Repository":
        return cls(storage.connect(path))

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    def close(self) -> None:
        self._connection.close()

    def dump_table_names_and_columns(self) -> str:
        return storage.dump_table_names_and_columns(self._connection)

    # ------------------------------------------------------------------ writes

    def save_search(
        self,
        parsed: ParsedSearch,
        request_id: str,
        cache_key: str | None = None,
        *,
        keywords: Sequence[str] | None = None,
        signer_version: str = DEFAULT_SIGNER_VERSION,
        score_version: str = DEFAULT_SCORE_VERSION,
        collected_at: str | None = None,
        cache_ttl_seconds: int | None = None,
    ) -> None:
        """把一次解析结果整批落库。任何一条不合法 → 整批回滚。

        整批携带的会话指纹（video / provenance 上的 ``session_id_hash``）在开事务
        之前先过撤回闸门：已撤回的会话一行都不许落，包括它派生的缓存。
        """
        if not request_id:
            raise ValueError("request_id is required")

        creators = tuple(parsed.creators or ())
        videos = tuple(parsed.videos or ())
        provenances = tuple(parsed.provenances or ())

        creator_ids = {creator.id for creator in creators}
        missing = sorted({video.creator_id for video in videos} - creator_ids)
        if missing:
            raise ValueError(f"videos reference unknown creators: {', '.join(missing)}")

        entity_ids = creator_ids | {video.id for video in videos}
        orphan_refs = sorted(
            {record.entity_id for record in provenances} - entity_ids,
        )
        if orphan_refs:
            raise ValueError(f"provenance references unknown entities: {', '.join(orphan_refs)}")

        timestamp = collected_at or _latest_timestamp(parsed)
        search_keywords = tuple(keywords) if keywords else _derived_keywords(provenances)
        now = _utc_now()

        self._assert_sessions_usable(provenances, videos)

        with self._connection:
            for creator in creators:
                self._upsert_creator(creator, now=now)
            for video in videos:
                self._upsert_video(video, now=now)
            for record in provenances:
                self._insert_provenance(record, signer_version=signer_version, now=now)
            for rank, creator in enumerate(creators, start=1):
                self._upsert_search_result(
                    request_id=request_id,
                    creator_id=creator.id,
                    keywords=search_keywords,
                    score_version=score_version,
                    rank=rank,
                    now=now,
                )

        if cache_key:
            # 派生摘要也过一遍敏感闸门（put_cached 里还有一道），避免只有单点防线。
            payload = _search_fingerprint(request_id, creators, videos)
            assert_clean_record(payload, context="cache payload")
            self.put_cached(
                cache_key,
                payload,
                collected_at=timestamp,
                expires_at=_expiry_from(timestamp, cache_ttl_seconds),
                request_id=request_id,
            )

    def put_cached(
        self,
        cache_key: str,
        response_json: str,
        collected_at: str,
        expires_at: str,
        request_id: str | None = None,
        session_id_hash: str | None = None,
    ) -> None:
        """写缓存。已撤回的会话不许再产出缓存行，payload 也要过敏感闸门。"""
        if session_id_hash:
            deletion.assert_session_usable(self._connection, session_id_hash)
        assert_clean_record(response_json, context="cache payload")
        with self._connection:
            self._connection.execute(
                "INSERT INTO request_cache (cache_key, response_json, collected_at,"
                " expires_at, request_id, session_id_hash, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(cache_key) DO UPDATE SET response_json = excluded.response_json,"
                " collected_at = excluded.collected_at, expires_at = excluded.expires_at,"
                " request_id = excluded.request_id, session_id_hash = excluded.session_id_hash,"
                " created_at = excluded.created_at",
                (cache_key, response_json, collected_at, expires_at,
                 request_id, session_id_hash, _utc_now()),
            )

    def record_audit(
        self,
        request_id: str,
        operation: str,
        outcome: str,
        detail: str | None = None,
        actor: str | None = None,
    ) -> None:
        if not request_id or not operation or not outcome:
            raise ValueError("request_id, operation and outcome are required")
        now = _utc_now()
        with self._connection:
            self._connection.execute(
                "INSERT INTO api_audit (request_id, operation, outcome, detail, actor,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(request_id) DO UPDATE SET operation = excluded.operation,"
                " outcome = excluded.outcome, detail = excluded.detail,"
                " actor = excluded.actor, updated_at = excluded.updated_at",
                (request_id, operation, outcome, detail, actor, now, now),
            )

    def upsert_session_meta(
        self,
        session_id: str,
        session_id_hash: str,
        *,
        source: str | None = None,
        status: str = deletion.SESSION_STATUS_ACTIVE,
        imported_at: str | None = None,
        expires_at: str | None = None,
    ) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT INTO session_meta (session_id, session_id_hash, source, status,"
                " imported_at, expires_at, revoked_at, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, NULL, ?)"
                " ON CONFLICT(session_id) DO UPDATE SET session_id_hash = excluded.session_id_hash,"
                " source = excluded.source, status = excluded.status,"
                " imported_at = excluded.imported_at, expires_at = excluded.expires_at",
                (session_id, session_id_hash, source, status, imported_at, expires_at, _utc_now()),
            )

    # ------------------------------------------------------------------ reads

    def get_search(self, request_id: str) -> LoadedSearch | None:
        rows = self._connection.execute(
            "SELECT * FROM search_result WHERE request_id = ? ORDER BY rank, creator_id",
            (request_id,),
        ).fetchall()
        if not rows:
            return None

        creator_ids = [row["creator_id"] for row in rows]
        results = tuple(
            SearchResultRow(
                request_id=row["request_id"],
                creator_id=row["creator_id"],
                keywords=_decode_tuple(row["keywords_json"]),
                score=row["score"],
                score_version=row["score_version"],
                rank=row["rank"],
            )
            for row in rows
        )

        creators = tuple(
            _row_to_creator(row)
            for row in self._connection.execute(
                f"SELECT * FROM creator WHERE id IN ({_placeholders(creator_ids)})"
                " AND deleted_at IS NULL ORDER BY id",
                creator_ids,
            ).fetchall()
        )
        videos = tuple(
            _row_to_video(row)
            for row in self._connection.execute(
                f"SELECT * FROM video WHERE creator_id IN ({_placeholders(creator_ids)})"
                " AND deleted_at IS NULL ORDER BY platform_video_id",
                creator_ids,
            ).fetchall()
        )
        provenance = tuple(
            _row_to_provenance(row) for row in self._provenance_rows(creators, videos)
        )

        return LoadedSearch(
            request_id=request_id,
            creators=creators,
            videos=videos,
            provenance=provenance,
            search_results=results,
        )

    def get_cached(self, cache_key: str, now: str | None = None) -> CacheEntry | None:
        row = self._connection.execute(
            "SELECT * FROM request_cache WHERE cache_key = ?", (cache_key,)
        ).fetchone()
        if row is None:
            return None

        reference = now or _utc_now()
        if _is_expired(row["expires_at"], reference):
            with self._connection:
                self._connection.execute(
                    "DELETE FROM request_cache WHERE cache_key = ?", (cache_key,)
                )
            return None

        return CacheEntry(
            request_id=row["request_id"],
            session_id_hash=row["session_id_hash"],
            cache_key=row["cache_key"],
            response_json=row["response_json"],
            collected_at=row["collected_at"],
            expires_at=row["expires_at"],
        )

    def list_audit(self) -> list[sqlite3.Row]:
        return self._connection.execute(
            "SELECT * FROM api_audit ORDER BY created_at, request_id"
        ).fetchall()

    # -------------------------------------------------------------- governance

    def delete_creator(self, creator_id: str) -> deletion.DeletionReport:
        return deletion.delete_creator(self._connection, creator_id)

    def purge_session(
        self, session_id_hash: str, *, revoked_at: str | None = None
    ) -> deletion.DeletionReport:
        return deletion.purge_session(self._connection, session_id_hash, revoked_at=revoked_at)

    # ---------------------------------------------------------------- internals

    def _assert_sessions_usable(
        self, provenances: Sequence[Provenance], videos: Sequence[Video]
    ) -> None:
        """整批写入前的撤回闸门：命中任一已撤回指纹就不开事务。"""
        hashes = {record.session_id_hash for record in provenances}
        hashes |= {video.session_id_hash for video in videos}
        for session_id_hash in sorted(item for item in hashes if item):
            deletion.assert_session_usable(self._connection, session_id_hash)

    def _upsert_creator(self, creator: Creator, *, now: str) -> None:
        columns = (
            "id", "platform", "platform_user_id", "username", "display_name", "bio",
            "profile_url", "avatar_url", "region_code", "language", "is_verified",
            "follower_count", "following_count", "likes_count", "video_count",
            "first_seen_at", "last_seen_at", "created_at", "updated_at", "deleted_at",
        )
        values = (
            creator.id, creator.platform, creator.platform_user_id, creator.username,
            creator.display_name, creator.bio, creator.profile_url, creator.avatar_url,
            creator.region_code, creator.language, _encode_bool(creator.is_verified),
            creator.follower_count, creator.following_count, creator.likes_count,
            creator.video_count, creator.first_seen_at, creator.last_seen_at,
            now, now, creator.deleted_at,
        )
        assignments = (
            "platform_user_id = excluded.platform_user_id,"
            " display_name = excluded.display_name,"
            " bio = excluded.bio,"
            " avatar_url = excluded.avatar_url,"
            " region_code = excluded.region_code,"
            " language = excluded.language,"
            " is_verified = excluded.is_verified,"
            " follower_count = excluded.follower_count,"
            " following_count = excluded.following_count,"
            " likes_count = excluded.likes_count,"
            " video_count = excluded.video_count,"
            " first_seen_at = MIN(creator.first_seen_at, excluded.first_seen_at),"
            " last_seen_at = MAX(creator.last_seen_at, excluded.last_seen_at),"
            " updated_at = excluded.updated_at"
        )
        self._connection.execute(
            f"INSERT INTO creator ({','.join(columns)}) VALUES ({_placeholders(columns)})"
            f" ON CONFLICT(id) DO UPDATE SET {assignments}",
            values,
        )

    def _upsert_video(self, video: Video, *, now: str) -> None:
        columns = (
            "id", "platform_video_id", "creator_id", "description", "hashtags_json",
            "create_time", "region_code", "view_count", "like_count", "comment_count",
            "share_count", "video_url", "collected_at", "source", "source_scope",
            "session_id_hash", "created_at", "deleted_at",
        )
        values = (
            video.id, video.platform_video_id, video.creator_id, video.description,
            _encode_tuple(video.hashtags), video.create_time, video.region_code,
            video.view_count, video.like_count, video.comment_count, video.share_count,
            video.video_url, video.collected_at, video.source, video.source_scope,
            video.session_id_hash, now, None,
        )
        assignments = (
            "description = excluded.description,"
            " hashtags_json = excluded.hashtags_json,"
            " create_time = excluded.create_time,"
            " region_code = excluded.region_code,"
            " view_count = excluded.view_count,"
            " like_count = excluded.like_count,"
            " comment_count = excluded.comment_count,"
            " share_count = excluded.share_count,"
            " video_url = excluded.video_url,"
            " collected_at = excluded.collected_at,"
            " source = excluded.source,"
            " source_scope = excluded.source_scope,"
            " session_id_hash = excluded.session_id_hash"
        )
        self._connection.execute(
            f"INSERT INTO video ({','.join(columns)}) VALUES ({_placeholders(columns)})"
            f" ON CONFLICT(id) DO UPDATE SET {assignments}",
            values,
        )

    def _insert_provenance(
        self, record: Provenance, *, signer_version: str, now: str
    ) -> None:
        self._connection.execute(
            "INSERT INTO provenance (id, entity_type, entity_id, source, source_scope,"
            " keywords_json, collected_at, session_id_hash, raw_payload_hash,"
            " signer_version, created_at, revoked_at, deleted_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)"
            " ON CONFLICT(id) DO NOTHING",
            (
                record.id, record.entity_type, record.entity_id, record.source,
                record.source_scope, _encode_tuple(record.keywords), record.collected_at,
                record.session_id_hash, record.raw_payload_hash, signer_version, now,
            ),
        )

    def _upsert_search_result(
        self,
        *,
        request_id: str,
        creator_id: str,
        keywords: Sequence[str],
        score_version: str,
        rank: int,
        now: str,
    ) -> None:
        self._connection.execute(
            "INSERT INTO search_result (id, request_id, creator_id, keywords_json, score,"
            " score_version, rank, created_at) VALUES (?, ?, ?, ?, NULL, ?, ?, ?)"
            " ON CONFLICT(request_id, creator_id) DO UPDATE SET"
            " keywords_json = excluded.keywords_json,"
            " score_version = excluded.score_version,"
            " rank = excluded.rank",
            (
                f"{request_id}:{creator_id}", request_id, creator_id,
                _encode_tuple(keywords), score_version, rank, now,
            ),
        )

    def _provenance_rows(
        self, creators: Sequence[Creator], videos: Sequence[Video]
    ) -> list[sqlite3.Row]:
        pairs = [("creator", creator.id) for creator in creators]
        pairs += [("video", video.id) for video in videos]
        if not pairs:
            return []

        clause = " OR ".join("(entity_type = ? AND entity_id = ?)" for _ in pairs)
        params: list[Any] = []
        for entity_type, entity_id in pairs:
            params.extend((entity_type, entity_id))
        return self._connection.execute(
            f"SELECT * FROM provenance WHERE {clause} AND deleted_at IS NULL ORDER BY id",
            params,
        ).fetchall()


# ---------------------------------------------------------------------- codecs


def _row_to_creator(row: sqlite3.Row) -> Creator:
    return Creator(
        id=row["id"],
        platform=row["platform"],
        platform_user_id=row["platform_user_id"],
        username=row["username"],
        display_name=row["display_name"],
        bio=row["bio"],
        profile_url=row["profile_url"],
        avatar_url=row["avatar_url"],
        region_code=row["region_code"],
        language=row["language"],
        is_verified=_decode_bool(row["is_verified"]),
        follower_count=row["follower_count"],
        following_count=row["following_count"],
        likes_count=row["likes_count"],
        video_count=row["video_count"],
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        deleted_at=row["deleted_at"],
    )


def _row_to_video(row: sqlite3.Row) -> Video:
    return Video(
        id=row["id"],
        platform_video_id=row["platform_video_id"],
        creator_id=row["creator_id"],
        description=row["description"],
        hashtags=_decode_tuple(row["hashtags_json"]),
        create_time=row["create_time"],
        region_code=row["region_code"],
        view_count=row["view_count"],
        like_count=row["like_count"],
        comment_count=row["comment_count"],
        share_count=row["share_count"],
        video_url=row["video_url"],
        collected_at=row["collected_at"],
        source=row["source"],
        source_scope=row["source_scope"],
        session_id_hash=row["session_id_hash"],
    )


def _row_to_provenance(row: sqlite3.Row) -> Provenance:
    return Provenance(
        id=row["id"],
        entity_type=row["entity_type"],
        entity_id=row["entity_id"],
        source=row["source"],
        source_scope=row["source_scope"],
        keywords=_decode_tuple(row["keywords_json"]),
        collected_at=row["collected_at"],
        session_id_hash=row["session_id_hash"],
        raw_payload_hash=row["raw_payload_hash"],
    )


def _encode_bool(value: bool | None) -> int | None:
    return None if value is None else int(value)


def _decode_bool(value: int | None) -> bool | None:
    return None if value is None else bool(value)


def _encode_tuple(values: Iterable[str]) -> str:
    return json.dumps(list(values or ()), ensure_ascii=False, separators=(",", ":"))


def _decode_tuple(raw: str) -> tuple[str, ...]:
    if not raw:
        return ()
    decoded = json.loads(raw)
    return tuple(decoded) if isinstance(decoded, list) else ()


def _placeholders(items: Sequence[Any]) -> str:
    return ",".join("?" * len(items))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _latest_timestamp(parsed: ParsedSearch) -> str:
    candidates = [video.collected_at for video in parsed.videos or ()]
    candidates += [creator.last_seen_at for creator in parsed.creators or ()]
    valid = sorted(item for item in candidates if item)
    if valid:
        return valid[-1]
    return _utc_now()


def _derived_keywords(provenances: Sequence[Provenance]) -> tuple[str, ...]:
    for record in provenances:
        if record.keywords:
            return tuple(record.keywords)
    return ()


def _search_fingerprint(
    request_id: str, creators: Sequence[Creator], videos: Sequence[Video]
) -> str:
    """缓存里只放派生摘要，不放原始响应、Cookie 或签名输入。"""
    return json.dumps(
        {
            "request_id": request_id,
            "creator_ids": [creator.id for creator in creators],
            "video_ids": [video.id for video in videos],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _expiry_from(collected_at: str, ttl_seconds: int | None) -> str:
    ttl = DEFAULT_CACHE_TTL_SECONDS if ttl_seconds is None else ttl_seconds
    start = _parse_iso(collected_at) or datetime.now(timezone.utc)
    return (start + timedelta(seconds=ttl)).isoformat()


def _is_expired(expires_at: str, now: str) -> bool:
    expires = _parse_iso(expires_at)
    reference = _parse_iso(now)
    if expires is None or reference is None:
        return bool(expires_at) and str(now) >= str(expires_at)
    return reference >= expires


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


__all__ = [
    "AuditEntry",
    "CacheEntry",
    "DEFAULT_CACHE_TTL_SECONDS",
    "LoadedSearch",
    "Repository",
    "SearchResultRow",
]
