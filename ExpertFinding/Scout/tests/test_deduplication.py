"""Creator/video deduplication behaviour.

Fixtures are defined locally (shared conftest is frozen for other tasks).
"""

from __future__ import annotations

from app.domain.deduplication import (
    creator_id_merge_map,
    creator_identity_key,
    creator_identity_keys,
    deduplicate_creators,
    deduplicate_videos,
)
from app.domain.models import Creator, Video
from app.domain.normalization import canonical_profile_url

COLLECTED_AT = "2026-10-02T00:00:00+00:00"


def creator(
    *,
    id: str | None = None,
    platform_user_id: str | None = None,
    username: str = "coach",
    display_name: str | None = None,
    bio: str | None = None,
    profile_url: str | None = None,
    follower_count: int | None = None,
    likes_count: int | None = None,
    video_count: int | None = None,
    first_seen_at: str = COLLECTED_AT,
    last_seen_at: str = COLLECTED_AT,
) -> Creator:
    return Creator(
        id=id or f"tiktok:{username or 'unknown'}",
        platform="tiktok",
        platform_user_id=platform_user_id,
        username=username,
        display_name=display_name,
        bio=bio,
        profile_url=profile_url if profile_url is not None else canonical_profile_url(username),
        avatar_url=None,
        region_code=None,
        language=None,
        is_verified=None,
        follower_count=follower_count,
        following_count=None,
        likes_count=likes_count,
        video_count=video_count,
        first_seen_at=first_seen_at,
        last_seen_at=last_seen_at,
    )


