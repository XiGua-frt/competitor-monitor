"""Collapse repeated observations of the same author / video into one record.

Identity rules (in descending priority):

1. ``platform_user_id`` — the stable upstream id;
2. the canonical profile URL (see :func:`app.domain.normalization.canonical_profile_url`);
3. ``platform + normalized username``;
4. the domain ``id`` as a last resort, so records that carry no identity at all
   are never merged into each other.

Display name is deliberately **not** an identity key: it is free text, changes
at will and is not unique.

Identity keys are adjudicated **in order** rather than applied all at once: the
first tier that both records carry decides. A creator with a
``platform_user_id`` therefore only ever merges with records carrying the *same*
``platform_user_id``; the weaker tiers are the fallback for records that have no
stable id at all, and they may never pull two different ``platform_user_id``
into one author. Values missing on the winning record are filled in from the
other records rather than being lost.
"""

from __future__ import annotations

from dataclasses import fields
from typing import Iterable

from app.domain.models import Creator, Video
from app.domain.normalization import canonical_profile_url, normalize_username

__all__ = [
    "creator_identity_key",
    "creator_identity_keys",
    "creator_id_merge_map",
    "deduplicate_creators",
    "deduplicate_videos",
    "video_identity_key",
]

PLATFORM_USER_ID_PREFIX = "platform_user_id:"
PROFILE_URL_PREFIX = "profile_url:"
USERNAME_PREFIX = "username:"
FALLBACK_PREFIX = "creator_id:"


def creator_identity_keys(creator: Creator) -> tuple[str, ...]:
    """Every key *creator* can be recognised by, strongest first."""
    keys: list[str] = []

    platform_user_id = creator.platform_user_id
    if isinstance(platform_user_id, str) and platform_user_id.strip():
        keys.append(f"{PLATFORM_USER_ID_PREFIX}{creator.platform}:{platform_user_id.strip()}")

    profile_url = canonical_profile_url(creator.profile_url) or canonical_profile_url(
        creator.username
    )
    if profile_url:
        keys.append(f"{PROFILE_URL_PREFIX}{profile_url}")

    username = normalize_username(creator.username)
    if username:
        keys.append(f"{USERNAME_PREFIX}{creator.platform}:{username}")

    if not keys:
        keys.append(f"{FALLBACK_PREFIX}{creator.id}")

    return tuple(keys)


def creator_identity_key(creator: Creator) -> str | None:
    """The strongest key available, or ``None`` for a record without identity."""
    keys = creator_identity_keys(creator)
    if len(keys) == 1 and keys[0].startswith(FALLBACK_PREFIX):
        return None
    return keys[0]


def deduplicate_creators(creators: Iterable[Creator]) -> tuple[Creator, ...]:
    """Return one :class:`Creator` per real author, in first-seen order."""
    records = list(creators or ())
    return tuple(_merge_group(group) for group in _group_creators(records))


def creator_id_merge_map(creators: Iterable[Creator]) -> dict[str, str]:
    """Map every input ``Creator.id`` onto the id that survives deduplication.

    Videos keep pointing at the id they were parsed with; this map lets the
    aggregation layer re-point them at the canonical author.
    """
    records = list(creators or ())
    mapping: dict[str, str] = {}
    for group in _group_creators(records):
        canonical_id = _merge_group(group).id
        for creator in group:
            mapping[creator.id] = canonical_id
    return mapping


def deduplicate_videos(videos: Iterable[Video]) -> tuple[Video, ...]:
    """Return one :class:`Video` per platform video, in first-seen order."""
    groups: dict[str, list[Video]] = {}
    order: list[str] = []
    for video in videos or ():
        key = video_identity_key(video)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(video)
    return tuple(_merge_video_group(groups[key]) for key in order)


def video_identity_key(video: Video) -> str:
    platform_video_id = video.platform_video_id
    if isinstance(platform_video_id, str) and platform_video_id.strip():
        return f"platform_video_id:{platform_video_id.strip()}"
    return f"video_id:{video.id}"


def _key_tier(key: str) -> int | None:
    """The adjudication step *key* belongs to; ``None`` = never merges anybody."""
    if key.startswith(PLATFORM_USER_ID_PREFIX):
        return 0
    if key.startswith(PROFILE_URL_PREFIX):
        return 1
    if key.startswith(USERNAME_PREFIX):
        return 2
    return None


