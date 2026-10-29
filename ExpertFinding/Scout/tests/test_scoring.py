"""Relevance / quality / freshness scoring and the phase1-v1 total score."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.domain.aggregation import aggregate_creator_results
from app.domain.models import Creator, Video
from app.domain.normalization import canonical_profile_url
from app.domain.scoring import (
    SCORE_VERSION,
    score_creator,
    score_creators,
    score_components,
)

NOW = "2026-10-02T00:00:00+00:00"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "search_general_fitness_coach.json"


def creator(*, username: str = "coach", bio: str | None = None, follower_count: int | None = None):
    return Creator(
        id=f"tiktok:{username}",
        platform="tiktok",
        platform_user_id=f"uid-{username}",
        username=username,
        display_name=None,
        bio=bio,
        profile_url=canonical_profile_url(username),
        avatar_url=None,
        region_code=None,
        language=None,
        is_verified=None,
        follower_count=follower_count,
        following_count=None,
        likes_count=None,
        video_count=None,
        first_seen_at=NOW,
        last_seen_at=NOW,
    )


def video(
    *,
    platform_video_id: str,
    creator_id: str = "tiktok:coach",
    description: str | None = None,
    hashtags: tuple[str, ...] = (),
    create_time: str | None = None,
    view_count: int | None = None,
    like_count: int | None = None,
    comment_count: int | None = None,
    share_count: int | None = None,
):
    return Video(
        id=f"tiktok:{platform_video_id}",
        platform_video_id=platform_video_id,
        creator_id=creator_id,
        description=description,
        hashtags=hashtags,
        create_time=create_time,
        region_code=None,
        view_count=view_count,
        like_count=like_count,
        comment_count=comment_count,
        share_count=share_count,
        video_url=None,
        collected_at=NOW,
        source="tiktok_search_general_full",
        source_scope="public_search_result",
        session_id_hash="sha256:" + "0" * 64,
    )


def aggregate(*videos, keywords=("fitness coach",), hashtags=(), creator_=None, follower_count=None):
    author = creator_ or creator(follower_count=follower_count)
    rows = aggregate_creator_results(
        creators=[author],
        videos=list(videos),
        keywords=keywords,
        hashtags=hashtags,
        now=NOW,
    )
    assert len(rows) == 1
    return rows[0]


def test_total_score_uses_spec_weights():
    scored = score_components(relevance=80, quality=60, freshness=50)

    assert scored.total_score == pytest.approx(0.55 * 80 + 0.35 * 60 + 0.10 * 50)
    assert scored.relevance_score == 80
    assert scored.quality_score == 60
    assert scored.freshness_score == 50
    assert scored.score_version == SCORE_VERSION == "phase1-v1"


def test_missing_component_is_renormalized_and_reported_not_swallowed_as_zero():
    scored = score_components(relevance=80, quality=None, freshness=50)

    available_weight = 0.55 + 0.10
    assert scored.total_score == pytest.approx((0.55 * 80 + 0.10 * 50) / available_weight)
    assert scored.quality_score is None
    assert scored.missing_components == ("quality",)
    assert scored.weight_basis == ("relevance", "freshness")


def test_all_components_missing_yields_no_score():
    scored = score_components()

    assert scored.total_score is None
    assert scored.missing_components == ("relevance", "quality", "freshness")


def test_missing_metrics_are_not_treated_as_zero_in_score_explanation():
    row = aggregate(video(platform_video_id="v1", description="fitness coach tips", like_count=10))

    scored = score_creator(row, now=NOW)

    assert scored.quality_score is None or "view_count_unavailable" in scored.match_reasons
    assert scored.total_score is not None
    assert "view_count_unavailable" in scored.match_reasons
    assert scored.engagement_rate is None


def test_engagement_rate_is_only_computed_when_every_participant_exists():
    row = aggregate(
        video(
            platform_video_id="v1",
            description="fitness coach tips",
            view_count=1000,
            like_count=50,
            comment_count=30,
            share_count=20,
        ),
        follower_count=50000,
    )

    scored = score_creator(row, now=NOW)

    assert scored.engagement_rate == pytest.approx((50 + 30 + 20) / 1000)
    assert scored.quality_score is not None


def test_engagement_rate_is_none_when_one_participant_is_missing():
    row = aggregate(
        video(
            platform_video_id="v1",
            description="fitness coach tips",
            view_count=1000,
            like_count=50,
            comment_count=30,
            share_count=None,
        )
    )

    scored = score_creator(row, now=NOW)

    assert scored.engagement_rate is None
    assert "share_count_unavailable" in scored.unavailable_metrics


def test_freshness_uses_the_newest_matched_video():
    row = aggregate(
        video(
            platform_video_id="v1",
            description="fitness coach tips",
            create_time="2026-10-01T00:00:00+00:00",
            view_count=100,
            like_count=1,
            comment_count=1,
            share_count=1,
        ),
        video(
            platform_video_id="v2",
            description="fitness coach routine",
            create_time="2026-01-01T00:00:00+00:00",
            view_count=100,
            like_count=1,
            comment_count=1,
            share_count=1,
        ),
    )

    scored = score_creator(row, now=NOW)

    assert scored.freshness_score == pytest.approx(100 * (1 - 1 / 365))


def test_missing_create_time_reports_freshness_unavailable():
    row = aggregate(video(platform_video_id="v1", description="fitness coach tips"))

    scored = score_creator(row, now=NOW)

    assert scored.freshness_score is None
    assert "create_time_unavailable" in scored.match_reasons
    assert "create_time_unavailable" in scored.unavailable_metrics


def test_relevance_is_unavailable_without_keywords_or_hashtags():
    author = creator()
    row = aggregate_creator_results(
        creators=[author],
        videos=[video(platform_video_id="v1", description="anything")],
        now=NOW,
    )[0]

    scored = score_creator(row, now=NOW)

    assert scored.relevance_score is None
    assert "relevance_unavailable" in scored.match_reasons


def test_score_creator_exposes_aggregate_facts_and_version():
    row = aggregate(
        video(
            platform_video_id="v1",
            description="fitness coach tips",
            create_time="2026-10-01T00:00:00+00:00",
            view_count=1000,
            like_count=50,
            comment_count=30,
            share_count=20,
        ),
        follower_count=50000,
    )

    scored = score_creator(row, now=NOW)

    assert scored.score_version == "phase1-v1"
    assert scored.matched_video_count == 1
    assert scored.matched_keywords == ("fitness coach",)
    assert scored.creator.username == "coach"
    assert 0 <= scored.total_score <= 100


def test_score_creators_sorts_by_total_score_descending():
    strong = creator(username="strong", follower_count=900000)
    weak = creator(username="weak", follower_count=100)
    videos = [
        video(
            platform_video_id="vs",
            creator_id=strong.id,
            description="fitness coach tips",
            create_time="2026-10-01T00:00:00+00:00",
            view_count=100000,
            like_count=9000,
            comment_count=500,
            share_count=400,
        ),
        video(
            platform_video_id="vw",
            creator_id=weak.id,
            description="fitness coach tips",
            create_time="2026-02-01T00:00:00+00:00",
            view_count=1000,
            like_count=5,
            comment_count=1,
            share_count=0,
        ),
    ]
    rows = aggregate_creator_results(
        creators=[weak, strong], videos=videos, keywords=("fitness coach",), now=NOW
    )

    scored = score_creators(rows, now=NOW)

    assert [row.creator.username for row in scored] == ["strong", "weak"]
    assert scored[0].total_score >= scored[1].total_score


def test_real_fixture_every_result_has_reasons_and_a_score():
    from app.domain.parser import parse_search_response
    from app.domain.normalization import hash_session_id

    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    parsed = parse_search_response(
        payload,
        keywords=("fitness coach",),
        session_id_hash=hash_session_id("offline-fixture-session"),
        collected_at=NOW,
    )
    rows = aggregate_creator_results(
        creators=parsed.creators,
        videos=parsed.videos,
        keywords=("fitness coach", "fitness"),
        hashtags=("fitnesscoach",),
        now=NOW,
    )

    scored = score_creators(rows, now=NOW)

    assert scored
    for row in scored:
        assert row.score_version == "phase1-v1"
        assert row.match_reasons
        assert row.total_score is None or 0 <= row.total_score <= 100
        assert row.matched_video_count >= 1
