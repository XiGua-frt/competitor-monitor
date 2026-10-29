"""Task 7 — discovery service 与离线同步搜索用例（不触网）。

fake transport 直接返回 ``tests/fixtures/search_general_fitness_coach.json``
的内容，repository 用真实 SQLite（tmp_path），不发任何网络请求、不读 ``.secrets/``。
所有 fixture（fake transport / repo / clock）就地定义，不改动 ``conftest.py``。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from app.discovery.service import (
    DAMAGING_STOP_STATUSES,
    DEFAULT_LIMIT,
    NORMAL_STOP_STATUSES,
    SCHEMA_VERSION,
    SIGNER_VERSION,
    DiscoveryService,
    SearchQuery,
)
from app.domain.normalization import hash_session_id
from app.governance.deletion import SessionRevokedError
from app.governance.provenance import SENSITIVE_FIELD_MARKERS, find_sensitive_fields
from app.storage.repositories import Repository
import app.transport.pagination as pagination_module
from app.transport.pagination import (
    COMPLETED,
    COOLING_STOPPED,
    PAGINATION_STALLED,
    PAGINATION_STOPPED,
    SearchRun,
)
from app.transport.public_readonly import PublicReadonlySearch
from app.transport.response_classifier import (
    DETAIL_WAF,
    RawReply,
    WafChallengeError,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "search_general_fitness_coach.json"

COLLECTED_AT = "2026-10-02T12:00:00+00:00"
SESSION_ID_HASH = hash_session_id("task7-test-session")
OTHER_SESSION_ID_HASH = hash_session_id("task7-other-session")

KEYWORD = "fitness coach"


# --------------------------------------------------------------------------- fakes


def load_fixture_payload() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def fixture_records() -> tuple[dict, ...]:
    """fixture 的 ``data``：13 条视频记录（type 1）+ 1 张作者卡（type 4）。"""
    return tuple(load_fixture_payload()["data"])


def terminal_page() -> dict:
    """末页：无记录、无 cursor，用于让分页策略自然结束。"""
    return dict(load_fixture_payload(), data=[], cursor=None, has_more=0)


def cookie_username_records() -> tuple[dict, ...]:
    """username 恰为整词 ``cookie``：派生缓存指纹会命中敏感闸门。

    这不是真的会话泄露（缓存里只有 id 列表），但整词标记会拒收，正好用来验证
    缓存播种失败时服务降级而不是整次搜索失败。
    """
    return (
        {
            "type": 1,
            "item": {
                "id": "7400000000000000001",
                "desc": "fitness coach 日常训练 #fitness",
                "createTime": 1759000000,
                "author": {
                    "id": "6950000000000000001",
                    "uniqueId": "cookie",
                    "nickname": "Cookie",
                },
                "stats": {"playCount": 12000, "diggCount": 300, "commentCount": 10,
                          "shareCount": 2},
                "authorStats": {"followerCount": 120000, "videoCount": 30},
            },
        },
    )


class FakeTransport:
    """``PublicReadonlySearch.search(keyword, *, max_results=None) -> SearchRun`` 的替身。"""

    def __init__(self, runs=None, errors=None, default_records=(), default_status=COMPLETED):
        self.runs: dict[str, SearchRun] = dict(runs or {})
        self.errors: dict[str, Exception] = dict(errors or {})
        self.default_records = tuple(default_records)
        self.default_status = default_status
        self.calls: list[tuple[str, int | None]] = []

    def search(self, keyword: str, *, max_results: int | None = None) -> SearchRun:
        self.calls.append((keyword, max_results))
        error = self.errors.get(keyword)
        if error is not None:
            raise error
        run = self.runs.get(keyword)
        if run is not None:
            return run
        return SearchRun(
            keyword=keyword,
            records=self.default_records,
            pages=1,
            status=self.default_status,
            detail="fake 返回 fixture 页面",
            attempts=1,
        )

    @property
    def call_count(self) -> int:
        return len(self.calls)


def completed_run(keyword: str = KEYWORD, records=()) -> SearchRun:
    return SearchRun(
        keyword=keyword,
        records=tuple(records or fixture_records()),
        pages=1,
        status=COMPLETED,
        detail="到达末页，数据完整",
        attempts=1,
    )


def upstream_status_constants() -> set[str]:
    """pagination.py **本模块定义**的状态常量 + ``SearchRun`` 的默认状态。

    用 AST 只取本文件顶层的大写字符串赋值：``vars(pagination)`` 会混入从
    ``response_classifier`` 导入的 outcome 种类（``SUCCESS`` / ``COOLING`` /
    ``WAF_CHALLENGE`` / …），它们不是 ``SearchRun.status``。
    """
    tree = ast.parse(Path(pagination_module.__file__).read_text(encoding="utf-8"))
    statuses = {SearchRun(keyword="probe").status}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id.isupper() for t in node.targets):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            statuses.add(node.value.value)
    return statuses


# ------------------------------------------------------------------------ fixtures


@pytest.fixture
def repo(tmp_path):
    repository = Repository.connect(tmp_path / "scout-task7.db")
    try:
        yield repository
    finally:
        repository.close()


@pytest.fixture
def clock():
    return lambda: COLLECTED_AT


@pytest.fixture
def fake_transport():
    return FakeTransport(runs={KEYWORD: completed_run()})


@pytest.fixture
def service(fake_transport, repo, clock):
    return DiscoveryService(
        transport=fake_transport,
        repository=repo,
        session_id_hash=SESSION_ID_HASH,
        clock=clock,
    )


def base_query(**overrides) -> SearchQuery:
    options = dict(
        keywords=(KEYWORD,),
        hashtags=("fitness",),
        exclude_keywords=("招聘",),
        limit=20,
        cursor=None,
    )
    options.update(overrides)
    return SearchQuery(**options)


# ---------------------------------------------------------------------------- tests


def test_search_service_runs_search_to_creator_results_without_network(service, fake_transport):
    response = service.search(base_query(), request_id="req-1")

    assert response.schema_version == SCHEMA_VERSION
    assert response.data
    assert all(row.creator_id for row in response.data)
    assert all("sessionid" not in row.to_public_dict() for row in response.data)
    assert all("session_id_hash" in row.provenance for row in response.data)
    assert fake_transport.call_count == 1


def test_public_dto_carries_no_session_material_or_raw_payload(service):
    response = service.search(base_query(), request_id="req-dto")

    serialized = json.dumps(response.to_public_dict(), ensure_ascii=False, default=str)
    assert find_sensitive_fields(serialized) == ()
    for marker in SENSITIVE_FIELD_MARKERS:
        assert marker not in serialized.lower()

    row = response.data[0].to_public_dict()
    assert "raw_payload" not in row
    assert "raw" not in row
    assert "cookie" not in row
    assert "x_bogus" not in row
    assert row["provenance"]["session_id_hash"] == SESSION_ID_HASH


def test_provenance_maps_source_scope_signer_and_collected_at(service):
    response = service.search(base_query(), request_id="req-prov")

    for row in response.data:
        provenance = row.provenance
        assert provenance["source"]
        assert provenance["source_scope"]
        assert provenance["signer_version"] == SIGNER_VERSION == "none-v1"
        assert provenance["collected_at"] == COLLECTED_AT
        assert provenance["session_id_hash"] == SESSION_ID_HASH

    note = response.meta["signer"]["note"]
    assert "未使用动态签名" in note
    assert "只读请求模板" in note


def test_search_persists_results_and_writes_audit(service, repo):
    response = service.search(base_query(), request_id="req-store")
    assert response.data

    loaded = repo.get_search("req-store")
    assert loaded is not None
    assert loaded.creators
    assert loaded.videos

    audits = [row for row in repo.list_audit() if row["request_id"] == "req-store"]
    assert len(audits) == 1
    assert audits[0]["operation"] == "search"
    assert audits[0]["outcome"] == "ok"


def test_cache_hit_does_not_call_transport_again(service, fake_transport):
    first = service.search(base_query(), request_id="req-cache-1")
    assert fake_transport.call_count == 1
    assert first.meta["cache"]["hit"] is False

    second = service.search(base_query(), request_id="req-cache-2")

    assert fake_transport.call_count == 1
    assert second.meta["cache"]["hit"] is True
    assert second.partial is False
    assert [row.creator_id for row in second.data] == [row.creator_id for row in first.data]


def test_cache_hit_keeps_matched_and_truncated_counts_consistent(service, fake_transport):
    """同一 query：命中缓存的 total_matched / rows_truncated 必须与实采一致。

    命中路径若拿不到过滤后计数，就会用「limit 截断后的行数」冒充命中总数
    （实采 6/4 → 缓存 2/0）。
    """
    query = base_query(limit=2)

    first = service.search(query, request_id="req-counts-live")
    second = service.search(query, request_id="req-counts-cached")

    assert fake_transport.call_count == 1
    assert second.meta["cache"]["hit"] is True
    assert len(first.data) == len(second.data) == 2
    # 实采确实被 limit 截断，否则这条用例测不出差异
    assert first.pagination["total_matched"] > 2
    assert first.meta["counts"]["rows_truncated"] > 0

    assert second.pagination["total_matched"] == first.pagination["total_matched"]
    assert second.pagination["returned"] == first.pagination["returned"]
    assert second.meta["counts"]["rows_truncated"] == first.meta["counts"]["rows_truncated"]
    assert second.meta["counts"]["creators_matched"] == first.meta["counts"]["creators_matched"]
    assert second.meta["counts"]["creators_filtered_out"] == (
        first.meta["counts"]["creators_filtered_out"]
    )


def test_cache_scope_includes_session_and_signer_and_schema(service, fake_transport, repo, clock):
    service.search(base_query(), request_id="req-scope-1")

    other = DiscoveryService(
        transport=fake_transport,
        repository=repo,
        session_id_hash=OTHER_SESSION_ID_HASH,
        clock=clock,
    )
    other.search(base_query(), request_id="req-scope-2")

    assert fake_transport.call_count == 2

    different_limit = DiscoveryService(
        transport=fake_transport,
        repository=repo,
        session_id_hash=SESSION_ID_HASH,
        clock=clock,
    )
    different_limit.search(base_query(limit=5), request_id="req-scope-3")
    assert fake_transport.call_count == 3


def test_purged_session_removes_its_cache_entry(service, repo):
    service.search(base_query(), request_id="req-purge-1")
    cache_key = service.cache_key(base_query())
    assert repo.get_cached(cache_key, now=COLLECTED_AT) is not None

    repo.purge_session(SESSION_ID_HASH, revoked_at=COLLECTED_AT)

    assert repo.get_cached(cache_key, now=COLLECTED_AT) is None


def test_revoked_session_refuses_search_even_when_cache_exists(service, repo):
    repo.upsert_session_meta("session-1", SESSION_ID_HASH, imported_at=COLLECTED_AT)
    service.search(base_query(), request_id="req-revoke-1")

    repo.purge_session(SESSION_ID_HASH, revoked_at=COLLECTED_AT)

    with pytest.raises(SessionRevokedError):
        service.search(base_query(), request_id="req-revoke-2")

    audits = {row["request_id"]: row for row in repo.list_audit()}
    assert audits["req-revoke-2"]["outcome"] == "error"
    assert "session_revoked" in audits["req-revoke-2"]["detail"]


def test_cooling_partial_results_are_marked_partial_with_stable_reason(repo, clock):
    run = SearchRun(
        keyword=KEYWORD,
        records=fixture_records()[:3],
        pages=2,
        status=COOLING_STOPPED,
        detail="空响应体连续 3 次，退避已达上限 8.0s，停止本轮搜索",
        attempts=4,
    )
    transport = FakeTransport(runs={KEYWORD: run})
    service = DiscoveryService(
        transport=transport, repository=repo, session_id_hash=SESSION_ID_HASH, clock=clock
    )

    response = service.search(base_query(), request_id="req-cooling")

    assert response.partial is True
    assert "run_stopped:cooling_stopped" in response.meta["partial_reasons"]
    assert response.meta["transport"]["stops"][0]["status"] == COOLING_STOPPED
    assert response.data
    assert response.meta["result_kind"] == "creators_found"


def test_stalled_pagination_is_reported_as_partial(repo, clock):
    run = SearchRun(
        keyword=KEYWORD,
        records=fixture_records(),
        pages=2,
        status=PAGINATION_STALLED,
        detail="重复页指纹：上游返回同一批记录",
        attempts=2,
    )
    transport = FakeTransport(runs={KEYWORD: run})
    service = DiscoveryService(
        transport=transport, repository=repo, session_id_hash=SESSION_ID_HASH, clock=clock
    )

    response = service.search(base_query(), request_id="req-stalled")

    assert response.partial is True
    assert "run_stopped:pagination_stalled" in response.meta["partial_reasons"]


def test_pagination_stopped_is_a_normal_completion_and_is_cached(repo, clock):
    """跑满 limit 的 pagination_stopped 是正常终止：不 partial、照常落缓存。"""
    run = SearchRun(
        keyword=KEYWORD,
        records=fixture_records(),
        pages=1,
        status=PAGINATION_STOPPED,
        detail="达到 max_results 上限 20",
        attempts=1,
    )
    transport = FakeTransport(runs={KEYWORD: run})
    service = DiscoveryService(
        transport=transport, repository=repo, session_id_hash=SESSION_ID_HASH, clock=clock
    )
    query = base_query()

    response = service.search(query, request_id="req-pagination-stopped")

    assert response.partial is False
    assert response.meta["partial_reasons"] == ()
    assert response.meta["transport"]["stops"] == ()
    assert response.meta["transport"]["runs"][0]["status"] == PAGINATION_STOPPED
    assert response.data
    assert repo.get_cached(service.cache_key(query), now=COLLECTED_AT) is not None

    again = service.search(query, request_id="req-pagination-stopped-2")

    assert transport.call_count == 1
    assert again.meta["cache"]["hit"] is True


@pytest.mark.parametrize("status", [COOLING_STOPPED, PAGINATION_STALLED])
def test_damaging_stops_are_partial_and_never_cached(repo, clock, status):
    """cooling_stopped / pagination_stalled 完整度受损：partial 且不落缓存。"""
    run = SearchRun(
        keyword=KEYWORD,
        records=fixture_records(),
        pages=2,
        status=status,
        detail="本轮提前结束，完整度受损",
        attempts=2,
    )
    transport = FakeTransport(runs={KEYWORD: run})
    service = DiscoveryService(
        transport=transport, repository=repo, session_id_hash=SESSION_ID_HASH, clock=clock
    )
    query = base_query()

    response = service.search(query, request_id=f"req-damaged-{status}")

    assert response.partial is True
    assert f"run_stopped:{status}" in response.meta["partial_reasons"]
    assert repo.get_cached(service.cache_key(query), now=COLLECTED_AT) is None

    service.search(query, request_id=f"req-damaged-{status}-2")

    assert transport.call_count == 2  # 没落缓存，第二轮必须重新采集


def test_stop_status_tables_cover_upstream_statuses():
    """NORMAL ∪ DAMAGING 必须覆盖 pagination.py 全部 SearchRun 状态常量。

    上游新增状态而这里没登记时，这条断言先失败，防止新状态被
    ``_completeness_damaged`` 静默当成「完整」处理。
    """
    upstream = upstream_status_constants()
    assert upstream, "AST 解析应至少找到上游的状态常量"
    known = set(NORMAL_STOP_STATUSES) | set(DAMAGING_STOP_STATUSES)
    assert known == upstream
    assert not set(NORMAL_STOP_STATUSES) & set(DAMAGING_STOP_STATUSES)


def test_cache_identity_varies_by_signer_version(fake_transport, repo, clock):
    service = DiscoveryService(
        transport=fake_transport, repository=repo, session_id_hash=SESSION_ID_HASH,
        signer_version=SIGNER_VERSION, clock=clock,
    )
    other = DiscoveryService(
        transport=fake_transport, repository=repo, session_id_hash=SESSION_ID_HASH,
        signer_version="msToken-v2", clock=clock,
    )

    service.search(base_query(), request_id="req-signer-1")
    other.search(base_query(), request_id="req-signer-2")

    assert fake_transport.call_count == 2
    assert service.cache_key(base_query()) != other.cache_key(base_query())


def test_cache_identity_varies_by_schema_version(fake_transport, repo, clock):
    service = DiscoveryService(
        transport=fake_transport, repository=repo, session_id_hash=SESSION_ID_HASH, clock=clock
    )
    other = DiscoveryService(
        transport=fake_transport, repository=repo, session_id_hash=SESSION_ID_HASH,
        schema_version="1.1", clock=clock,
    )

    assert service.schema_version == SCHEMA_VERSION
    assert other.schema_version == "1.1"

    first = service.search(base_query(), request_id="req-schema-1")
    second = other.search(base_query(), request_id="req-schema-2")

    assert first.schema_version == SCHEMA_VERSION
    assert second.schema_version == "1.1"
    assert fake_transport.call_count == 2
    assert service.cache_key(base_query()) != other.cache_key(base_query())


def test_cache_seed_rejection_degrades_to_business_result(repo, clock):
    """缓存播种被敏感闸门拒收时：数据照常落库、业务结果照常返回，只标注缓存未写。"""
    run = SearchRun(
        keyword=KEYWORD,
        records=cookie_username_records(),
        pages=1,
        status=COMPLETED,
        detail="fake 返回 cookie 用户名页面",
        attempts=1,
    )
    transport = FakeTransport(runs={KEYWORD: run})
    service = DiscoveryService(
        transport=transport, repository=repo, session_id_hash=SESSION_ID_HASH, clock=clock
    )
    query = base_query()

    response = service.search(query, request_id="req-cache-seed-rejected")

    assert response.data
    assert response.partial is False
    assert response.meta["cache"]["hit"] is False
    assert response.meta["cache"]["cache_skip_reason"].startswith("cache_seed_rejected")
    assert repo.get_search("req-cache-seed-rejected") is not None
    assert repo.get_cached(service.cache_key(query), now=COLLECTED_AT) is None

    audits = {row["request_id"]: row for row in repo.list_audit()}
    assert audits["req-cache-seed-rejected"]["outcome"] == "ok"
    assert "cache_seed_rejected" in audits["req-cache-seed-rejected"]["detail"]

    service.search(query, request_id="req-cache-seed-rejected-2")
    assert transport.call_count == 2  # 缓存没写 → 重新采集，而不是命中空缓存


def test_waf_on_one_keyword_keeps_other_keyword_results_partial(repo, clock):
    transport = FakeTransport(
        runs={KEYWORD: completed_run()},
        errors={"fitness": WafChallengeError(DETAIL_WAF)},
    )
    service = DiscoveryService(
        transport=transport, repository=repo, session_id_hash=SESSION_ID_HASH, clock=clock
    )
    query = base_query(keywords=(KEYWORD, "fitness"))

    response = service.search(query, request_id="req-waf-partial")

    assert response.partial is True
    assert "transport_error:waf_challenge" in response.meta["partial_reasons"]
    assert response.meta["transport"]["errors"][0]["code"] == "waf_challenge"
    assert response.data
    assert response.meta["transport"]["runs"][0]["keyword"] == KEYWORD


def test_waf_on_every_keyword_is_not_reported_as_no_creators(repo, clock):
    transport = FakeTransport(errors={KEYWORD: WafChallengeError(DETAIL_WAF)})
    service = DiscoveryService(
        transport=transport, repository=repo, session_id_hash=SESSION_ID_HASH, clock=clock
    )

    response = service.search(base_query(), request_id="req-waf-all")

    assert response.data == ()
    assert response.partial is True
    assert response.meta["result_kind"] == "no_data_collected"
    assert response.meta["transport"]["errors"]
    assert "transport_error:waf_challenge" in response.meta["partial_reasons"]


def test_incomplete_collection_is_not_cached(repo, clock):
    transport = FakeTransport(runs={KEYWORD: completed_run()},
                              errors={"fitness": WafChallengeError(DETAIL_WAF)})
    service = DiscoveryService(
        transport=transport, repository=repo, session_id_hash=SESSION_ID_HASH, clock=clock
    )
    query = base_query(keywords=(KEYWORD, "fitness"))

    service.search(query, request_id="req-nocache-1")
    assert transport.call_count == 2

    service.search(query, request_id="req-nocache-2")
    assert transport.call_count == 4  # 不完整轮次不写缓存，第二次仍要重新采集


def test_empty_pages_are_not_reported_as_no_creators(repo, clock):
    transport = FakeTransport(default_records=(), default_status=COMPLETED)
    service = DiscoveryService(
        transport=transport, repository=repo, session_id_hash=SESSION_ID_HASH, clock=clock
    )

    response = service.search(base_query(), request_id="req-empty")

    assert response.data == ()
    assert response.partial is True
    assert response.meta["result_kind"] == "no_data_collected"
    assert "run_stopped:no_records_collected" in response.meta["partial_reasons"]


def test_filters_are_applied_and_explained(service):
    response = service.search(
        base_query(min_followers=50_000_000, min_views=1_000_000_000), request_id="req-filter"
    )

    assert response.data == ()
    assert response.partial is False
    assert response.meta["result_kind"] == "no_creators_after_filters"
    assert response.meta["counts"]["creators_filtered_out"]
    assert response.meta["counts"]["records_collected"] > 0


def test_limit_truncates_rows_and_pagination_stays_stable(service):
    response = service.search(base_query(limit=2), request_id="req-limit")

    assert len(response.data) == 2
    assert response.pagination["limit"] == 2
    assert response.pagination["returned"] == 2
    # Phase 1 给不出续传 cursor，所以不声称「还有更多」；截断量改在 counts 里如实给出
    assert response.pagination["has_more"] is False
    assert response.pagination["next_cursor"] is None
    assert response.pagination["total_matched"] > 2
    assert response.meta["counts"]["rows_truncated"] == (
        response.pagination["total_matched"] - 2
    )
    assert [row.rank for row in response.data] == [1, 2]


def test_all_rows_filtered_out_reports_zero_matched_and_no_more(service):
    query = base_query(min_followers=50_000_000, min_views=1_000_000_000)
    response = service.search(query, request_id="req-all-filtered")

    assert response.data == ()
    assert response.pagination["returned"] == 0
    assert response.pagination["total_matched"] == 0
    assert response.pagination["has_more"] is False
    assert response.pagination["next_cursor"] is None
    assert response.meta["counts"]["creators_matched"] == 0
    assert response.meta["counts"]["rows_truncated"] == 0
    assert response.meta["result_kind"] == "no_creators_after_filters"


def test_cursor_is_echoed_and_flagged_as_non_resumable(service):
    response = service.search(base_query(cursor="12"), request_id="req-cursor")

    assert response.pagination["cursor"] == "12"
    assert response.pagination["next_cursor"] is None
    assert any("cursor" in warning for warning in response.meta["warnings"])


def test_normalization_makes_cache_key_stable(service, fake_transport, repo, clock):
    service.search(base_query(keywords=("  Fitness   COACH ",)), request_id="req-norm-1")
    service.search(base_query(keywords=("fitness coach",)), request_id="req-norm-2")

    assert fake_transport.call_count == 1
    assert fake_transport.calls[0] == (KEYWORD, DEFAULT_LIMIT)


def test_works_with_real_public_readonly_transport(repo, clock):
    """服务与真实 transport 接口对接：注入 fetch，不发网络请求。"""
    pages = [load_fixture_payload(), terminal_page()]
    seen: list[tuple[str, str | None]] = []

    def fake_fetch(keyword: str, cursor: str | None = None) -> RawReply:
        seen.append((keyword, cursor))
        page = dict(pages[min(len(seen) - 1, len(pages) - 1)])
        if len(seen) == 1:
            page["has_more"] = 0  # 构造末页，让分页策略自然结束
        return RawReply(status=200, body=json.dumps(page, ensure_ascii=False))

    transport = PublicReadonlySearch(fake_fetch, max_results=20)
    service = DiscoveryService(
        transport=transport, repository=repo, session_id_hash=SESSION_ID_HASH, clock=clock
    )

    response = service.search(base_query(), request_id="req-real-transport")

    assert response.schema_version == SCHEMA_VERSION
    assert response.data
    assert all(row.creator_id for row in response.data)
    assert response.partial is False
    assert seen == [(KEYWORD, None)]


def test_request_id_is_required(service):
    with pytest.raises(ValueError):
        service.search(base_query(), request_id="")


def test_keywords_must_not_be_empty(service):
    with pytest.raises(ValueError):
        service.search(base_query(keywords=()), request_id="req-empty-kw")
