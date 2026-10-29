"""Task 8 — 内部同步 API 合约测试（离线）。

测试通过 in-process handler 直接调用 ``ApiApp``：不绑端口、不发外部请求、
不读 ``.secrets/``。所有 fixture（fake transport / repo / vault / client）
就地定义在本文件，不改动 ``conftest.py``，也不修改任何上游模块。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.api.app import ApiApp, VaultSessionProvider
from app.api.auth import ApiKeyAuth, RequestBudget
from app.api.errors import (
    AUTH_REQUIRED,
    IDEMPOTENCY_CONFLICT,
    METHOD_NOT_ALLOWED,
    NOT_FOUND,
    PROFILE_UNRESOLVED,
    SESSION_REAUTH_REQUIRED,
    SESSION_REVOKED,
    UPSTREAM_RATE_LIMITED,
    UPSTREAM_SCHEMA_CHANGED,
    UPSTREAM_WAF_CHALLENGE,
)
from app.discovery.service import SCHEMA_VERSION, DiscoveryService
from app.domain.normalization import hash_session_id
from app.sessions.models import SessionMeta, SessionStatus
from app.sessions.vault import InMemorySessionVault
from app.storage.repositories import Repository
from app.transport.pagination import COMPLETED, SearchRun
from app.transport.response_classifier import (
    RateLimitedError,
    SchemaChangedError,
    TransportError,
    WafChallengeError,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "search_general_fitness_coach.json"

API_KEY = "internal-test-key"
AUTH = {"Authorization": f"Bearer {API_KEY}"}

COLLECTED_AT = "2026-10-02T12:00:00+00:00"
SESSION_ID = "tiktok:task8contract"
SESSION_ID_HASH = hash_session_id("task8-contract-session")

KEYWORD = "fitness coach"


# --------------------------------------------------------------------------- fakes


def fixture_records() -> tuple[dict, ...]:
    return tuple(json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["data"])


class FakeTransport:
    """``PublicReadonlySearch.search(keyword, *, max_results=None) -> SearchRun`` 的替身。"""

    def __init__(self, records=None, errors=None):
        self.records = tuple(records if records is not None else fixture_records())
        self.errors = dict(errors or {})
        self.calls: list[tuple[str, int | None]] = []

    def search(self, keyword: str, *, max_results: int | None = None) -> SearchRun:
        self.calls.append((keyword, max_results))
        error = self.errors.get(keyword)
        if error is not None:
            raise error
        return SearchRun(
            keyword=keyword,
            records=self.records,
            pages=1,
            status=COMPLETED,
            detail="fake 返回 fixture 页面",
            attempts=1,
        )

    @property
    def call_count(self) -> int:
        return len(self.calls)


class ApiClient:
    """in-process 客户端：直接调用 handler，不经过 socket。"""

    def __init__(self, app):
        self._app = app

    def request(self, method: str, path: str, headers=None, json_body=None):
        body = b"" if json_body is None else json.dumps(json_body).encode("utf-8")
        return self._app(method, path, headers=dict(headers or {}), body=body)

    def get(self, path: str, headers=None):
        return self.request("GET", path, headers=headers)

    def post(self, path: str, headers=None, json=None):
        return self.request("POST", path, headers=headers, json_body=json)


def build_app(*, service, repository, vault=None, budget=None, api_keys=(API_KEY,)):
    return ApiApp(
        service=service,
        repository=repository,
        auth=ApiKeyAuth(api_keys=api_keys),
        budget=budget or RequestBudget(max_calls=1000, window_seconds=60),
        sessions=VaultSessionProvider(vault) if vault is not None else None,
        clock=lambda: COLLECTED_AT,
    )


def build_service(transport, repository, session_id_hash=SESSION_ID_HASH):
    return DiscoveryService(
        transport=transport,
        repository=repository,
        session_id_hash=session_id_hash,
        clock=lambda: COLLECTED_AT,
    )


def active_meta(session_id=SESSION_ID, expires_at=None, status=SessionStatus.ACTIVE):
    return SessionMeta(
        session_id=session_id,
        session_id_hash=SESSION_ID_HASH,
        source="task8-fixture",
        status=status,
        imported_at=COLLECTED_AT,
        expires_at=expires_at,
    )


# ------------------------------------------------------------------------ fixtures


@pytest.fixture
def repo(tmp_path):
    repository = Repository.connect(tmp_path / "task8-contract.db")
    try:
        yield repository
    finally:
        repository.close()


@pytest.fixture
def transport():
    return FakeTransport()


@pytest.fixture
def service(transport, repo):
    return build_service(transport, repo)


@pytest.fixture
def vault():
    return InMemorySessionVault(active_meta())


@pytest.fixture
def app(service, repo, vault):
    return build_app(service=service, repository=repo, vault=vault)


@pytest.fixture
def api_client(app):
    return ApiClient(app)


# --------------------------------------------------------------------------- tests


def test_search_response_has_stable_schema_and_no_session_material(api_client):
    response = api_client.post(
        "/v1/tiktok/creator-search",
        headers={"Authorization": "Bearer internal-test-key"},
        json={"keywords": ["fitness coach"], "page": {"limit": 20}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == "1.0"
    assert "request_id" in body
    serialized = response.text
    for forbidden in ("sessionid", "msToken", "storage", "raw_headers", "signature"):
        assert forbidden not in serialized


def test_search_envelope_carries_contract_keys(api_client):
    response = api_client.post(
        "/v1/tiktok/creator-search", headers=AUTH, json={"keywords": [KEYWORD]}
    )

    assert response.status_code == 200
    body = response.json()
    for key in ("schema_version", "request_id", "data", "pagination", "meta"):
        assert key in body
    assert body["schema_version"] == SCHEMA_VERSION
    assert body["pagination"]["limit"] == 20
    assert body["pagination"]["returned"] == len(body["data"])
    assert body["meta"]["request_id"] == body["request_id"]
    assert response.headers["X-Request-Id"] == body["request_id"]


def test_creator_rows_only_expose_public_fields(api_client):
    response = api_client.post(
        "/v1/tiktok/creator-search", headers=AUTH, json={"keywords": [KEYWORD]}
    )

    rows = response.json()["data"]
    assert rows
    for row in rows:
        assert set(row) == {
            "creator_id",
            "username",
            "profile_url",
            "keywords",
            "total_score",
            "relevance_score",
            "quality_score",
            "freshness_score",
            "score_version",
            "rank",
            "matched_video_count",
            "matched_keywords",
            "match_reasons",
            "unavailable_metrics",
            "follower_count",
            "region_code",
            "total_view_count",
            "collected_at",
            "source",
            "source_scope",
            "provenance",
        }
        assert row["provenance"]["session_id_hash"] == SESSION_ID_HASH
        assert "raw_payload" not in row["provenance"]


def test_search_requires_api_key(api_client):
    response = api_client.post("/v1/tiktok/creator-search", json={"keywords": [KEYWORD]})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == AUTH_REQUIRED
    assert "WWW-Authenticate" in response.headers


def test_async_search_jobs_route_does_not_exist(api_client):
    created = api_client.post(
        "/v1/tiktok/search-jobs", headers=AUTH, json={"keywords": [KEYWORD]}
    )
    fetched = api_client.get("/v1/tiktok/search-jobs/job-1", headers=AUTH)

    assert created.status_code == 404
    assert fetched.status_code == 404
    assert created.json()["error"]["code"] == NOT_FOUND
    assert "request_id" in created.json()


def test_known_path_with_wrong_method_is_405(api_client):
    response = api_client.get("/v1/tiktok/creator-search", headers=AUTH)

    assert response.status_code == 405
    assert response.json()["error"]["code"] == METHOD_NOT_ALLOWED


def test_profiles_resolve_completes_from_local_store_without_network(api_client, transport):
    search = api_client.post(
        "/v1/tiktok/creator-search", headers=AUTH, json={"keywords": [KEYWORD]}
    )
    username = search.json()["data"][0]["username"]
    calls_before = transport.call_count

    response = api_client.post(
        "/v1/tiktok/profiles/resolve", headers=AUTH, json={"usernames": [username]}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["data"][0]["username"] == username
    assert body["meta"]["network_calls"] == 0
    assert transport.call_count == calls_before


def test_profiles_resolve_accepts_profile_urls(api_client, transport):
    search = api_client.post(
        "/v1/tiktok/creator-search", headers=AUTH, json={"keywords": [KEYWORD]}
    )
    username = search.json()["data"][0]["username"]
    calls_before = transport.call_count

    response = api_client.post(
        "/v1/tiktok/profiles/resolve",
        headers=AUTH,
        json={"profiles": [f"https://www.tiktok.com/@{username}"]},
    )

    assert response.status_code == 200
    assert response.json()["data"][0]["username"] == username
    assert transport.call_count == calls_before


def test_profiles_resolve_reports_unresolvable_handle_without_requesting_it(
    api_client, transport
):
    response = api_client.post(
        "/v1/tiktok/profiles/resolve",
        headers=AUTH,
        json={"usernames": ["definitely-not-in-local-store"]},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == PROFILE_UNRESOLVED
    assert "网络" in response.json()["error"]["message"]
    assert transport.call_count == 0


def test_session_status_route_reports_active_state(api_client):
    response = api_client.get(
        f"/internal/v1/tiktok/sessions/{SESSION_ID}/status", headers=AUTH
    )

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["session_id"] == SESSION_ID
    assert body["data"]["status"] == "active"
    assert body["data"]["usable"] is True


def test_revoked_session_stops_further_search(api_client):
    revoked = api_client.post(
        f"/internal/v1/tiktok/sessions/{SESSION_ID}/revoke", headers=AUTH, json={}
    )
    assert revoked.status_code == 200
    assert revoked.json()["data"]["status"] == "revoked"
    assert revoked.json()["data"]["usable"] is False

    status = api_client.get(
        f"/internal/v1/tiktok/sessions/{SESSION_ID}/status", headers=AUTH
    )
    assert status.json()["data"]["status"] == "revoked"

    search = api_client.post(
        "/v1/tiktok/creator-search", headers=AUTH, json={"keywords": [KEYWORD]}
    )
    assert search.status_code == 409
    assert search.json()["error"]["code"] == SESSION_REVOKED


def test_expired_session_requires_reauth(service, repo, transport):
    expired_vault = InMemorySessionVault(
        active_meta(expires_at="2020-01-01T00:00:00+00:00")
    )
    client = ApiClient(build_app(service=service, repository=repo, vault=expired_vault))

    response = client.post(
        "/v1/tiktok/creator-search", headers=AUTH, json={"keywords": [KEYWORD]}
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == SESSION_REAUTH_REQUIRED
    assert transport.call_count == 0


def test_empty_vault_blocks_search_fail_closed(service, repo, transport):
    """fail-closed：vault 里零个会话时按无有效会话处理，绝不放行搜索。"""
    client = ApiClient(
        build_app(service=service, repository=repo, vault=InMemorySessionVault())
    )

    response = client.post(
        "/v1/tiktok/creator-search", headers=AUTH, json={"keywords": [KEYWORD]}
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == SESSION_REAUTH_REQUIRED
    assert transport.call_count == 0


def test_multiple_sessions_block_search_fail_closed(service, repo, transport):
    """fail-closed：vault 里多于一个会话时无法确定唯一活动会话，同样拦截。"""
    ambiguous_vault = InMemorySessionVault(
        active_meta(session_id="tiktok:session-one"),
        active_meta(session_id="tiktok:session-two"),
    )
    client = ApiClient(
        build_app(service=service, repository=repo, vault=ambiguous_vault)
    )

    response = client.post(
        "/v1/tiktok/creator-search", headers=AUTH, json={"keywords": [KEYWORD]}
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == SESSION_REAUTH_REQUIRED
    assert transport.call_count == 0


@pytest.mark.parametrize(
    "error,expected_code,expected_status",
    [
        (WafChallengeError("waf"), UPSTREAM_WAF_CHALLENGE, 503),
        (RateLimitedError("429"), UPSTREAM_RATE_LIMITED, 429),
        (SchemaChangedError("schema"), UPSTREAM_SCHEMA_CHANGED, 502),
        # 基类 TransportError 的 kind 是 unexpected_status：同样必须映射成
        # 非 200 的稳定错误码，不允许 200 + 空 data 冒充「没有达人」。
        (TransportError("unexpected status"), UPSTREAM_SCHEMA_CHANGED, 502),
    ],
)
def test_upstream_failures_map_to_stable_error_codes(
    repo, error, expected_code, expected_status
):
    transport = FakeTransport(errors={KEYWORD: error})
    client = ApiClient(
        build_app(service=build_service(transport, repo), repository=repo)
    )

    response = client.post(
        "/v1/tiktok/creator-search", headers=AUTH, json={"keywords": [KEYWORD]}
    )

    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == expected_code


def test_partial_upstream_failure_stays_200_and_self_declares(repo):
    """有数据时保持 200 + partial 自证：不把半截结果伪装成完整结果，也不整轮失败。"""
    transport = FakeTransport(errors={KEYWORD: WafChallengeError("waf")})
    client = ApiClient(
        build_app(service=build_service(transport, repo), repository=repo)
    )

    response = client.post(
        "/v1/tiktok/creator-search",
        headers=AUTH,
        json={"keywords": [KEYWORD, "yoga trainer"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["partial"] is True
    assert "transport_error:waf_challenge" in body["meta"]["partial_reasons"]
    assert body["data"]


def test_idempotent_replay_returns_the_stored_response(api_client):
    payload = {"keywords": [KEYWORD], "limit": 5}
    first = api_client.post(
        "/v1/tiktok/creator-search",
        headers={**AUTH, "Idempotency-Key": "task8-replay-0001"},
        json=payload,
    )
    second = api_client.post(
        "/v1/tiktok/creator-search",
        headers={**AUTH, "Idempotency-Key": "task8-replay-0001"},
        json=payload,
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["request_id"] == first.json()["request_id"]
    assert second.json()["data"] == first.json()["data"]
    assert second.headers.get("Idempotency-Replay") == "true"


def test_idempotency_key_reused_with_different_payload_is_rejected(api_client):
    headers = {**AUTH, "Idempotency-Key": "task8-conflict-001"}
    api_client.post("/v1/tiktok/creator-search", headers=headers, json={"keywords": ["a"]})

    response = api_client.post(
        "/v1/tiktok/creator-search", headers=headers, json={"keywords": ["b"]}
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == IDEMPOTENCY_CONFLICT


def test_one_call_leaves_two_audit_rows_without_overwrite(api_client, repo):
    """审计 append 语义：service 行（裸 request_id）与 API 行（前缀匹配）
    同时保留，API 行不再覆盖 service 行。"""
    response = api_client.post(
        "/v1/tiktok/creator-search", headers=AUTH, json={"keywords": [KEYWORD]}
    )

    assert response.status_code == 200
    request_id = response.json()["request_id"]
    rows = [
        dict(row)
        for row in repo.list_audit()
        if row["request_id"] == request_id
        or row["request_id"].startswith(f"{request_id}:")
    ]

    assert len(rows) == 2
    assert {row["request_id"] for row in rows} == {
        request_id,
        f"{request_id}:creator_search",
    }
    assert {row["operation"] for row in rows} == {"search", "creator_search"}
