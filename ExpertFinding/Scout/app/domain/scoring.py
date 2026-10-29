"""Explainable relevance / quality / freshness scoring for one author.

Rules (Phase 1, version ``phase1-v1``):

* ``total_score = 0.55 * relevance + 0.35 * quality + 0.10 * freshness``.
* A component that cannot be computed (no keyword criteria, no view counts, no
  create time, ...) is ``None`` — never ``0`` — and is named in
  ``missing_components`` / ``unavailable_metrics``.
* When a component is missing the weights of the *remaining* components are
  renormalised over their own sum, so a missing component cannot silently drag
  the total down. This deliberately deviates from the literal formula above in
  favour of the "missing ≠ 0" rule; see :func:`score_components` and always read
  a renormalised ``total_score`` together with its ``weight_basis``.
* ``engagement_rate = (likes + comments + shares) / max(views, 1)`` is only
  computed when every participant is present.

Changing any of these rules requires bumping :data:`SCORE_VERSION`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from app.domain.aggregation import CreatorAggregate
from app.domain.models import Creator

__all__ = [
    "SCORE_VERSION",
    "RELEVANCE_WEIGHT",
    "QUALITY_WEIGHT",
    "FRESHNESS_WEIGHT",
    "ScoreBreakdown",
    "ScoredCreator",
    "score_components",
    "score_creator",
    "score_creators",
]

SCORE_VERSION = "phase1-v1"

RELEVANCE_WEIGHT = 0.55
QUALITY_WEIGHT = 0.35
FRESHNESS_WEIGHT = 0.10

# An engagement rate of 10 % saturates the quality sub-score.
ENGAGEMENT_REFERENCE_RATE = 0.10
# 10 million followers saturates the follower sub-score.
FOLLOWER_REFERENCE_LOG10 = 7.0
# A video older than a year contributes no freshness at all.
FRESHNESS_WINDOW_DAYS = 365.0

_KEYWORD_COVERAGE_WEIGHT = 0.5
_HASHTAG_COVERAGE_WEIGHT = 0.3
_MATCH_DEPTH_WEIGHT = 0.2
_ENGAGEMENT_WEIGHT = 0.7
_FOLLOWER_WEIGHT = 0.3


@dataclass(frozen=True)
class ScoreBreakdown:
    """The weighted combination itself, independent of any author."""

    relevance_score: float | None
    quality_score: float | None
    freshness_score: float | None
    total_score: float | None
    score_version: str
    weight_basis: tuple[str, ...]
    missing_components: tuple[str, ...]


@dataclass(frozen=True)
class ScoredCreator:
    """One author, its aggregated evidence and the explanation of the score."""

    creator: Creator
    aggregate: CreatorAggregate
    relevance_score: float | None
    quality_score: float | None
    freshness_score: float | None
    total_score: float | None
    score_version: str
    weight_basis: tuple[str, ...]
    missing_components: tuple[str, ...]
    engagement_rate: float | None
    matched_video_count: int
    matched_keywords: tuple[str, ...]
    match_reasons: tuple[str, ...]
    unavailable_metrics: tuple[str, ...]
    scored_at: str | None


def score_components(
    *,
    relevance: float | None = None,
    quality: float | None = None,
    freshness: float | None = None,
) -> ScoreBreakdown:
    """Combine the three components into the Phase 1 total score."""
    weights = {
        "relevance": (RELEVANCE_WEIGHT, relevance),
        "quality": (QUALITY_WEIGHT, quality),
        "freshness": (FRESHNESS_WEIGHT, freshness),
    }
    basis = tuple(name for name, (_, value) in weights.items() if value is not None)
    missing = tuple(name for name, (_, value) in weights.items() if value is None)

    if not basis:
        total = None
    else:
        # Deliberate deviation from the literal spec formula
        # ``0.55r + 0.35q + 0.10f``: a component that cannot be computed is not
        # treated as 0 (that would silently punish an author for missing data and
        # violate the "missing ≠ 0", never-fabricate-a-number rule of Phase 1).
        # Instead the weights of the *available* components are renormalised over
        # their own sum, so the result stays on the same 0-100 scale. The tradeoff
        # is that the total is then a weighted average of what could be measured,
        # not a share of the full formula — callers are told exactly which
        # components the number rests on via ``weight_basis`` /
        # ``missing_components`` and must read the two together. Fully-measured
        # authors keep the exact spec formula unchanged.
        total_weight = sum(weights[name][0] for name in basis)
        total = sum(weights[name][0] * weights[name][1] for name in basis) / total_weight

    return ScoreBreakdown(
        relevance_score=relevance,
        quality_score=quality,
        freshness_score=freshness,
        total_score=total,
        score_version=SCORE_VERSION,
        weight_basis=basis,
        missing_components=missing,
    )


def score_creator(aggregate: CreatorAggregate, *, now: str | None = None) -> ScoredCreator:
    """Score one aggregated author and explain every number that is missing."""
    reference_time = now or aggregate.aggregated_at

    unavailable = list(aggregate.unavailable_metrics)
    reasons = list(aggregate.match_reasons)

    relevance = _relevance(aggregate, reasons)
    engagement_rate = _engagement_rate(aggregate, unavailable)
    quality = _quality(aggregate, engagement_rate, unavailable)
    freshness = _freshness(aggregate, reference_time, unavailable)

    for component, value in (("relevance", relevance), ("quality", quality), ("freshness", freshness)):
        if value is None:
            reasons.append(f"{component}_unavailable")

    breakdown = score_components(relevance=relevance, quality=quality, freshness=freshness)
    reasons.append(f"score_version:{SCORE_VERSION}")
    if engagement_rate is not None:
        reasons.append(f"engagement_rate:{engagement_rate:.4f}")

    return ScoredCreator(
        creator=aggregate.creator,
        aggregate=aggregate,
        relevance_score=relevance,
        quality_score=quality,
        freshness_score=freshness,
        total_score=breakdown.total_score,
        score_version=SCORE_VERSION,
        weight_basis=breakdown.weight_basis,
        missing_components=breakdown.missing_components,
        engagement_rate=engagement_rate,
        matched_video_count=aggregate.matched_video_count,
        matched_keywords=aggregate.matched_keywords,
        match_reasons=tuple(dict.fromkeys(reasons)),
        unavailable_metrics=tuple(dict.fromkeys(unavailable)),
        scored_at=reference_time,
    )


def score_creators(
    aggregates: Iterable[CreatorAggregate], *, now: str | None = None
) -> tuple[ScoredCreator, ...]:
    """Score every aggregate, best first; unscorable rows keep their order at the end."""
    scored = [score_creator(aggregate, now=now) for aggregate in aggregates or ()]
    scored.sort(key=lambda row: (row.total_score is None, -(row.total_score or 0.0)))
    return tuple(scored)


def _relevance(aggregate: CreatorAggregate, reasons: list[str]) -> float | None:
    if not aggregate.keywords and not aggregate.hashtags:
        return None

    parts: list[tuple[float, float]] = []
    if aggregate.keywords:
        coverage = len(aggregate.matched_keywords) / len(aggregate.keywords)
        parts.append((_KEYWORD_COVERAGE_WEIGHT, coverage))
        reasons.append(f"keyword_coverage:{len(aggregate.matched_keywords)}/{len(aggregate.keywords)}")
    if aggregate.hashtags:
        coverage = len(aggregate.matched_hashtags) / len(aggregate.hashtags)
        parts.append((_HASHTAG_COVERAGE_WEIGHT, coverage))
        reasons.append(f"hashtag_coverage:{len(aggregate.matched_hashtags)}/{len(aggregate.hashtags)}")

    total_videos = max(len(aggregate.videos), 1)
    depth = min(1.0, aggregate.matched_video_count / total_videos)
    parts.append((_MATCH_DEPTH_WEIGHT, depth))
    reasons.append(f"match_depth:{aggregate.matched_video_count}/{len(aggregate.videos)}")

    return 100.0 * _weighted(parts)


def _quality(
    aggregate: CreatorAggregate,
    engagement_rate: float | None,
    unavailable: list[str],
) -> float | None:
    parts: list[tuple[float, float]] = []
    if engagement_rate is not None:
        parts.append((_ENGAGEMENT_WEIGHT, min(1.0, engagement_rate / ENGAGEMENT_REFERENCE_RATE)))

    followers = aggregate.creator.follower_count
    if isinstance(followers, int) and followers >= 0:
        scale = min(1.0, math.log10(followers + 1) / FOLLOWER_REFERENCE_LOG10)
        parts.append((_FOLLOWER_WEIGHT, scale))
    elif followers is None:
        _mark(unavailable, "follower_count_unavailable")

    if not parts:
        return None
    return 100.0 * _weighted(parts)


def _engagement_rate(aggregate: CreatorAggregate, unavailable: list[str]) -> float | None:
    """Aggregate engagement over matched videos that carry every participant."""
    views = 0
    interactions = 0
    used = 0

    for video in aggregate.matched_videos:
        counts = (
            video.view_count,
            video.like_count,
            video.comment_count,
            video.share_count,
        )
        if any(not isinstance(value, int) for value in counts):
            for field, value in zip(
                ("view_count", "like_count", "comment_count", "share_count"), counts
            ):
                if not isinstance(value, int):
                    _mark(unavailable, f"{field}_unavailable")
            continue
        views += video.view_count
        interactions += video.like_count + video.comment_count + video.share_count
        used += 1

    if used == 0:
        _mark(unavailable, "engagement_unavailable")
        return None
    return interactions / max(views, 1)


def _freshness(
    aggregate: CreatorAggregate, reference_time: str | None, unavailable: list[str]
) -> float | None:
    newest = aggregate.newest_create_time
    if not newest or not reference_time:
        _mark(unavailable, "create_time_unavailable")
        return None

    created = _parse_datetime(newest)
    reference = _parse_datetime(reference_time)
    if created is None or reference is None:
        _mark(unavailable, "create_time_unavailable")
        return None

    age_days = (reference - created).total_seconds() / 86400.0
    if age_days < 0:
        age_days = 0.0
    return max(0.0, 100.0 * (1.0 - age_days / FRESHNESS_WINDOW_DAYS))


def _weighted(parts: list[tuple[float, float]]) -> float:
    total_weight = sum(weight for weight, _ in parts)
    if total_weight <= 0:
        return 0.0
    return sum(weight * value for weight, value in parts) / total_weight


def _parse_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _mark(unavailable: list[str], marker: str) -> None:
    if marker not in unavailable:
        unavailable.append(marker)
