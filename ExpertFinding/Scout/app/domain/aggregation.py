"""Fold many search videos into one explainable row per author.

A "matched" video is a video that

* is not filtered out by an ``exclude_keywords`` hit (description or author bio),
* and, when keywords/hashtags are supplied, hits at least one of them in its own
  description or hashtags.

Keyword matching is Unicode ``casefold`` substring matching over the video
description **and** the author bio; hashtags are compared as normalized values.

The two levels are kept apart: a keyword found in the **bio** is *author-level*
evidence — it qualifies the author (and is counted exactly once per author), but
it never promotes a single video and never inflates ``matched_video_count`` or
the ``total_*`` sums, which are computed from videos that matched on their own.
Every row carries ``match_reasons`` strings of the form ``field=value:count`` so
a human can see which field matched, on which value and how often. Missing
upstream numbers stay ``None`` and are additionally listed in
``unavailable_metrics`` — never silently turned into ``0``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from app.domain.deduplication import (
    creator_id_merge_map,
    deduplicate_creators,
    deduplicate_videos,
)
from app.domain.models import Creator, Video
from app.domain.normalization import normalize_hashtag, normalize_keyword

__all__ = ["CreatorAggregate", "aggregate_creator_results"]

_WHITESPACE = re.compile(r"\s+")

_UNAVAILABLE_VIDEO_FIELDS = (
    "view_count",
    "like_count",
    "comment_count",
    "share_count",
    "create_time",
)


@dataclass(frozen=True)
class CreatorAggregate:
    """All matched evidence for one author, before any score is applied."""

    creator: Creator
    videos: tuple[Video, ...]
    matched_videos: tuple[Video, ...]
    matched_video_count: int
    matched_keywords: tuple[str, ...]
    matched_hashtags: tuple[str, ...]
    match_reasons: tuple[str, ...]
    keywords: tuple[str, ...]
    hashtags: tuple[str, ...]
    total_view_count: int | None
    total_like_count: int | None
    total_comment_count: int | None
    total_share_count: int | None
    newest_create_time: str | None
    unavailable_metrics: tuple[str, ...]
    aggregated_at: str | None


def aggregate_creator_results(
    *,
    creators: Iterable[Creator],
    videos: Iterable[Video],
    keywords: Iterable[str] = (),
    hashtags: Iterable[str] = (),
    exclude_keywords: Iterable[str] = (),
    now: str | None = None,
) -> tuple[CreatorAggregate, ...]:
    """Return one aggregate per author carrying matched evidence.

    An author survives either because one of its videos matched, or because its
    own bio matched a keyword; in the latter case the row can legitimately have
    ``matched_video_count == 0``.
    """
    author_list = list(creators or ())
    video_list = list(videos or ())

    wanted_keywords = _normalized_keywords(keywords)
    wanted_hashtags = _normalized_hashtags(hashtags)
    blocked_keywords = _normalized_keywords(exclude_keywords)

    authors = deduplicate_creators(author_list)
    id_map = creator_id_merge_map(author_list)
    unique_videos = deduplicate_videos(video_list)

    by_author: dict[str, list[Video]] = {author.id: [] for author in authors}
    for video in unique_videos:
        owner = id_map.get(video.creator_id, video.creator_id)
        if owner in by_author:
            by_author[owner].append(video)

    rows: list[CreatorAggregate] = []
    for author in authors:
        row = _aggregate_author(
            author,
            tuple(by_author[author.id]),
            keywords=wanted_keywords,
            hashtags=wanted_hashtags,
            exclude_keywords=blocked_keywords,
            now=now,
        )
        if row is not None:
            rows.append(row)
    return tuple(rows)


def _aggregate_author(
    author: Creator,
    videos: tuple[Video, ...],
    *,
    keywords: tuple[str, ...],
    hashtags: tuple[str, ...],
    exclude_keywords: tuple[str, ...],
    now: str | None,
) -> CreatorAggregate | None:
    bio = _fold(author.bio)

    for blocked in exclude_keywords:
        if blocked and blocked in bio:
            return None  # the author's own bio disqualifies every video

    # Author-level evidence: the bio is one string per author, so it is counted
    # once — never multiplied by the number of videos the author happens to have.
    bio_hits = [word for word in keywords if word and word in bio]

    matched: list[Video] = []
    hit_keywords: dict[str, dict[str, int]] = {}
    hit_hashtags: dict[str, int] = {}
    excluded: list[str] = []

    for video in videos:
        description = _fold(video.description)
        if any(blocked and blocked in description for blocked in exclude_keywords):
            for blocked in exclude_keywords:
                if blocked and blocked in description:
                    excluded.append(f"excluded:description={blocked}")
            continue

        keyword_hits = [word for word in keywords if word and word in description]
        hashtag_hits = _matching_hashtags(video, hashtags)

        has_criteria = bool(keywords or hashtags)
        if has_criteria and not (keyword_hits or hashtag_hits):
            continue

        matched.append(video)
        for word in keyword_hits:
            hit_keywords.setdefault(word, {}).setdefault("description", 0)
            hit_keywords[word]["description"] += 1
        for tag in hashtag_hits:
            hit_hashtags[tag] = hit_hashtags.get(tag, 0) + 1

    for word in bio_hits:
        hit_keywords.setdefault(word, {})["bio"] = 1

    if not matched and not bio_hits:
        return None

    reasons: list[str] = []
    for word in keywords:
        counters = hit_keywords.get(word)
        if not counters:
            continue
        for field in ("description", "bio"):
            count = counters.get(field)
            if count:
                reasons.append(f"keyword:{field}={word}:{count}")
    for tag in hashtags:
        count = hit_hashtags.get(tag)
        if count:
            reasons.append(f"hashtag:hashtags={tag}:{count}")
    if not keywords and not hashtags:
        reasons.append(f"unfiltered:matched_videos:{len(matched)}")
    reasons.extend(dict.fromkeys(excluded))

    matched_keywords = tuple(word for word in keywords if word in hit_keywords)
    matched_hashtags = tuple(tag for tag in hashtags if tag in hit_hashtags)

    unavailable = list(_missing_author_metrics(author))
    # A bio-only row matches no video at all, yet still reports `None` totals:
    # explain them against the author's videos instead of leaving them silent.
    probe = matched or videos
    for field in _UNAVAILABLE_VIDEO_FIELDS:
        if any(getattr(video, field) is None for video in probe):
            marker = f"{field}_unavailable"
            if marker not in unavailable:
                unavailable.append(marker)
    reasons.extend(marker for marker in unavailable if marker not in reasons)

    create_times = [video.create_time for video in matched if video.create_time]

    return CreatorAggregate(
        creator=author,
        videos=videos,
        matched_videos=tuple(matched),
        matched_video_count=len(matched),
        matched_keywords=matched_keywords,
        matched_hashtags=matched_hashtags,
        match_reasons=tuple(dict.fromkeys(reasons)),
        keywords=keywords,
        hashtags=hashtags,
        total_view_count=_sum(matched, "view_count"),
        total_like_count=_sum(matched, "like_count"),
        total_comment_count=_sum(matched, "comment_count"),
        total_share_count=_sum(matched, "share_count"),
        newest_create_time=max(create_times) if create_times else None,
        unavailable_metrics=tuple(unavailable),
        aggregated_at=now,
    )


def _missing_author_metrics(author: Creator) -> Iterable[str]:
    if author.follower_count is None:
        yield "follower_count_unavailable"
    if author.video_count is None:
        yield "author_video_count_unavailable"
    if author.likes_count is None:
        yield "author_likes_count_unavailable"
    if not author.bio:
        yield "bio_unavailable"


def _matching_hashtags(video: Video, hashtags: tuple[str, ...]) -> list[str]:
    if not hashtags:
        return []
    present = {normalize_hashtag(tag) for tag in video.hashtags or ()}
    return [tag for tag in hashtags if tag in present]


def _normalized_keywords(keywords: Iterable[str]) -> tuple[str, ...]:
    folded = (_fold_keyword(keyword) for keyword in keywords or ())
    return tuple(dict.fromkeys(item for item in folded if item))


def _normalized_hashtags(hashtags: Iterable[str]) -> tuple[str, ...]:
    normalized = (normalize_hashtag(tag) for tag in hashtags or ())
    return tuple(dict.fromkeys(tag for tag in normalized if tag))


def _fold_keyword(value: str) -> str:
    return _WHITESPACE.sub(" ", normalize_keyword(value)).casefold()


def _fold(value: str | None) -> str:
    if not isinstance(value, str):
        return ""
    return _WHITESPACE.sub(" ", value).casefold()


def _sum(videos: tuple[Video, ...], field: str) -> int | None:
    values = [getattr(video, field) for video in videos]
    values = [value for value in values if isinstance(value, int)]
    return sum(values) if values else None
