"""Translate one raw TikTok search response into domain objects.

The parser is the only place that knows upstream field names. Everything
downstream (deduplication, aggregation, scoring, storage) sees the frozen
dataclasses from :mod:`app.domain.models` only — raw payloads stay in memory
and survive solely as a SHA-256 fingerprint inside :class:`Provenance`.

Author and video figures are taken from the fields embedded in the search
result (``item.author`` / ``item.authorStats``). No profile page is fetched:
profile SSR is behind a WAF challenge in Phase 1.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable

from app.domain.models import (
    ENTITY_TYPE_CREATOR,
    ENTITY_TYPE_VIDEO,
    PLATFORM_TIKTOK,
    SOURCE_SCOPE_PUBLIC_SEARCH_RESULT,
    SOURCE_TIKTOK_SEARCH_GENERAL_FULL,
    Creator,
    ParsedSearch,
    Provenance,
    Video,
)
from app.domain.normalization import (
    canonical_profile_url,
    hash_session_id,
    is_hashed_session_id,
    normalize_hashtag,
    normalize_keyword,
    normalize_username,
)

__all__ = ["ParsedSearch", "parse_search_response"]

# `data` mixes video records (upstream type 1, or "item" in normalized dumps)
# with user cards (upstream type 4, carrying `user_list`). Only the former are
# videos; anything else is skipped explicitly rather than accidentally.
_VIDEO_RECORD_TYPES = frozenset({"item", "1"})
_VIDEO_RECORD_TYPE_INT = 1


def parse_search_response(
    response: Any,
    *,
    keywords: Iterable[str],
    session_id_hash: str,
    collected_at: str,
) -> ParsedSearch:
    """Parse a `/api/search/general/full/` response body into domain objects."""
    if not isinstance(response, dict):
        raise ValueError("search response must be a mapping")

    status_code = response.get("status_code")
    if status_code != 0:
        raise ValueError(f"unsupported upstream status_code: {status_code!r}")

    session_ref = _coerce_session_id_hash(session_id_hash)
    normalized_keywords = _normalize_keywords(keywords)

    creators: dict[str, Creator] = {}
    videos: list[Video] = []
    provenances: list[Provenance] = []

    for record in response.get("data") or []:
        if not _is_video_record(record):
            continue

        item = record["item"]
        video_id = _clean_text(item.get("id"))
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        username = _author_username(author)
        if not video_id or not username:
            continue

        creator = _build_creator(author, item, collected_at)
        creators.setdefault(creator.id, creator)
        creator = creators[creator.id]

        video = _build_video(
            item,
            creator_id=creator.id,
            username=creator.username,
            collected_at=collected_at,
            session_id_hash=session_ref,
        )
        videos.append(video)

        provenances.append(
            _build_provenance(
                entity_type=ENTITY_TYPE_VIDEO,
                entity_id=video.id,
                raw_payload=item,
                keywords=normalized_keywords,
                collected_at=collected_at,
                session_id_hash=session_ref,
            )
        )
        provenances.append(
            _build_provenance(
                entity_type=ENTITY_TYPE_CREATOR,
                entity_id=creator.id,
                raw_payload={
                    "author": author,
                    "authorStats": item.get("authorStats"),
                },
                keywords=normalized_keywords,
                collected_at=collected_at,
                session_id_hash=session_ref,
            )
        )

    # A creator seen twice in one response yields a duplicate provenance pair;
    # collapse by identity so the tuple stays a faithful observation list.
    unique_provenances: dict[str, Provenance] = {}
    for provenance in provenances:
        unique_provenances.setdefault(provenance.id, provenance)

    return ParsedSearch(
        creators=tuple(creators.values()),
        videos=tuple(videos),
        provenances=tuple(unique_provenances.values()),
    )


def _is_video_record(record: Any) -> bool:
    if not isinstance(record, dict):
        return False
    if not isinstance(record.get("item"), dict):
        return False

    record_type = record.get("type")
    if record_type == _VIDEO_RECORD_TYPE_INT:
        return True
    return isinstance(record_type, str) and record_type in _VIDEO_RECORD_TYPES


def _build_creator(author: dict[str, Any], item: dict[str, Any], collected_at: str) -> Creator:
    stats = _stats_mapping(item, "authorStats", "authorStatsV2")
    username = _author_username(author)
    platform_user_id = _first_text(author, "id", "uid", "secUid", "sec_uid")

    return Creator(
        id=f"{PLATFORM_TIKTOK}:{username}",
        platform=PLATFORM_TIKTOK,
        platform_user_id=platform_user_id,
        username=username,
        display_name=_clean_text(author.get("nickname")),
        bio=_clean_text(author.get("signature")),
        profile_url=canonical_profile_url(username),
        avatar_url=_extract_url(_first_present(author, "avatarLarger", "avatarThumb", "avatar_larger", "avatar_thumb")),
        region_code=_first_text(author, "region", "region_code"),
        language=_first_text(author, "language"),
        is_verified=_optional_bool(author.get("verified")),
        follower_count=_to_int(_first_present(stats, "followerCount", "follower_count")),
        following_count=_to_int(_first_present(stats, "followingCount", "following_count")),
        likes_count=_to_int(_first_present(stats, "heartCount", "heart", "likes_count")),
        video_count=_to_int(_first_present(stats, "videoCount", "video_count")),
        first_seen_at=collected_at,
        last_seen_at=collected_at,
    )


def _build_video(
    item: dict[str, Any],
    *,
    creator_id: str,
    username: str,
    collected_at: str,
    session_id_hash: str,
) -> Video:
    video_id = _clean_text(item.get("id")) or ""
    stats = _stats_mapping(item, "stats", "statsV2")

    return Video(
        id=f"{PLATFORM_TIKTOK}:{video_id}",
        platform_video_id=video_id,
        creator_id=creator_id,
        description=_clean_text(item.get("desc")),
        hashtags=_extract_hashtags(item),
        create_time=_to_iso8601(item.get("createTime", item.get("create_time"))),
        region_code=_first_text(item, "region", "region_code"),
        view_count=_to_int(_first_present(stats, "playCount", "play_count")),
        like_count=_to_int(_first_present(stats, "diggCount", "digg_count")),
        comment_count=_to_int(_first_present(stats, "commentCount", "comment_count")),
        share_count=_to_int(_first_present(stats, "shareCount", "share_count")),
        video_url=f"{canonical_profile_url(username)}/video/{video_id}",
        collected_at=collected_at,
        source=SOURCE_TIKTOK_SEARCH_GENERAL_FULL,
        source_scope=SOURCE_SCOPE_PUBLIC_SEARCH_RESULT,
        session_id_hash=session_id_hash,
    )


def _build_provenance(
    *,
    entity_type: str,
    entity_id: str,
    raw_payload: Any,
    keywords: tuple[str, ...],
    collected_at: str,
    session_id_hash: str,
) -> Provenance:
    payload_hash = canonical_payload_hash(raw_payload)
    return Provenance(
        id=f"{entity_type}:{entity_id}:{payload_hash[:16]}",
        entity_type=entity_type,
        entity_id=entity_id,
        source=SOURCE_TIKTOK_SEARCH_GENERAL_FULL,
        source_scope=SOURCE_SCOPE_PUBLIC_SEARCH_RESULT,
        keywords=keywords,
        collected_at=collected_at,
        session_id_hash=session_id_hash,
        raw_payload_hash=payload_hash,
    )


def canonical_payload_hash(payload: Any) -> str:
    """SHA-256 of the canonical JSON encoding of *payload* (hex, no prefix)."""
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _coerce_session_id_hash(value: Any) -> str:
    """Only ever keep a session reference in irreversible hashed form."""
    if isinstance(value, str) and is_hashed_session_id(value):
        return value
    return hash_session_id(value)


def _normalize_keywords(keywords: Iterable[str]) -> tuple[str, ...]:
    normalized = (normalize_keyword(keyword) for keyword in keywords or ())
    return tuple(dict.fromkeys(item for item in normalized if item))


def _author_username(author: dict[str, Any]) -> str:
    return normalize_username(_first_present(author, "uniqueId", "unique_id", "shortId", "short_id"))


def _stats_mapping(item: dict[str, Any], primary: str, secondary: str) -> dict[str, Any]:
    for key in (primary, secondary):
        value = item.get(key)
        if isinstance(value, dict) and value:
            return value
    return {}


def _first_present(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def _first_text(mapping: dict[str, Any], *keys: str) -> str | None:
    return _clean_text(_first_present(mapping, *keys))


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _extract_url(value: Any) -> str | None:
    """Return the first usable URL, verbatim.

    Exemption (deliberate, see ``tests/test_parser.py``): TikTok avatar URLs are
    public CDN addresses and carry upstream CDN query parameters such as
    ``x-signature=``, ``refresh_token=`` and ``x-expires=``. Those parameters are
    produced by TikTok's own CDN for the *already public* image object — they are
    not request-signing material minted locally, not session credentials, and
    stripping them would break the only avatar reference we have. The sensitive
    material this module must never surface is session state (``msToken``,
    ``ttwid``, ``odin_tt``, ``sessionid``, ``sid_tt``) and local signature
    inputs; none of those ever reach a domain field, and the value-level guard
    in the test asserts exactly that list.
    """
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        urls = value.get("url_list")
        if isinstance(urls, list):
            for url in urls:
                if isinstance(url, str) and url.strip():
                    return url.strip()
    return None


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _to_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        candidate = value.strip().replace(",", "")
        if candidate.isdigit():
            return int(candidate)
    return None


def _to_iso8601(value: Any) -> str | None:
    if isinstance(value, str):
        return _clean_text(value)
    epoch = _to_int(value)
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _extract_hashtags(item: dict[str, Any]) -> tuple[str, ...]:
    raw: list[str] = []

    for challenge in item.get("challenges") or []:
        if isinstance(challenge, dict):
            title = challenge.get("title")
            if isinstance(title, str):
                raw.append(title)

    for extra in item.get("textExtra") or []:
        if isinstance(extra, dict):
            name = extra.get("hashtagName")
            if isinstance(name, str):
                raw.append(name)

    hashtags = (normalize_hashtag(name) for name in raw)
    return tuple(dict.fromkeys(tag for tag in hashtags if tag))
