"""Video → author aggregation and explainable match reasons."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from app.domain.aggregation import aggregate_creator_results
from app.domain.models import Creator, Video
from app.domain.normalization import canonical_profile_url, hash_session_id
from app.domain.parser import parse_search_response

COLLECTED_AT = "2026-10-02T00:00:00+00:00"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "search_general_fitness_coach.json"


def creator(
    *,
    id: str | None = None,
    platform_user_id: str | None = None,
    username: str = "coach",
    bio: str | None = None,
    follower_count: int | None = None,
) -> Creator:
    return Creator(
        id=id or f"tiktok:{username}",
        platform="tiktok",
        platform_user_id=platform_user_id,
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
        first_seen_at=COLLECTED_AT,
        last_seen_at=COLLECTED_AT,
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
) -> Video:
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
        collected_at=COLLECTED_AT,
        source="tiktok_search_general_full",
        source_scope="public_search_result",
        session_id_hash="sha256:" + "0" * 64,
    )


def test_aggregation_returns_one_creator_with_video_metrics_and_reasons():
    coach = creator(username="coach", bio="online fitness coach")
    videos = [
        video(platform_video_id="v1", description="fitness coach tips for beginners"),
        video(platform_video_id="v2", description="another fitness coach routine"),
    ]

    aggregated = aggregate_creator_results(
        creators=[coach],
        videos=videos,
        keywords=("fitness coach",),
        hashtags=("fitness",),
        exclude_keywords=("招聘",),
        now=COLLECTED_AT,
    )

    assert len(aggregated) == 1
    assert aggregated[0].matched_video_count == 2
    assert aggregated[0].matched_keywords == ("fitness coach",)
    assert aggregated[0].match_reasons
    assert any("description=fitness coach" in reason for reason in aggregated[0].match_reasons)
    assert any("bio=fitness coach" in reason for reason in aggregated[0].match_reasons)


def test_match_reason_carries_field_value_and_count():
    coach = creator(username="coach")
    videos = [
        video(platform_video_id="v1", description="fitness coach tips"),
        video(platform_video_id="v2", description="fitness coach routine"),
        video(platform_video_id="v3", description="unrelated clip"),
    ]

    aggregated = aggregate_creator_results(
        creators=[coach], videos=videos, keywords=("fitness coach",), now=COLLECTED_AT
    )

    assert aggregated[0].matched_video_count == 2
    assert "keyword:description=fitness coach:2" in aggregated[0].match_reasons


def test_exclude_keyword_filters_the_hit_video_out():
    coach = creator(username="coach")
    videos = [
        video(platform_video_id="v1", description="fitness coach 招聘 中"),
        video(platform_video_id="v2", description="fitness coach routine"),
    ]

    aggregated = aggregate_creator_results(
        creators=[coach],
        videos=videos,
        keywords=("fitness coach",),
        exclude_keywords=("招聘",),
        now=COLLECTED_AT,
    )

    assert aggregated[0].matched_video_count == 1
    assert aggregated[0].matched_videos[0].platform_video_id == "v2"


def test_exclude_keyword_in_author_bio_drops_the_author():
    coach = creator(username="coach", bio="fitness coach 招聘")
    videos = [video(platform_video_id="v1", description="fitness coach routine")]

    aggregated = aggregate_creator_results(
        creators=[coach],
        videos=videos,
        keywords=("fitness coach",),
        exclude_keywords=("招聘",),
        now=COLLECTED_AT,
    )

    assert aggregated == ()


def test_keyword_match_is_unicode_casefold():
    coach = creator(username="coach")
    videos = [video(platform_video_id="v1", description="Wir sind in der Straße")]

    aggregated = aggregate_creator_results(
        creators=[coach], videos=videos, keywords=("strasse",), now=COLLECTED_AT
    )

    assert aggregated[0].matched_video_count == 1
    assert aggregated[0].matched_keywords == ("strasse",)


def test_author_bio_alone_can_match_a_keyword_but_counts_no_video():
    """A bio hit is author-level evidence: the row survives with zero videos."""
    coach = creator(username="coach", bio="Certified FITNESS COACH")
    videos = [video(platform_video_id="v1", description="morning routine")]

    aggregated = aggregate_creator_results(
        creators=[coach], videos=videos, keywords=("fitness coach",), now=COLLECTED_AT
    )

    assert len(aggregated) == 1
    assert aggregated[0].matched_video_count == 0
    assert aggregated[0].matched_videos == ()
    assert aggregated[0].matched_keywords == ("fitness coach",)
    assert "keyword:bio=fitness coach:1" in aggregated[0].match_reasons


def test_bio_hit_does_not_promote_unrelated_videos():
    """Regression: every video used to inherit the author's bio hit (bug I1)."""
    coach = creator(username="coach", bio="Certified fitness coach")
    videos = [
        video(platform_video_id="v1", description="morning routine", view_count=100),
        video(platform_video_id="v2", description="meal prep", view_count=200),
        video(platform_video_id="v3", description="q&a sunday", view_count=300),
    ]

    aggregated = aggregate_creator_results(
        creators=[coach], videos=videos, keywords=("fitness coach",), now=COLLECTED_AT
    )

    assert len(aggregated) == 1
    assert aggregated[0].matched_video_count == 0
    assert aggregated[0].total_view_count is None
    assert [reason for reason in aggregated[0].match_reasons if reason.startswith("keyword:")] == [
        "keyword:bio=fitness coach:1"
    ]


