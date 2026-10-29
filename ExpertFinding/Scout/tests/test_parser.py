"""Search response -> domain model parsing contract (Task 1 + Task 2)."""

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from app.domain.normalization import hash_session_id
from app.domain.parser import parse_search_response

FIXTURE = Path(__file__).parent / "fixtures/search_general_fitness_coach.json"

COLLECTED_AT = "2026-10-02T00:00:00+00:00"
SESSION_ID_HASH = hash_session_id("test-session")


def _load_fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _parse(response, **kwargs):
    options = {
        "keywords": ("fitness coach",),
        "session_id_hash": SESSION_ID_HASH,
        "collected_at": COLLECTED_AT,
    }
    options.update(kwargs)
    return parse_search_response(response, **options)


def test_fixture_parses_video_records_and_ignores_user_card_as_video():
    response = _load_fixture()
    parsed = _parse(response)

    assert len(parsed.videos) == 13
    assert len(parsed.creators) >= 10
    assert all(video.platform_video_id for video in parsed.videos)
    assert all(creator.platform == "tiktok" for creator in parsed.creators)


def test_parser_preserves_missing_fields_as_none_not_zero():
    response = {"status_code": 0, "data": [{"type": "item", "item": {
        "id": "v1", "desc": "fitness", "author": {"unique_id": "coach"},
        "stats": {},
    }}]}
    parsed = _parse(response, keywords=("fitness",))

    video = parsed.videos[0]
    assert video.view_count is None
    assert video.like_count is None
    assert video.comment_count is None


def test_user_list_card_contributes_no_video_and_no_creator():
    response = _load_fixture()
    parsed = _parse(response)

    user_list_usernames = set()
    for record in response["data"]:
        for entry in record.get("user_list") or []:
            info = entry.get("user_info") or {}
            if info.get("unique_id"):
                user_list_usernames.add(info["unique_id"].lower())

    assert user_list_usernames

    video_authors = {video.creator_id for video in parsed.videos}
    parsed_usernames = {creator.username for creator in parsed.creators}

    # `coach.bluee` legitimately appears both in the user card and in a video,
    # so only the user-card-exclusive accounts may be absent.
    card_only = user_list_usernames - parsed_usernames
    assert card_only, "expected at least one user-card-only account in the fixture"
    assert not (card_only & parsed_usernames)
    assert len(video_authors) == len(parsed.videos)


def test_creator_stats_come_from_author_stats_only():
    parsed = _parse(_load_fixture())

    by_username = {creator.username: creator for creator in parsed.creators}
    assert "mike_thurston" in by_username
    mike = by_username["mike_thurston"]
    assert mike.follower_count == 591100
    assert mike.is_verified is True
    assert mike.profile_url == "https://www.tiktok.com/@mike_thurston"


def test_video_hashtags_are_extracted_and_normalized():
    parsed = _parse(_load_fixture())

    hashtags = {hashtag for video in parsed.videos for hashtag in video.hashtags}
    assert "fitnesscoach" in hashtags
    assert all(tag == tag.strip().lower() for tag in hashtags)
    assert all(not tag.startswith("#") for tag in hashtags)


def test_create_time_is_normalized_to_iso8601_string():
    parsed = _parse(_load_fixture())

    assert parsed.videos[0].create_time == "2025-03-15T03:06:27+00:00"


def test_domain_objects_never_expose_raw_payload_or_session_material():
    parsed = _parse(_load_fixture())

    forbidden = ("cookie", "mstoken", "storage", "raw_header", "signature")
    for obj in list(parsed.videos) + list(parsed.creators) + list(parsed.provenances):
        for field_name in asdict(obj):
            lowered = field_name.lower()
            assert not any(token in lowered for token in forbidden), field_name


def test_domain_values_never_carry_session_material():
    """Field names are not enough: the values themselves must stay clean.

    Only session material is asserted here, case-sensitively. ``x-signature`` is
    deliberately *not* in this list: 13/13 fixture ``Creator.avatar_url`` values
    are TikTok public CDN URLs whose query string carries the CDN's own
    ``x-signature=`` / ``refresh_token=`` / ``x-expires=`` parameters. Those are
    upstream CDN access parameters for an already public image, not local signing
    input, so they are exempt (see ``app.domain.parser._extract_url``).
    """
    parsed = _parse(_load_fixture())

    serialized = json.dumps(
        [asdict(obj) for obj in list(parsed.videos) + list(parsed.creators) + list(parsed.provenances)],
        ensure_ascii=False,
        default=str,
    )

    for token in ("msToken", "ttwid", "odin_tt", "sessionid", "sid_tt"):
        assert token not in serialized, f"session material {token!r} leaked into a domain value"


def test_provenance_records_source_keywords_and_payload_hash():
    parsed = _parse(_load_fixture())

    assert len(parsed.provenances) == len(parsed.videos) + len(parsed.creators)

    hashes = {prov.raw_payload_hash for prov in parsed.provenances}
    assert all(len(value) == 64 for value in hashes)
    assert all(all(char in "0123456789abcdef" for char in value) for value in hashes)

    video_provenance = [prov for prov in parsed.provenances if prov.entity_type == "video"]
    assert video_provenance
    assert video_provenance[0].keywords == ("fitness coach",)
    assert video_provenance[0].collected_at == COLLECTED_AT
    assert video_provenance[0].session_id_hash == SESSION_ID_HASH
    assert video_provenance[0].source == "tiktok_search_general_full"


def test_raw_payload_hash_is_stable_and_changes_with_payload():
    response = {"status_code": 0, "data": [{"type": "item", "item": {
        "id": "v1", "desc": "fitness", "author": {"unique_id": "coach"},
        "stats": {},
    }}]}

    first = _parse(response)
    second = _parse(json.loads(json.dumps(response)))
    assert first.provenances == second.provenances

    response["data"][0]["item"]["desc"] = "changed"
    assert _parse(response).provenances != first.provenances


def test_session_id_is_only_kept_as_irreversible_hash():
    parsed = _parse(_load_fixture(), session_id_hash="raw-session-value-123")

    for video in parsed.videos:
        assert video.session_id_hash.startswith("sha256:")
        assert "raw-session-value-123" not in json.dumps(asdict(video))


def test_unsupported_status_code_is_rejected():
    with pytest.raises(ValueError, match="status_code"):
        _parse({"status_code": 2151, "data": []})


def test_records_without_a_video_item_are_skipped():
    response = {
        "status_code": 0,
        "data": [
            {"type": 4, "user_list": [{"user_info": {"unique_id": "someone"}}]},
            {"type": "item", "item": "not-a-dict"},
            {"type": "item"},
            "not-a-record",
            {"type": "item", "item": {
                "id": "v1", "desc": "fitness", "author": {"unique_id": "coach"},
            }},
        ],
    }
    parsed = _parse(response)

    assert len(parsed.videos) == 1
    assert parsed.videos[0].platform_video_id == "v1"
    assert [creator.username for creator in parsed.creators] == ["coach"]
