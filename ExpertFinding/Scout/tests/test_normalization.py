"""Canonical identifier normalization (Task 2)."""

import hashlib

from app.domain.normalization import (
    canonical_profile_url,
    hash_session_id,
    normalize_hashtag,
    normalize_keyword,
    normalize_username,
)


def test_username_and_profile_url_have_one_canonical_form():
    assert normalize_username("@Creator_A") == "creator_a"
    assert normalize_username("https://www.tiktok.com/@Creator_A?lang=en") == "creator_a"
    assert canonical_profile_url("Creator_A") == "https://www.tiktok.com/@creator_a"


def test_hashtag_removes_hash_and_casefolds():
    assert normalize_hashtag("#Fitness") == "fitness"
    assert normalize_hashtag(" fitness ") == "fitness"


def test_username_strips_at_prefix_and_surrounding_noise():
    assert normalize_username("  @Coach.Blue ") == "coach.blue"
    assert normalize_username("@Coach.Blue") == normalize_username("Coach.Blue")
    assert normalize_username("https://www.tiktok.com/@coach.blue/video/123") == "coach.blue"


def test_empty_or_non_string_username_is_empty_string():
    assert normalize_username("") == ""
    assert normalize_username("   ") == ""
    assert normalize_username("@") == ""
    assert normalize_username(None) == ""


def test_canonical_profile_url_is_idempotent():
    once = canonical_profile_url("@Creator_A")
    assert once == "https://www.tiktok.com/@creator_a"
    assert canonical_profile_url(once) == once
    assert canonical_profile_url("https://www.tiktok.com/@Creator_A?lang=en") == once


def test_keyword_is_whitespace_collapsed_and_casefolded():
    assert normalize_keyword("  Fitness   Coach ") == "fitness coach"
    assert normalize_keyword("FITNESS") == "fitness"


def test_keyword_rejects_empty_input():
    assert normalize_keyword("") == ""
    assert normalize_keyword("   ") == ""


def test_hashtag_keeps_dots_underscores_and_digits():
    assert normalize_hashtag("#Gym_Tok2024") == "gym_tok2024"
    assert normalize_hashtag("##Fitness") == "fitness"
    assert normalize_hashtag("") == ""


def test_session_id_hash_is_irreversible_and_deterministic():
    raw = "session-abc-123"
    hashed = hash_session_id(raw)

    assert hashed.startswith("sha256:")
    digest = hashed.removeprefix("sha256:")
    assert digest == hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert raw not in hashed
    assert hash_session_id(raw) == hashed
    assert hash_session_id("session-abc-124") != hashed
