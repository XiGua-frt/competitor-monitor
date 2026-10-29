"""Frozen domain models shared by discovery, storage and the internal API.

Rules that every producer of these models must honour:

* a missing upstream value is ``None`` — never ``0``, never ``""``;
* session material appears only as the irreversible ``session_id_hash``;
* no raw payload, Cookie, msToken, storage state or signature ever becomes a
  field of a domain object.
"""

from __future__ import annotations

from dataclasses import dataclass

PLATFORM_TIKTOK = "tiktok"

SOURCE_TIKTOK_SEARCH_GENERAL_FULL = "tiktok_search_general_full"
SOURCE_SCOPE_PUBLIC_SEARCH_RESULT = "public_search_result"

ENTITY_TYPE_CREATOR = "creator"
ENTITY_TYPE_VIDEO = "video"


@dataclass(frozen=True)
class Creator:
    id: str
    platform: str
    platform_user_id: str | None
    username: str
    display_name: str | None
    bio: str | None
    profile_url: str
    avatar_url: str | None
    region_code: str | None
    language: str | None
    is_verified: bool | None
    follower_count: int | None
    following_count: int | None
    likes_count: int | None
    video_count: int | None
    first_seen_at: str
    last_seen_at: str
    deleted_at: str | None = None


@dataclass(frozen=True)
class Video:
    id: str
    platform_video_id: str
    creator_id: str
    description: str | None
    hashtags: tuple[str, ...]
    create_time: str | None
    region_code: str | None
    view_count: int | None
    like_count: int | None
    comment_count: int | None
    share_count: int | None
    video_url: str | None
    collected_at: str
    source: str
    source_scope: str
    session_id_hash: str


@dataclass(frozen=True)
class Provenance:
    """Where a single observed entity came from, and how to re-verify it."""

    id: str
    entity_type: str
    entity_id: str
    source: str
    source_scope: str
    keywords: tuple[str, ...]
    collected_at: str
    session_id_hash: str
    raw_payload_hash: str


@dataclass(frozen=True)
class ParsedSearch:
    """Everything one search response yields, already in domain form."""

    creators: tuple[Creator, ...]
    videos: tuple[Video, ...]
    provenances: tuple[Provenance, ...]


@dataclass(frozen=True)
class SearchResult:
    """一位作者的聚合评分结果行 —— discovery service 每行返回的东西。

    与 :class:`ParsedSearch` 的关系：两者不是同一个语义，这里不是别名。
    ``ParsedSearch`` 是「一次搜索响应」的原始领域切片（creators / videos /
    provenances 三个集合）；``SearchResult`` 是在它之上经过去重、聚合与评分之后
    得到的「每位作者一行」的结果行 —— 携带 scores 与 ``match_reasons``，与
    :mod:`app.domain.scoring` 的 ``ScoredCreator`` 一一对应。

    之所以单独定义在 ``models.py`` 而不是从 ``scoring`` 导出别名：``scoring``
    依赖本模块，反向导入会成环；因此由 discovery service 在评分之后把
    ``ScoredCreator`` 投影成 ``SearchResult``，本模型自身不引用评分实现。

    与 ``ParsedSearch`` 一样遵守本模块顶部三条规则，尤其：行内不含 raw payload、
    Cookie、msToken 或签名输入；会话只以不可逆的哈希出现在 :class:`Provenance`。
    """

    creator_id: str
    username: str
    profile_url: str
    keywords: tuple[str, ...]
    total_score: float | None
    relevance_score: float | None
    quality_score: float | None
    freshness_score: float | None
    score_version: str
    rank: int | None
    matched_video_count: int
    matched_keywords: tuple[str, ...]
    match_reasons: tuple[str, ...]
    unavailable_metrics: tuple[str, ...]
    collected_at: str
    source: str
    source_scope: str
