"""Task 8 — API 鉴权、预算与敏感输入边界测试（离线）。

同样通过 in-process handler 调用，不绑端口、不发请求。API Key 只用合成值
``internal-test-key``，不读 ``.secrets/``、不落真实 key。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.api import auth as auth_module
from app.api.app import ApiApp, VaultSessionProvider
from app.api.auth import ApiKeyAuth, RequestBudget
from app.api.errors import (
    AUTH_REQUIRED,
    FORBIDDEN_INPUT,
    INVALID_QUERY,
    PAYLOAD_TOO_LARGE,
    REQUEST_BUDGET_EXCEEDED,
)
from app.api.schemas import (
    MAX_BODY_BYTES,
    MAX_IDEMPOTENCY_KEY_LENGTH,
    MAX_KEYWORDS,
    MAX_LIMIT,
)
from app.discovery.service import DiscoveryService
from app.domain.normalization import hash_session_id
from app.sessions.models import SessionMeta, SessionStatus
from app.sessions.vault import InMemorySessionVault
from app.storage.repositories import Repository
from app.transport.pagination import COMPLETED, SearchRun

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "search_general_fitness_coach.json"

API_KEY = "internal-test-key"
AUTH = {"Authorization": f"Bearer {API_KEY}"}
COLLECTED_AT = "2026-10-02T12:00:00+00:00"
SESSION_ID = "tiktok:task8security"
SESSION_ID_HASH = hash_session_id("task8-security-session")
KEYWORD = "fitness coach"


# --------------------------------------------------------------------------- fakes


def fixture_records() -> tuple[dict, ...]:
    return tuple(json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["data"])


class FakeTransport:
    def __init__(self):
        self.calls: list[tuple[str, int | None]] = []

    def search(self, keyword: str, *, max_results: int | None = None) -> SearchRun:
        self.calls.append((keyword, max_results))
        return SearchRun(
            keyword=keyword,
            records=fixture_records(),
            pages=1,
            status=COMPLETED,
            detail="fake 返回 fixture 页面",
            attempts=1,
        )

    @property
    def call_count(self) -> int:
        return len(self.calls)


class ApiClient:
    def __init__(self, app):
        self._app = app

    def request(self, method: str, path: str, headers=None, json_body=None):
        body = b"" if json_body is None else json.dumps(json_body).encode("utf-8")
        return self._app(method, path, headers=dict(headers or {}), body=body)

    def get(self, path: str, headers=None):
        return self.request("GET", path, headers=headers)

    def post(self, path: str, headers=None, json=None):
        return self.request("POST", path, headers=headers, json_body=json)


def build_app(*, service, repository, budget=None, vault=None, api_keys=(API_KEY,)):
    return ApiApp(
        service=service,
        repository=repository,
        auth=ApiKeyAuth(api_keys=api_keys),
        budget=budget or RequestBudget(max_calls=1000, window_seconds=60),
        sessions=VaultSessionProvider(vault) if vault is not None else None,
        clock=lambda: COLLECTED_AT,
    )


# ------------------------------------------------------------------------ fixtures


@pytest.fixture
def repo(tmp_path):
    repository = Repository.connect(tmp_path / "task8-security.db")
    try:
        yield repository
    finally:
        repository.close()


@pytest.fixture
def transport():
    return FakeTransport()


@pytest.fixture
def service(transport, repo):
    return DiscoveryService(
        transport=transport,
        repository=repo,
        session_id_hash=SESSION_ID_HASH,
        clock=lambda: COLLECTED_AT,
    )


@pytest.fixture
def vault():
    return InMemorySessionVault(
        SessionMeta(
            session_id=SESSION_ID,
            session_id_hash=SESSION_ID_HASH,
            source="task8-fixture",
            status=SessionStatus.ACTIVE,
            imported_at=COLLECTED_AT,
            expires_at=None,
        )
    )


@pytest.fixture
def app(service, repo, vault):
    return build_app(service=service, repository=repo, vault=vault)


@pytest.fixture
def api_client(app):
    return ApiClient(app)


# --------------------------------------------------------------------------- tests


def test_external_request_cannot_supply_cookie_or_arbitrary_url(api_client):
    response = api_client.post(
        "/v1/tiktok/creator-search",
        headers={"Authorization": "Bearer internal-test-key"},
        json={"keywords": ["fitness"], "cookie": "secret", "arbitrary_url": "https://example.com"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == FORBIDDEN_INPUT


def test_sensitive_fields_are_rejected_nested_and_on_every_route(api_client):
    cases = [
        ("/v1/tiktok/creator-search", {"keywords": ["fitness"], "sessionid": "x"}),
        ("/v1/tiktok/creator-search", {"keywords": ["fitness"], "headers": {"msToken": "x"}}),
        ("/v1/tiktok/creator-search", {"keywords": ["fitness"], "options": {"proxy": "1.2.3.4"}}),
        ("/v1/tiktok/profiles/resolve", {"usernames": ["somebody"], "cookie": "x"}),
    ]
    for path, payload in cases:
        response = api_client.post(path, headers=AUTH, json=payload)
        assert response.status_code == 400, path
        assert response.json()["error"]["code"] == FORBIDDEN_INPUT, path
        assert response.json()["error"]["details"]["forbidden_fields"]


def test_credential_shaped_values_are_rejected_too(api_client):
    response = api_client.post(
        "/v1/tiktok/creator-search",
        headers=AUTH,
        json={"keywords": ["fitness"], "note": "sessionid=abc123; msToken=def"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == FORBIDDEN_INPUT


def test_missing_or_wrong_api_key_is_rejected(api_client):
    missing = api_client.post("/v1/tiktok/creator-search", json={"keywords": [KEYWORD]})
    wrong = api_client.post(
        "/v1/tiktok/creator-search",
        headers={"Authorization": "Bearer internal-test-keyX"},
        json={"keywords": [KEYWORD]},
    )
    near = api_client.post(
        "/v1/tiktok/creator-search",
        headers={"Authorization": "Bearer Internal-test-key"},
        json={"keywords": [KEYWORD]},
    )

    for response in (missing, wrong, near):
        assert response.status_code == 401
        assert response.json()["error"]["code"] == AUTH_REQUIRED
        assert API_KEY not in response.text


def test_authorization_scheme_is_enforced(api_client):
    raw = api_client.post(
        "/v1/tiktok/creator-search",
        headers={"Authorization": API_KEY},
        json={"keywords": [KEYWORD]},
    )
    other_scheme = api_client.post(
        "/v1/tiktok/creator-search",
        headers={"Authorization": f"Token {API_KEY}"},
        json={"keywords": [KEYWORD]},
    )

    assert raw.status_code == 401
    assert other_scheme.status_code == 401

    # X-API-Key 第二套鉴权入口已移除：只认 Authorization: Bearer
    header_variant = api_client.post(
        "/v1/tiktok/creator-search",
        headers={"X-API-Key": API_KEY},
        json={"keywords": [KEYWORD]},
    )
    assert header_variant.status_code == 401
    assert header_variant.json()["error"]["code"] == AUTH_REQUIRED


def test_api_key_is_compared_in_constant_time(monkeypatch, api_client):
    calls = []
    real_compare = auth_module.hmac.compare_digest

    def spy(left, right):
        calls.append((left, right))
        return real_compare(left, right)

    monkeypatch.setattr(auth_module.hmac, "compare_digest", spy)

    api_client.post("/v1/tiktok/creator-search", headers=AUTH, json={"keywords": [KEYWORD]})
    assert calls

    api_client.post(
        "/v1/tiktok/creator-search",
        headers={"Authorization": "Bearer internal-test-ke"},
        json={"keywords": [KEYWORD]},
    )
    assert len(calls) > 1
    assert all(isinstance(left, bytes) and isinstance(right, bytes) for left, right in calls)


def test_api_key_never_reaches_response_or_audit(api_client, repo):
    response = api_client.post(
        "/v1/tiktok/creator-search", headers=AUTH, json={"keywords": [KEYWORD]}
    )

    assert response.status_code == 200
    assert API_KEY not in response.text
    assert API_KEY not in json.dumps(dict(response.headers))

    audits = [dict(row) for row in repo.list_audit()]
    assert audits
    for entry in audits:
        assert API_KEY not in json.dumps(entry, default=str)
        assert entry["actor"] is None or entry["actor"].startswith("key:")


def test_request_body_size_is_bounded(api_client):
    oversized = {"keywords": [KEYWORD], "padding": "x" * (MAX_BODY_BYTES + 1)}

    response = api_client.post("/v1/tiktok/creator-search", headers=AUTH, json=oversized)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == PAYLOAD_TOO_LARGE


def test_oversized_content_length_is_rejected_before_any_read(api_client, transport):
    """Content-Length 闸门在读取之前生效：超大声明直接 413，body 不会进内存。"""
    response = api_client.post(
        "/v1/tiktok/creator-search",
        headers={**AUTH, "Content-Length": str(MAX_BODY_BYTES + 1)},
        json={"keywords": [KEYWORD]},
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == PAYLOAD_TOO_LARGE
    assert response.json()["error"]["details"]["max_body_bytes"] == MAX_BODY_BYTES
    assert transport.call_count == 0


def test_malformed_content_length_is_rejected(api_client):
    response = api_client.post(
        "/v1/tiktok/creator-search",
        headers={**AUTH, "Content-Length": "not-a-number"},
        json={"keywords": [KEYWORD]},
    )
    negative = api_client.post(
        "/v1/tiktok/creator-search",
        headers={**AUTH, "Content-Length": "-5"},
        json={"keywords": [KEYWORD]},
    )

    for bad in (response, negative):
        assert bad.status_code == 400
        assert bad.json()["error"]["code"] == INVALID_QUERY


def test_keyword_count_is_bounded(api_client):
    response = api_client.post(
        "/v1/tiktok/creator-search",
        headers=AUTH,
        json={"keywords": [f"kw-{index}" for index in range(MAX_KEYWORDS + 1)]},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == INVALID_QUERY
    assert "keywords" in json.dumps(response.json()["error"]["details"])


def test_limit_cannot_be_raised_above_the_cap(api_client, transport):
    too_large = api_client.post(
        "/v1/tiktok/creator-search", headers=AUTH, json={"keywords": [KEYWORD], "limit": MAX_LIMIT + 1}
    )
    zero = api_client.post(
        "/v1/tiktok/creator-search", headers=AUTH, json={"keywords": [KEYWORD], "limit": 0}
    )

    assert too_large.status_code == 400
    assert too_large.json()["error"]["code"] == INVALID_QUERY
    assert zero.status_code == 400
    assert transport.call_count == 0


def test_caller_cannot_raise_its_own_budget_or_crawl_volume(api_client):
    response = api_client.post(
        "/v1/tiktok/creator-search",
        headers=AUTH,
        json={"keywords": [KEYWORD], "budget": 10000, "max_calls": 10000, "max_pages": 100},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == INVALID_QUERY
    assert set(response.json()["error"]["details"]["unknown_fields"]) == {
        "budget",
        "max_calls",
        "max_pages",
    }


def test_idempotency_key_is_validated(api_client):
    too_long = api_client.post(
        "/v1/tiktok/creator-search",
        headers={**AUTH, "Idempotency-Key": "k" * (MAX_IDEMPOTENCY_KEY_LENGTH + 1)},
        json={"keywords": [KEYWORD]},
    )
    bad_chars = api_client.post(
        "/v1/tiktok/creator-search",
        headers={**AUTH, "Idempotency-Key": "not a valid key!"},
        json={"keywords": [KEYWORD]},
    )
    in_body = api_client.post(
        "/v1/tiktok/creator-search",
        headers=AUTH,
        json={"keywords": [KEYWORD], "idempotency_key": "short"},
    )

    for response in (too_long, bad_chars, in_body):
        assert response.status_code == 400
        assert response.json()["error"]["code"] == INVALID_QUERY


def test_budget_exceeded_blocks_calls_without_extra_crawling(service, repo, transport):
    client = ApiClient(
        build_app(
            service=service,
            repository=repo,
            budget=RequestBudget(max_calls=1, window_seconds=60),
        )
    )

    first = client.post("/v1/tiktok/creator-search", headers=AUTH, json={"keywords": [KEYWORD]})
    second = client.post("/v1/tiktok/creator-search", headers=AUTH, json={"keywords": [KEYWORD]})

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["error"]["code"] == REQUEST_BUDGET_EXCEEDED
    assert "Retry-After" in second.headers
    assert transport.call_count == 1


def test_audit_records_outcome_for_every_call(api_client, repo):
    denied = api_client.post(
        "/v1/tiktok/creator-search",
        headers=AUTH,
        json={"keywords": ["fitness"], "cookie": "secret"},
    )
    ok = api_client.post("/v1/tiktok/creator-search", headers=AUTH, json={"keywords": [KEYWORD]})
    unauthenticated = api_client.post(
        "/v1/tiktok/creator-search", json={"keywords": [KEYWORD]}
    )

    assert denied.status_code == 400
    assert ok.status_code == 200
    assert unauthenticated.status_code == 401

    outcomes = {row["outcome"] for row in repo.list_audit()}
    assert {"denied", "ok"} <= outcomes
    operations = {row["operation"] for row in repo.list_audit()}
    assert "creator_search" in operations


def test_session_id_in_path_is_validated(api_client):
    traversal = api_client.get(
        "/internal/v1/tiktok/sessions/..%2F..%2Fetc%2Fpasswd/status", headers=AUTH
    )
    unknown = api_client.get("/internal/v1/tiktok/sessions/nope/status", headers=AUTH)

    assert traversal.status_code == 400
    assert traversal.json()["error"]["code"] == INVALID_QUERY
    assert unknown.status_code == 404


def test_session_revoke_rejects_forbidden_body(api_client):
    """revoke 路由收 body，敏感字段同样要过输入闸门，且不执行撤销。"""
    response = api_client.post(
        f"/internal/v1/tiktok/sessions/{SESSION_ID}/revoke",
        headers=AUTH,
        json={"sessionid": "injected"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == FORBIDDEN_INPUT
    assert response.json()["error"]["details"]["forbidden_fields"]

    status = api_client.get(
        f"/internal/v1/tiktok/sessions/{SESSION_ID}/status", headers=AUTH
    )
    assert status.json()["data"]["status"] == "active"


def test_error_bodies_carry_no_session_material(api_client, repo, transport):
    response = api_client.post(
        "/v1/tiktok/creator-search", headers=AUTH, json={"keywords": [], "cookie": "x"}
    )

    assert response.status_code == 400
    serialized = response.text.lower()
    for forbidden in ("sessionid", "mstoken", "storage_state", "raw_headers", "signature"):
        assert forbidden not in serialized
    assert "Bearer" not in response.text