def test_bio_hit_does_not_inflate_totals_of_the_really_matching_video():
    coach = creator(username="coach", bio="Certified fitness coach")
    videos = [
        video(platform_video_id="v1", description="fitness coach tips", view_count=10),
        video(platform_video_id="v2", description="meal prep", view_count=200),
        video(platform_video_id="v3", description="q&a sunday", view_count=300),
    ]

    aggregated = aggregate_creator_results(
        creators=[coach], videos=videos, keywords=("fitness coach",), now=COLLECTED_AT
    )

    assert aggregated[0].matched_video_count == 1
    assert aggregated[0].total_view_count == 10
    assert "keyword:description=fitness coach:1" in aggregated[0].match_reasons
    assert "keyword:bio=fitness coach:1" in aggregated[0].match_reasons


def test_hashtags_are_compared_by_normalized_value():
    coach = creator(username="coach")
    videos = [video(platform_video_id="v1", description="leg day", hashtags=("#Fitness", "Gym-Tok"))]

    aggregated = aggregate_creator_results(
        creators=[coach], videos=videos, hashtags=("fitness",), now=COLLECTED_AT
    )

    assert aggregated[0].matched_video_count == 1
    assert aggregated[0].matched_hashtags == ("fitness",)
    assert "hashtag:hashtags=fitness:1" in aggregated[0].match_reasons


def test_hashtag_reason_carries_field_value_and_count():
    coach = creator(username="coach")
    videos = [
        video(platform_video_id="v1", hashtags=("#Fitness",)),
        video(platform_video_id="v2", hashtags=("fitness",)),
        video(platform_video_id="v3", hashtags=("yoga",)),
    ]

    aggregated = aggregate_creator_results(
        creators=[coach], videos=videos, hashtags=("fitness",), now=COLLECTED_AT
    )

    assert aggregated[0].matched_video_count == 2
    assert "hashtag:hashtags=fitness:2" in aggregated[0].match_reasons


def test_author_without_any_match_is_dropped():
    coach = creator(username="coach")
    other = creator(username="yoga", platform_user_id="u2")
    videos = [
        video(platform_video_id="v1", creator_id=other.id, description="yoga flow"),
    ]

    aggregated = aggregate_creator_results(
        creators=[coach, other], videos=videos, keywords=("fitness coach",), now=COLLECTED_AT
    )

    assert aggregated == ()


def test_missing_view_count_is_reported_as_not_available():
    coach = creator(username="coach")
    videos = [video(platform_video_id="v1", description="fitness coach tips", like_count=10)]

    aggregated = aggregate_creator_results(
        creators=[coach], videos=videos, keywords=("fitness coach",), now=COLLECTED_AT
    )

    assert "view_count_unavailable" in aggregated[0].unavailable_metrics
    assert "view_count_unavailable" in aggregated[0].match_reasons


def test_duplicate_author_records_collapse_into_one_row():
    coach = creator(id="tiktok:coach", platform_user_id="u1", username="coach")
    renamed = creator(id="tiktok:coach-renamed", platform_user_id="u1", username="coach-renamed")
    videos = [
        video(platform_video_id="v1", creator_id=coach.id, description="fitness coach tips"),
        video(platform_video_id="v2", creator_id=renamed.id, description="fitness coach routine"),
    ]

    aggregated = aggregate_creator_results(
        creators=[coach, renamed], videos=videos, keywords=("fitness coach",), now=COLLECTED_AT
    )

    assert len(aggregated) == 1
    assert aggregated[0].creator.platform_user_id == "u1"
    assert aggregated[0].matched_video_count == 2


def test_real_fixture_acceptance_one_row_per_author_and_hits_aggregated():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    parsed = parse_search_response(
        payload,
        keywords=("fitness coach",),
        session_id_hash=hash_session_id("offline-fixture-session"),
        collected_at=COLLECTED_AT,
    )

    # A second page observing the same authors with *different* videos.
    page_two = tuple(
        replace(v, id=f"{v.id}-p2", platform_video_id=f"{v.platform_video_id}-p2")
        for v in parsed.videos
    )

    aggregated = aggregate_creator_results(
        creators=parsed.creators + parsed.creators,
        videos=parsed.videos + page_two,
        keywords=("fitness coach", "fitness"),
        hashtags=("fitnesscoach",),
        now=COLLECTED_AT,
    )

    author_ids = [row.creator.platform_user_id for row in aggregated]
    assert author_ids
    assert len(author_ids) == len(set(author_ids))
    assert all(row.matched_video_count >= 1 for row in aggregated)
    assert any(row.matched_video_count >= 2 for row in aggregated)
    assert all(row.match_reasons for row in aggregated)
    assert sum(row.matched_video_count for row in aggregated) == sum(
        len(row.matched_videos) for row in aggregated
    )


def test_real_fixture_repeated_page_does_not_double_count_identical_videos():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    parsed = parse_search_response(
        payload,
        keywords=("fitness coach",),
        session_id_hash=hash_session_id("offline-fixture-session"),
        collected_at=COLLECTED_AT,
    )

    single = aggregate_creator_results(
        creators=parsed.creators, videos=parsed.videos, keywords=("fitness",), now=COLLECTED_AT
    )
    repeated = aggregate_creator_results(
        creators=parsed.creators,
        videos=parsed.videos + parsed.videos,
        keywords=("fitness",),
        now=COLLECTED_AT,
    )

    assert [row.creator.id for row in single] == [row.creator.id for row in repeated]
    assert [row.matched_video_count for row in single] == [
        row.matched_video_count for row in repeated
    ]