def video(
    *,
    id: str | None = None,
    platform_video_id: str = "v1",
    creator_id: str = "tiktok:coach",
    description: str | None = None,
    view_count: int | None = None,
    like_count: int | None = None,
    comment_count: int | None = None,
    share_count: int | None = None,
) -> Video:
    return Video(
        id=id or f"tiktok:{platform_video_id}",
        platform_video_id=platform_video_id,
        creator_id=creator_id,
        description=description,
        hashtags=(),
        create_time=None,
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


def test_creator_dedup_prefers_stable_platform_id_then_username():
    creators = [
        creator(platform_user_id="u1", username="coach"),
        creator(platform_user_id="u1", username="coach-renamed"),
        creator(platform_user_id=None, username="@COACH"),
    ]

    result = deduplicate_creators(creators)

    assert len(result) == 1
    assert result[0].platform_user_id == "u1"


def test_dedup_falls_back_to_canonical_profile_url_when_no_platform_id():
    creators = [
        creator(
            platform_user_id=None,
            username="Coach_One",
            profile_url="https://www.tiktok.com/@coach_one?lang=en",
        ),
        creator(platform_user_id=None, username="COACH_ONE!"),
    ]

    result = deduplicate_creators(creators)

    assert len(result) == 1
    assert creator_identity_key(result[0]).startswith("profile_url:")


def test_display_name_alone_is_never_an_identity_key():
    creators = [
        creator(username="alice", display_name="Fit Coach"),
        creator(username="bob", display_name="Fit Coach"),
    ]

    assert len(deduplicate_creators(creators)) == 2


def test_different_platform_ids_with_different_handles_stay_separate():
    creators = [
        creator(platform_user_id="u1", username="coach"),
        creator(platform_user_id="u2", username="coach-two"),
    ]

    assert len(deduplicate_creators(creators)) == 2


def test_same_username_with_different_platform_user_ids_stay_two_authors():
    """A shared handle must not merge two distinct stable ids (regression: C1)."""
    creators = [
        creator(id="tiktok:a", platform_user_id="u1", username="coach"),
        creator(id="tiktok:b", platform_user_id="u2", username="coach"),
    ]

    result = deduplicate_creators(creators)

    assert len(result) == 2
    assert {row.platform_user_id for row in result} == {"u1", "u2"}


def test_same_platform_user_id_merges_across_renamed_handles():
    creators = [
        creator(id="tiktok:a", platform_user_id="u1", username="coach"),
        creator(id="tiktok:b", platform_user_id="u1", username="coach-renamed"),
    ]

    result = deduplicate_creators(creators)

    assert len(result) == 1
    assert result[0].platform_user_id == "u1"


def test_same_username_merges_when_neither_record_has_a_platform_user_id():
    creators = [
        creator(id="tiktok:a", platform_user_id=None, username="coach"),
        creator(id="tiktok:b", platform_user_id=None, username="@COACH"),
    ]

    result = deduplicate_creators(creators)

    assert len(result) == 1
    assert creator_identity_key(result[0]).startswith("profile_url:")


def test_same_canonical_profile_url_merges_when_neither_record_has_a_platform_user_id():
    creators = [
        creator(
            id="tiktok:a",
            platform_user_id=None,
            username="Coach_One",
            profile_url="https://www.tiktok.com/@coach_one?lang=en",
        ),
        creator(
            id="tiktok:b",
            platform_user_id=None,
            username="coach-one-official",
            profile_url="http://tiktok.com/@COACH_ONE",
        ),
    ]

    result = deduplicate_creators(creators)

    assert len(result) == 1
    assert creator_identity_key(result[0]) == (
        f"profile_url:{canonical_profile_url('coach_one')}"
    )


def test_a_record_without_id_cannot_bridge_two_platform_user_ids():
    """Transitive union-find must not resurrect the C1 merge."""
    creators = [
        creator(id="tiktok:a", platform_user_id="u1", username="coach"),
        creator(id="tiktok:b", platform_user_id="u2", username="coach"),
        creator(id="tiktok:c", platform_user_id=None, username="coach"),
    ]

    result = deduplicate_creators(creators)

    assert len(result) == 2
    assert {row.platform_user_id for row in result} == {"u1", "u2"}


def test_merge_keeps_most_complete_record_and_fills_missing_fields():
    sparse = creator(username="coach", first_seen_at="2026-10-02T00:00:00+00:00")
    rich = creator(
        username="coach",
        bio="online fitness coach",
        follower_count=1200,
        first_seen_at="2026-09-01T00:00:00+00:00",
        last_seen_at="2026-10-01T00:00:00+00:00",
    )

    merged = deduplicate_creators([sparse, rich])

    assert len(merged) == 1
    assert merged[0].bio == "online fitness coach"
    assert merged[0].follower_count == 1200
    assert merged[0].first_seen_at == "2026-09-01T00:00:00+00:00"
    assert merged[0].last_seen_at == "2026-10-02T00:00:00+00:00"


def test_identity_keys_are_ordered_by_strength():
    keys = creator_identity_keys(creator(platform_user_id="u1", username="coach"))

    assert keys[0].startswith("platform_user_id:")
    assert any(key.startswith("profile_url:") for key in keys)
    assert any(key.startswith("username:") for key in keys)


def test_creator_id_merge_map_points_duplicate_ids_at_the_canonical_id():
    first = creator(id="tiktok:coach", platform_user_id="u1", username="coach")
    renamed = creator(id="tiktok:coach-renamed", platform_user_id="u1", username="coach-renamed")

    mapping = creator_id_merge_map([first, renamed])

    assert mapping["tiktok:coach"] == "tiktok:coach"
    assert mapping["tiktok:coach-renamed"] == "tiktok:coach"


def test_creators_without_any_identity_are_not_merged():
    anonymous_a = creator(id="tiktok:anon-a", username="")
    anonymous_b = creator(id="tiktok:anon-b", username="")

    assert len(deduplicate_creators([anonymous_a, anonymous_b])) == 2


def test_video_dedup_by_platform_video_id_and_missing_counts_are_filled():
    videos = [
        video(platform_video_id="v1", view_count=100, like_count=None),
        video(id="tiktok:v1-page2", platform_video_id="v1", view_count=None, like_count=5),
        video(platform_video_id="v2"),
    ]

    result = deduplicate_videos(videos)

    assert len(result) == 2
    assert result[0].platform_video_id == "v1"
    assert result[0].view_count == 100
    assert result[0].like_count == 5