def _group_creators(records: list[Creator]) -> list[list[Creator]]:
    """Union-find grouping with *tiered* keys: strongest tier is decided first.

    Unions are attempted one tier at a time — ``platform_user_id``, then the
    canonical profile URL, then ``platform + username`` — and a union is refused
    when it would put two different ``platform_user_id`` into one author. That
    keeps the priority order intact: a renamed handle may still fold into the
    author that carries the matching stable id, but two distinct stable ids can
    never be bridged by a shared username or profile URL.
    """
    parent = list(range(len(records)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def stable_ids(root: int) -> set[str]:
        ids: set[str] = set()
        for index, creator in enumerate(records):
            if find(index) != root:
                continue
            value = creator.platform_user_id
            if isinstance(value, str) and value.strip():
                ids.add(value.strip())
        return ids

    def union(left: int, right: int) -> None:
        root, other = find(left), find(right)
        if root == other:
            return
        if len(stable_ids(root) | stable_ids(other)) > 1:
            return  # a fallback key must not merge two stable ids
        parent[max(root, other)] = min(root, other)

    tiers = [
        tuple((_key_tier(key), key) for key in creator_identity_keys(creator))
        for creator in records
    ]
    highest = max(
        (tier for creator_keys in tiers for tier, _ in creator_keys if tier is not None),
        default=-1,
    )

    for tier in range(highest + 1):
        key_owner: dict[str, int] = {}
        for index, creator_keys in enumerate(tiers):
            key = next((value for key_tier, value in creator_keys if key_tier == tier), None)
            if key is None:
                continue
            union(index, key_owner.setdefault(key, index))

    groups: dict[int, list[Creator]] = {}
    group_order: list[int] = []
    for index, creator in enumerate(records):
        root = find(index)
        if root not in groups:
            groups[root] = []
            group_order.append(root)
        groups[root].append(creator)
    return [groups[root] for root in group_order]


def _merge_group(group: list[Creator]) -> Creator:
    if len(group) == 1:
        return group[0]

    winner = max(
        range(len(group)),
        key=lambda index: (
            1 if group[index].platform_user_id else 0,
            _completeness(group[index]),
            -index,
        ),
    )
    merged = group[winner]

    values: dict[str, object] = {}
    for name in _CREATOR_VALUE_FIELDS:
        value = getattr(merged, name)
        if value is None:
            for creator in group:
                candidate = getattr(creator, name)
                if candidate is not None:
                    value = candidate
                    break
        values[name] = value

    first_seen = [c.first_seen_at for c in group if c.first_seen_at]
    last_seen = [c.last_seen_at for c in group if c.last_seen_at]

    return Creator(
        id=merged.id,
        platform=merged.platform,
        platform_user_id=merged.platform_user_id,
        username=merged.username,
        display_name=values["display_name"],
        bio=values["bio"],
        profile_url=merged.profile_url,
        avatar_url=values["avatar_url"],
        region_code=values["region_code"],
        language=values["language"],
        is_verified=values["is_verified"],
        follower_count=values["follower_count"],
        following_count=values["following_count"],
        likes_count=values["likes_count"],
        video_count=values["video_count"],
        first_seen_at=min(first_seen) if first_seen else merged.first_seen_at,
        last_seen_at=max(last_seen) if last_seen else merged.last_seen_at,
        deleted_at=merged.deleted_at,
    )


def _merge_video_group(group: list[Video]) -> Video:
    if len(group) == 1:
        return group[0]

    winner = max(range(len(group)), key=lambda index: (_completeness(group[index]), -index))
    merged = group[winner]

    values: dict[str, object] = {}
    for name in _VIDEO_VALUE_FIELDS:
        value = getattr(merged, name)
        if value is None:
            for video in group:
                candidate = getattr(video, name)
                if candidate is not None:
                    value = candidate
                    break
        values[name] = value

    return Video(
        id=merged.id,
        platform_video_id=merged.platform_video_id,
        creator_id=merged.creator_id,
        description=values["description"],
        hashtags=merged.hashtags or values["hashtags"],
        create_time=values["create_time"],
        region_code=values["region_code"],
        view_count=values["view_count"],
        like_count=values["like_count"],
        comment_count=values["comment_count"],
        share_count=values["share_count"],
        video_url=values["video_url"],
        collected_at=min(v.collected_at for v in group if v.collected_at) or merged.collected_at,
        source=merged.source,
        source_scope=merged.source_scope,
        session_id_hash=merged.session_id_hash,
    )


_CREATOR_VALUE_FIELDS = (
    "display_name",
    "bio",
    "avatar_url",
    "region_code",
    "language",
    "is_verified",
    "follower_count",
    "following_count",
    "likes_count",
    "video_count",
)

_VIDEO_VALUE_FIELDS = (
    "description",
    "hashtags",
    "create_time",
    "region_code",
    "view_count",
    "like_count",
    "comment_count",
    "share_count",
    "video_url",
)


def _completeness(record: object) -> int:
    """How many optional fields the record actually carries."""
    return sum(
        1
        for field in fields(record)
        if getattr(record, field.name) is not None
    )
