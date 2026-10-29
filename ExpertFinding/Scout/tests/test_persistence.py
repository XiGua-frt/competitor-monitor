"""Task 4 — SQLite persistence contract (offline, TDD).

Covers: restart recovery, sensitive-column exclusion, cache expiry, API audit,
transactional ``save_search``, creator cascade deletion and session purging.

The ``parsed_search`` fixture is defined here on purpose (Task 4 owns it): it
produces synthetic domain objects shaped exactly like
:func:`app.domain.parser.parse_search_response` output, so the persistence
layer is exercised without touching ``tests/fixtures/`` or ``conftest.py``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from app.domain.models import (
    ENTITY_TYPE_CREATOR,
    ENTITY_TYPE_VIDEO,
    PLATFORM_TIKTOK,
    Creator,
    ParsedSearch,
    Provenance,
    Video,
)
from app.domain.normalization import hash_session_id
from app.domain.parser import parse_search_response
from app.governance.deletion import SessionRevokedError, assert_session_usable
from app.governance.provenance import (
    SENSITIVE_FIELD_MARKERS,
    ProvenanceError,
    canonical_payload_hash,
    find_sensitive_fields,
    payload_hash_matches,
    verify_provenance,
)
from app.storage.repositories import Repository
from app.storage.sqlite import EXPECTED_TABLES, table_names

COLLECTED_AT = "2026-10-02T00:00:00+00:00"
SESSION_ID_HASH = hash_session_id("test-session")
OTHER_SESSION_ID_HASH = hash_session_id("other-session")

SOURCE = "tiktok_web"
SOURCE_SCOPE = "public_search_result"

FIXTURE = Path(__file__).parent / "fixtures/search_general_fitness_coach.json"


def make_creator(username="fitcoach", creator_id=None, **overrides):
    base = dict(
        id=creator_id or f"{PLATFORM_TIKTOK}:{username}",
        platform=PLATFORM_TIKTOK,
        platform_user_id="1000001",
        username=username,
        display_name="Fit Coach",
        bio=None,
        profile_url=f"https://www.tiktok.com/@{username}",
        avatar_url=None,
        region_code="US",
        language="en",
        is_verified=True,
        follower_count=1200,
        following_count=10,
        likes_count=500,
        video_count=30,
        first_seen_at=COLLECTED_AT,
        last_seen_at=COLLECTED_AT,
    )
    base.update(overrides)
    return Creator(**base)


def make_video(video_id="7001", creator_id=f"{PLATFORM_TIKTOK}:fitcoach", **overrides):
    base = dict(
        id=f"{PLATFORM_TIKTOK}:{video_id}",
        platform_video_id=video_id,
        creator_id=creator_id,
        description="morning workout",
        hashtags=("fitness", "coach"),
        create_time=None,
        region_code="US",
        view_count=1000,
        like_count=50,
        comment_count=2,
        share_count=1,
        video_url="https://www.tiktok.com/@fitcoach/video/7001",
        collected_at=COLLECTED_AT,
        source=SOURCE,
        source_scope=SOURCE_SCOPE,
        session_id_hash=SESSION_ID_HASH,
    )
    base.update(overrides)
    return Video(**base)


def make_provenance(entity_type, entity_id, payload):
    digest = canonical_payload_hash(payload)
    return Provenance(
        id=f"{entity_type}:{entity_id}:{digest[:16]}",
        entity_type=entity_type,
        entity_id=entity_id,
        source=SOURCE,
        source_scope=SOURCE_SCOPE,
        keywords=("fitness coach",),
        collected_at=COLLECTED_AT,
        session_id_hash=SESSION_ID_HASH,
        raw_payload_hash=digest,
    )


@pytest.fixture
def parsed_search():
    """Two creators, three videos and their provenance records."""
    coach = make_creator("fitcoach")
    runner = make_creator(
        "runnerly",
        platform_user_id="1000002",
        display_name="Runner Ly",
        follower_count=None,
        following_count=None,
        likes_count=None,
        video_count=None,
        is_verified=False,
        language=None,
        region_code=None,
    )

    videos = (
        make_video("7001", coach.id),
        make_video("7002", coach.id, view_count=None, like_count=None),
        make_video("7003", runner.id),
    )
    provenances = tuple(
        make_provenance(ENTITY_TYPE_CREATOR, creator.id, {"username": creator.username})
        for creator in (coach, runner)
    ) + tuple(
        make_provenance(ENTITY_TYPE_VIDEO, video.id, {"video_id": video.platform_video_id})
        for video in videos
    )

    return ParsedSearch(creators=(coach, runner), videos=videos, provenances=provenances)


@pytest.fixture
def repo(tmp_path):
    repository = Repository.connect(tmp_path / "scout.sqlite3")
    try:
        yield repository
    finally:
        repository.close()


def count_rows(connection, table):
    return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# --------------------------------------------------------------------------
# Step 1: restart recovery and sensitive-field exclusion
# --------------------------------------------------------------------------


def test_sqlite_round_trip_survives_reopen(tmp_path, parsed_search):
    db_path = tmp_path / "scout.sqlite3"
    repo = Repository.connect(db_path)
    repo.save_search(parsed_search, request_id="req_test")
    repo.close()

    reopened = Repository.connect(db_path)
    try:
        result = reopened.get_search("req_test")
        assert len(result.creators) >= 1
        assert result.provenance[0].source == "tiktok_web"
        assert "sessionid" not in reopened.dump_table_names_and_columns()
        assert "msToken" not in reopened.dump_table_names_and_columns()
    finally:
        reopened.close()


def test_round_trip_preserves_domain_values(tmp_path, parsed_search):
    db_path = tmp_path / "scout.sqlite3"
    repo = Repository.connect(db_path)
    repo.save_search(parsed_search, request_id="req_fields")
    repo.close()

    reopened = Repository.connect(db_path)
    try:
        result = reopened.get_search("req_fields")
    finally:
        reopened.close()

    coach = next(item for item in result.creators if item.username == "fitcoach")
    assert coach.id == f"{PLATFORM_TIKTOK}:fitcoach"
    assert coach.platform_user_id == "1000001"
    assert coach.follower_count == 1200
    assert coach.is_verified is True
    assert coach.deleted_at is None

    # A creator with missing upstream figures must come back as None, not 0.
    runner = next(item for item in result.creators if item.username == "runnerly")
    assert runner.follower_count is None
    assert runner.video_count is None
    assert runner.is_verified is False

    video = next(item for item in result.videos if item.platform_video_id == "7002")
    assert video.hashtags == ("fitness", "coach")
    assert video.view_count is None
    assert video.create_time is None
    assert video.session_id_hash == SESSION_ID_HASH

    provenance = result.provenance[0]
    assert provenance.keywords == ("fitness coach",)
    assert provenance.raw_payload_hash


def test_all_expected_tables_exist(repo):
    assert EXPECTED_TABLES <= set(table_names(repo.connection))


def test_no_sensitive_columns_anywhere(repo):
    dump = repo.dump_table_names_and_columns()
    for marker in SENSITIVE_FIELD_MARKERS + ("cookies_json", "storage_state", "msToken", "sessionid"):
        assert marker not in dump


def test_foreign_keys_are_enforced(repo):
    with pytest.raises(sqlite3.IntegrityError):
        repo.connection.execute(
            "INSERT INTO video (id, platform_video_id, creator_id, hashtags_json,"
            " collected_at, source, source_scope, session_id_hash, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("tiktok:orphan", "orphan", "tiktok:missing", "[]", COLLECTED_AT,
             SOURCE, SOURCE_SCOPE, SESSION_ID_HASH, COLLECTED_AT),
        )


def test_creator_username_is_unique_per_platform(repo, parsed_search):
    duplicate = replace(parsed_search.creators[0], id=f"{PLATFORM_TIKTOK}:other-id")
    repo.save_search(parsed_search, request_id="req_dup")
    with pytest.raises(sqlite3.IntegrityError):
        repo.connection.execute(
            "INSERT INTO creator (id, platform, username, profile_url, first_seen_at,"
            " last_seen_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (duplicate.id, PLATFORM_TIKTOK, duplicate.username, duplicate.profile_url,
             COLLECTED_AT, COLLECTED_AT, COLLECTED_AT, COLLECTED_AT),
        )


# --------------------------------------------------------------------------
# Transactional save_search
# --------------------------------------------------------------------------


def test_save_search_upserts_idempotently(repo, parsed_search):
    repo.save_search(parsed_search, request_id="req_up")
    repo.save_search(parsed_search, request_id="req_up")

    assert count_rows(repo.connection, "creator") == len(parsed_search.creators)
    assert count_rows(repo.connection, "video") == len(parsed_search.videos)
    assert len(repo.get_search("req_up").creators) == len(parsed_search.creators)


def test_save_search_is_atomic(repo, parsed_search):
    """失败必须发生在事务**内**：前一批合法数据先落库，第二批中途撞 UNIQUE 后整批回滚。

    这里刻意不让前置校验替事务"挡枪"：第二批的第一个作者是完全合法的（会被真的
    INSERT 进去），第二个作者才撞上 ``UNIQUE (platform, username)``。因此
    "新作者行最终不存在"只能由回滚解释 —— 把 ``with connection:`` 去掉就会失败。
    """
    repo.save_search(parsed_search, request_id="req_ok")
    before = {
        table: count_rows(repo.connection, table)
        for table in ("creator", "video", "provenance", "search_result")
    }
    assert before["creator"] == len(parsed_search.creators)

    newcomer = make_creator("newbie", platform_user_id="1000009")
    clash = replace(parsed_search.creators[0], id=f"{PLATFORM_TIKTOK}:clash-id")
    broken_video = make_video("9001", newcomer.id)
    broken = ParsedSearch(
        creators=(newcomer, clash),
        videos=(broken_video,),
        provenances=(
            make_provenance(ENTITY_TYPE_CREATOR, newcomer.id, {"username": newcomer.username}),
            make_provenance(ENTITY_TYPE_CREATOR, clash.id, {"username": clash.username}),
            make_provenance(ENTITY_TYPE_VIDEO, broken_video.id, {"video_id": "9001"}),
        ),
    )

    executed: list[str] = []
    repo.connection.set_trace_callback(executed.append)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            repo.save_search(broken, request_id="req_broken")
    finally:
        repo.connection.set_trace_callback(None)

    # 证据一：写入语句确实执行过（newcomer 的 INSERT 已经进了事务）。
    assert any("INSERT INTO creator" in statement for statement in executed)
    # 证据二：它没能留下 —— 事务已回滚，连接回到无事务状态。
    assert not repo.connection.in_transaction
    assert repo.connection.execute(
        "SELECT COUNT(*) FROM creator WHERE id = ?", (newcomer.id,)
    ).fetchone()[0] == 0
    assert count_rows(repo.connection, "creator") == before["creator"]
    assert count_rows(repo.connection, "video") == before["video"]
    assert count_rows(repo.connection, "provenance") == before["provenance"]
    assert count_rows(repo.connection, "search_result") == before["search_result"]
    assert repo.get_search("req_broken") is None


def test_save_search_records_search_result_per_creator(repo, parsed_search):
    repo.save_search(parsed_search, request_id="req_rows")

    rows = repo.connection.execute(
        "SELECT creator_id, score_version FROM search_result WHERE request_id = ?"
        " ORDER BY creator_id",
        ("req_rows",),
    ).fetchall()
    assert [row["creator_id"] for row in rows] == sorted(
        creator.id for creator in parsed_search.creators
    )
    assert all(row["score_version"] for row in rows)


def test_get_search_missing_request_is_none(repo):
    assert repo.get_search("req_unknown") is None


def test_real_fixture_round_trips_through_parser_and_storage(tmp_path):
    """Real Task-2 parser output must survive storage unchanged (no remapping)."""
    response = json.loads(FIXTURE.read_text(encoding="utf-8"))
    parsed = parse_search_response(
        response,
        keywords=("fitness coach",),
        session_id_hash=SESSION_ID_HASH,
        collected_at=COLLECTED_AT,
    )

    db_path = tmp_path / "fixture.sqlite3"
    repo = Repository.connect(db_path)
    repo.save_search(parsed, request_id="req_fixture")
    repo.close()

    reopened = Repository.connect(db_path)
    result = reopened.get_search("req_fixture")
    reopened.close()

    assert len(result.creators) == len(parsed.creators)
    assert len(result.videos) == len(parsed.videos)
    assert len(result.provenance) == len(parsed.provenances)
    # Source labels are stored verbatim: what the parser produced is what we return.
    assert {item.source for item in result.provenance} == {
        item.source for item in parsed.provenances
    }


def test_sensitive_write_attempt_is_rejected(repo):
    with pytest.raises(ValueError):
        repo.put_cached("k", json.dumps({"cookies_json": {"sessionid": "x"}}),
                        collected_at=COLLECTED_AT, expires_at="2999-01-01T00:00:00+00:00")


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------


def test_cache_hit_before_expiry(repo):
    repo.put_cached("k1", '{"status_code":0}', COLLECTED_AT, "2026-10-02T01:00:00+00:00")

    entry = repo.get_cached("k1", now="2026-10-02T00:30:00+00:00")
    assert entry is not None
    assert entry.response_json == '{"status_code":0}'
    assert entry.expires_at == "2026-10-02T01:00:00+00:00"


def test_cache_expired_entry_is_purged(repo):
    repo.put_cached("k2", '{"status_code":0}', COLLECTED_AT, "2026-10-02T01:00:00+00:00")

    assert repo.get_cached("k2", now="2026-10-02T02:00:00+00:00") is None
    assert count_rows(repo.connection, "request_cache") == 0


def test_cache_miss_returns_none(repo):
    assert repo.get_cached("missing", now=COLLECTED_AT) is None


def test_save_search_can_seed_the_cache(repo, parsed_search):
    repo.save_search(parsed_search, request_id="req_cache", cache_key="keyword:fitness coach")
    assert repo.get_cached("keyword:fitness coach", now=COLLECTED_AT) is not None


# --------------------------------------------------------------------------
# API audit
# --------------------------------------------------------------------------


def test_audit_survives_reopen(tmp_path):
    db_path = tmp_path / "audit.sqlite3"
    repo = Repository.connect(db_path)
    repo.record_audit("req_audit", "search.discovery", "ok")
    repo.record_audit("req_denied", "search.discovery", "denied", detail="bad api key")
    repo.close()

    reopened = Repository.connect(db_path)
    entries = {row["request_id"]: row for row in reopened.list_audit()}
    reopened.close()

    assert entries["req_audit"]["operation"] == "search.discovery"
    assert entries["req_audit"]["outcome"] == "ok"
    assert entries["req_audit"]["created_at"]
    assert entries["req_denied"]["outcome"] == "denied"


# --------------------------------------------------------------------------
# Step 5: cascade deletion and session purging
# --------------------------------------------------------------------------


def test_delete_creator_cascades_videos_provenance_and_search_results(repo, parsed_search):
    repo.save_search(parsed_search, request_id="req_del")

    report = repo.delete_creator(parsed_search.creators[0].id)

    assert report.creators == 1
    assert report.videos == 2
    assert count_rows(repo.connection, "creator") == 1
    assert count_rows(repo.connection, "video") == 1
    assert count_rows(repo.connection, "search_result") == 1
    remaining = repo.connection.execute(
        "SELECT COUNT(*) FROM provenance WHERE entity_id = ?",
        (parsed_search.creators[0].id,),
    ).fetchone()[0]
    assert remaining == 0

    result = repo.get_search("req_del")
    assert [creator.username for creator in result.creators] == ["runnerly"]
    assert repo.delete_creator("tiktok:unknown").creators == 0


def test_purge_session_clears_cache_and_source_records(repo, parsed_search):
    other_video = make_video("8001", f"{PLATFORM_TIKTOK}:fitcoach", session_id_hash=OTHER_SESSION_ID_HASH)
    repo.save_search(parsed_search, request_id="req_purge")
    repo.connection.execute(
        "UPDATE video SET session_id_hash = ? WHERE platform_video_id = ?",
        (OTHER_SESSION_ID_HASH, "7003"),
    )
    repo.connection.commit()
    repo.put_cached("keep-me", "{}", COLLECTED_AT, "2999-01-01T00:00:00+00:00",
                    session_id_hash=OTHER_SESSION_ID_HASH)
    repo.put_cached("drop-me", "{}", COLLECTED_AT, "2999-01-01T00:00:00+00:00",
                    session_id_hash=SESSION_ID_HASH)
    repo.upsert_session_meta("session-a", SESSION_ID_HASH)
    assert other_video.id  # keep the synthetic video referenced for readability

    report = repo.purge_session(SESSION_ID_HASH)

    assert report.cache_rows >= 1
    assert report.provenance_rows == len(parsed_search.provenances)
    assert repo.get_cached("drop-me", now=COLLECTED_AT) is None
    assert repo.get_cached("keep-me", now=COLLECTED_AT) is not None
    assert count_rows(repo.connection, "provenance") == 0
    assert count_rows(repo.connection, "video") == 1

    meta = repo.connection.execute(
        "SELECT status, revoked_at FROM session_meta WHERE session_id_hash = ?",
        (SESSION_ID_HASH,),
    ).fetchone()
    assert meta["status"] == "revoked"
    assert meta["revoked_at"]


def test_revoked_session_refuses_new_requests(repo, parsed_search):
    repo.save_search(parsed_search, request_id="req_revoke")
    repo.upsert_session_meta("session-a", SESSION_ID_HASH)
    assert assert_session_usable(repo, SESSION_ID_HASH) is None

    repo.purge_session(SESSION_ID_HASH)

    with pytest.raises(SessionRevokedError):
        assert_session_usable(repo, SESSION_ID_HASH)


def test_purge_session_closes_gate_for_unregistered_hash(repo):
    """从未登记的指纹被撤回后，闸门同样必须关闭（tombstone 兜住 fail-open）。"""
    assert assert_session_usable(repo, OTHER_SESSION_ID_HASH) is None

    report = repo.purge_session(OTHER_SESSION_ID_HASH)

    assert report.sessions == 1
    with pytest.raises(SessionRevokedError):
        assert_session_usable(repo, OTHER_SESSION_ID_HASH)

    tombstone = repo.connection.execute(
        "SELECT session_id, status, revoked_at FROM session_meta WHERE session_id_hash = ?",
        (OTHER_SESSION_ID_HASH,),
    ).fetchone()
    assert tombstone["status"] == "revoked"
    assert tombstone["revoked_at"]


# --------------------------------------------------------------------------
# 撤回闸门对写路径同样生效
# --------------------------------------------------------------------------


def test_put_cached_refuses_unregistered_but_revoked_session(repo):
    repo.purge_session(SESSION_ID_HASH)

    with pytest.raises(SessionRevokedError):
        repo.put_cached("drop", "{}", COLLECTED_AT, "2999-01-01T00:00:00+00:00",
                        session_id_hash=SESSION_ID_HASH)

    assert count_rows(repo.connection, "request_cache") == 0


def test_put_cached_refuses_registered_and_revoked_session(repo):
    repo.upsert_session_meta("session-a", SESSION_ID_HASH)
    repo.purge_session(SESSION_ID_HASH)

    with pytest.raises(SessionRevokedError):
        repo.put_cached("drop", "{}", COLLECTED_AT, "2999-01-01T00:00:00+00:00",
                        session_id_hash=SESSION_ID_HASH)

    assert count_rows(repo.connection, "request_cache") == 0
    # 其它未被撤回的会话不受影响。
    repo.put_cached("keep", "{}", COLLECTED_AT, "2999-01-01T00:00:00+00:00",
                    session_id_hash=OTHER_SESSION_ID_HASH)
    assert count_rows(repo.connection, "request_cache") == 1


def test_save_search_refuses_revoked_session(repo, parsed_search):
    repo.purge_session(SESSION_ID_HASH)

    with pytest.raises(SessionRevokedError):
        repo.save_search(parsed_search, request_id="req_revoked")

    assert count_rows(repo.connection, "creator") == 0
    assert count_rows(repo.connection, "video") == 0
    assert count_rows(repo.connection, "provenance") == 0
    assert count_rows(repo.connection, "search_result") == 0
    assert count_rows(repo.connection, "request_cache") == 0
    assert repo.get_search("req_revoked") is None


# --------------------------------------------------------------------------
# 敏感闸门：cookie 形态与非单点防线
# --------------------------------------------------------------------------


def test_cookie_markers_block_cache_writes(repo):
    # 值里刻意不带 sessionid 等其它标记：命中的只能是 cookie 形态本身。
    for payload in (
        '{"headers": {"Set-Cookie": "abcd=efgh"}}',
        '{"headers": {"set-cookie": "abcd=efgh"}}',
        '{"headers": {"set_cookie": "abcd=efgh"}}',
        '{"cookie": "abcd=efgh"}',
        '{"Cookie": "abcd=efgh"}',
        '{"cookies": ["abcd=efgh"]}',
        '{"cookiejar": ["abcd=efgh"]}',
    ):
        with pytest.raises(ProvenanceError):
            repo.put_cached("k", payload, COLLECTED_AT, "2999-01-01T00:00:00+00:00")

    assert count_rows(repo.connection, "request_cache") == 0


def test_cookie_marker_does_not_flag_ordinary_words(repo):
    """词边界取舍的证据：cookiequeen / cookieless 这类普通词不该被误杀。"""
    payload = json.dumps({"creator_ids": ["tiktok:cookiequeen"], "note": "cookieless"})

    assert find_sensitive_fields(payload) == ()
    repo.put_cached("k", payload, COLLECTED_AT, "2999-01-01T00:00:00+00:00")

    assert repo.get_cached("k", now=COLLECTED_AT) is not None


def test_save_search_validates_derived_cache_payload(tmp_path):
    """save_search 在写 request_cache 之前也校验一遍内容，不靠 put_cached 单点。"""
    creator = make_creator("cookie")
    video = make_video("9101", creator.id)
    parsed = ParsedSearch(
        creators=(creator,),
        videos=(video,),
        provenances=(
            make_provenance(ENTITY_TYPE_CREATOR, creator.id, {"username": creator.username}),
            make_provenance(ENTITY_TYPE_VIDEO, video.id, {"video_id": video.platform_video_id}),
        ),
    )

    repository = Repository.connect(tmp_path / "cookie.sqlite3")
    try:
        with pytest.raises(ProvenanceError):
            repository.save_search(parsed, request_id="req_cookie", cache_key="keyword:cookie")

        # 缓存一行都没写；业务行已提交（缓存播种失败不影响采集结果落库）。
        assert count_rows(repository.connection, "request_cache") == 0
        assert count_rows(repository.connection, "creator") == 1
    finally:
        repository.close()


# --------------------------------------------------------------------------
# governance helpers
# --------------------------------------------------------------------------


def test_provenance_verification_round_trip(parsed_search):
    provenance = parsed_search.provenances[0]

    assert verify_provenance(provenance)
    assert not verify_provenance(replace(provenance, session_id_hash="plaintext-session"))
    assert not verify_provenance(replace(provenance, raw_payload_hash="short"))
    assert payload_hash_matches(provenance, {"username": "fitcoach"})
    assert not payload_hash_matches(provenance, {"username": "someone-else"})
