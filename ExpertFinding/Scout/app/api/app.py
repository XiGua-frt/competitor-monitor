"""内部同步 JSON API 入口：路由、鉴权顺序、预算、审计与错误映射。

调用顺序（每一步都可能终止请求，且都会留下审计 outcome）：

    生成 request_id → Content-Length 闸门 → API Key → 敏感输入
    → 字段/limit/idempotency 校验 → idempotency 重放 → 预算 → 会话闸门
    → service → 错误映射 → 审计

只有 ``/v1`` 的搜索与本地补全、``/internal/v1`` 的会话状态/撤回四个路由；
异步 search-jobs 路由**不存在**（调用返回 404），也没有任何字段能让调用方
提供 Cookie、任意 URL 或调高抓取量——本 API 不是通用抓取代理。

传输层是标准库 ``http.server``：``make_http_handler`` 生成
``BaseHTTPRequestHandler`` 子类用于真实启动；测试直接调用 :meth:`ApiApp.handle`
（in-process，不绑端口、不发外部请求）。
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import urlsplit

from app.discovery.service import SearchResponse
from app.sessions.models import SessionStatus
from app.sessions.vault import InMemorySessionVault, is_revoked
from app.storage.repositories import Repository

from .auth import ApiKeyAuth, RequestBudget, assert_no_forbidden_input, header_value
from .errors import (
    AUTH_REQUIRED,
    DENIAL_CODES,
    IDEMPOTENCY_CONFLICT,
    INVALID_QUERY,
    METHOD_NOT_ALLOWED,
    NOT_FOUND,
    PAYLOAD_TOO_LARGE,
    PROFILE_UNRESOLVED,
    SESSION_REAUTH_REQUIRED,
    ApiError,
    map_exception,
    upstream_code_for_kind,
)
from .schemas import (
    MAX_BODY_BYTES,
    envelope,
    parse_resolve_request,
    parse_search_request,
    validate_idempotency_key,
    validate_session_id,
)

ROUTE_CREATOR_SEARCH = "/v1/tiktok/creator-search"
ROUTE_PROFILES_RESOLVE = "/v1/tiktok/profiles/resolve"
SESSION_ROUTE_RE = re.compile(
    r"^/internal/v1/tiktok/sessions/(?P<session_id>[^/]+)/(?P<action>status|revoke)$"
)

OPERATION_CREATOR_SEARCH = "creator_search"
OPERATION_PROFILES_RESOLVE = "profiles_resolve"
OPERATION_SESSION_STATUS = "session_status"
OPERATION_SESSION_REVOKE = "session_revoke"
OPERATION_UNKNOWN = "unknown"

OUTCOME_OK = "ok"
OUTCOME_PARTIAL = "partial"
OUTCOME_ERROR = "error"
OUTCOME_DENIED = "denied"

IDEMPOTENCY_HEADER = "Idempotency-Key"

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


# ------------------------------------------------------------------- transport


@dataclass(frozen=True)
class ApiResponse:
    """一次 HTTP 响应的最小表示，便于 in-process 测试直接断言。"""

    status_code: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")

    def json(self) -> Any:
        return json.loads(self.text)


@dataclass
class _CallContext:
    """审计作用域里传递的上下文：只有 actor 指纹，绝无 Key 明文。"""

    actor: str | None = None


# --------------------------------------------------------------- 本地补全适配器


class RepositoryProfileResolver:
    """只读本地补全：在已落库的公开作者资料里查 username。

    ``Repository`` 没有按 username 查询的入口，这里用它的连接做只读 SELECT
    （不改 schema、不改上游模块）。查不到就返回 ``None`` —— 由 API 转成明确错误，
    绝不退化成"访问 profile 页"的网络请求。
    """

    def __init__(self, repository: Repository):
        self._repository = repository

    def resolve(self, username: str) -> dict[str, Any] | None:
        row = self._repository.connection.execute(
            "SELECT id, username, display_name, profile_url, follower_count, region_code,"
            " video_count, last_seen_at FROM creator"
            " WHERE username = ? AND deleted_at IS NULL",
            (username,),
        ).fetchone()
        if row is None:
            return None
        return {
            "creator_id": row["id"],
            "username": row["username"],
            "display_name": row["display_name"],
            "profile_url": row["profile_url"],
            "follower_count": row["follower_count"],
            "region_code": row["region_code"],
            "video_count": row["video_count"],
            "last_seen_at": row["last_seen_at"],
            "resolved_from": "local_creator_store",
        }


# ------------------------------------------------------------- 会话登记簿适配器


class VaultSessionProvider:
    """把 :class:`InMemorySessionVault` 适配成 API 需要的 status/revoke 接口。"""

    def __init__(self, vault: InMemorySessionVault, session_id: str | None = None):
        self._vault = vault
        self._session_id = session_id

    def status(self, session_id: str) -> dict[str, Any]:
        meta = self._vault.get(session_id)
        expired = meta.is_expired()
        revoked = is_revoked(meta)
        status = (
            SessionStatus.REVOKED
            if revoked
            else (SessionStatus.EXPIRED if expired else SessionStatus.ACTIVE)
        )
        return {
            "session_id": meta.session_id,
            "session_id_hash": meta.session_id_hash,
            "source": meta.source,
            "status": status,
            "usable": not revoked and not expired,
            "imported_at": meta.imported_at,
            "expires_at": meta.expires_at,
        }

    def revoke(self, session_id: str) -> dict[str, Any]:
        self._vault.revoke(session_id)
        return self.status(session_id)

    def require_active(self) -> None:
        """搜索前的会话闸门，fail-closed。

        每次调用都读取 vault 当前状态（不是构造时快照）：vault 里恰好一个
        登记会话时按它校验（revoked/expired 由 vault 抛错翻译）；零个或多于
        一个时无法确定唯一活动会话，按无有效会话处理（SESSION_REAUTH_REQUIRED）。
        """
        session_id = self._session_id or _single_session_id(self._vault)
        if session_id is None:
            raise ApiError(
                SESSION_REAUTH_REQUIRED,
                "无法确定唯一活动会话：请先在登记簿中登记恰好一个有效会话",
            )
        self._vault.require_active(session_id)


def _single_session_id(vault: InMemorySessionVault) -> str | None:
    snapshot = vault.checkpoint()
    return snapshot[0]["session_id"] if len(snapshot) == 1 else None


# ------------------------------------------------------------------------ app


class ApiApp:
    """内部同步 API。service / repository / 会话登记簿全部由调用方注入。"""

    def __init__(
        self,
        *,
        service: Any,
        repository: Repository,
        auth: ApiKeyAuth | None = None,
        budget: RequestBudget | None = None,
        sessions: Any = None,
        profile_resolver: Any = None,
        clock: Callable[[], str] | None = None,
        request_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._service = service
        self._repository = repository
        self._auth = auth or ApiKeyAuth()
        self._budget = budget or RequestBudget()
        self._sessions = sessions
        self._profile_resolver = profile_resolver or RepositoryProfileResolver(repository)
        self._clock = clock
        self._request_id_factory = request_id_factory or _new_request_id
        self._idempotency: dict[str, tuple[str, dict[str, Any]]] = {}

    # ------------------------------------------------------------------ 入口

    def __call__(
        self, method: str, path: str, headers: Any = None, body: bytes = b""
    ) -> ApiResponse:
        return self.handle(method, path, headers=headers, body=body)

    def handle(
        self, method: str, path: str, headers: Any = None, body: bytes = b""
    ) -> ApiResponse:
        request_id = self._request_id(headers)
        try:
            return self._dispatch(method.upper(), urlsplit(path).path, headers, body or b"", request_id)
        except ApiError as exc:
            return self._error_response(exc, request_id)
        except Exception as exc:  # noqa: BLE001 - 顶层兜底，避免泄漏实现细节
            api_error = map_exception(exc)
            self._audit(
                request_id, _operation_for(path), OUTCOME_ERROR, {"error_code": api_error.code}
            )
            return self._error_response(api_error, request_id)

    # ------------------------------------------------------------------ 路由

    def _dispatch(
        self, method: str, path: str, headers: Any, body: bytes, request_id: str
    ) -> ApiResponse:
        with self._audit_scope(_operation_for(path), request_id) as context:
            violation = _content_length_violation(headers)
            if violation is not None:
                raise violation
            if (method, path) == ("POST", ROUTE_CREATOR_SEARCH):
                return self._creator_search(context, headers, body, request_id)
            if (method, path) == ("POST", ROUTE_PROFILES_RESOLVE):
                return self._profiles_resolve(context, headers, body, request_id)

            match = SESSION_ROUTE_RE.match(path)
            if match:
                session_id = validate_session_id(match.group("session_id"))
                action = match.group("action")
                if (method, action) == ("GET", "status"):
                    return self._session_status(context, headers, body, session_id, request_id)
                if (method, action) == ("POST", "revoke"):
                    return self._session_revoke(context, headers, body, session_id, request_id)
                raise ApiError(METHOD_NOT_ALLOWED, f"{method} 不允许用于该路由")

            if path in (ROUTE_CREATOR_SEARCH, ROUTE_PROFILES_RESOLVE):
                raise ApiError(METHOD_NOT_ALLOWED, f"{method} 不允许用于该路由")
            raise ApiError(NOT_FOUND, f"路由不存在：本 API 不提供该能力")

    # -------------------------------------------------------------- 业务处理

    def _creator_search(
        self, context: _CallContext, headers: Any, body: bytes, request_id: str
    ) -> ApiResponse:
        context.actor = self._auth.authenticate(headers)
        payload = _read_payload(body)
        assert_no_forbidden_input(payload)
        query, body_key = parse_search_request(payload)
        idempotency_key = self._idempotency_key_from(headers, body_key)

        replay = self._replay(
            OPERATION_CREATOR_SEARCH, idempotency_key, payload, request_id, context
        )
        if replay is not None:
            return replay

        self._consume_budget(context)
        if self._sessions is not None:
            self._sessions.require_active()

        response = self._service.search(query, request_id=request_id)
        self._raise_for_upstream(response)

        body_dict = response.to_public_dict()
        self._store_replay(idempotency_key, payload, body_dict)
        self._audit(
            request_id,
            OPERATION_CREATOR_SEARCH,
            OUTCOME_PARTIAL if response.partial else OUTCOME_OK,
            {"rows": len(response.data), "result_kind": response.meta.get("result_kind")},
            context.actor,
        )
        return self._json_response(200, body_dict, request_id)

    def _profiles_resolve(
        self, context: _CallContext, headers: Any, body: bytes, request_id: str
    ) -> ApiResponse:
        context.actor = self._auth.authenticate(headers)
        payload = _read_payload(body)
        assert_no_forbidden_input(payload)
        handles, body_key = parse_resolve_request(payload)
        idempotency_key = self._idempotency_key_from(headers, body_key)

        replay = self._replay(
            OPERATION_PROFILES_RESOLVE, idempotency_key, payload, request_id, context
        )
        if replay is not None:
            return replay

        self._consume_budget(context)

        resolved: list[dict[str, Any]] = []
        unresolved: list[str] = []
        for handle in handles:
            record = self._profile_resolver.resolve(handle)
            if record is None:
                unresolved.append(handle)
            else:
                resolved.append(record)
        if unresolved:
            raise ApiError(
                PROFILE_UNRESOLVED,
                "本地资料无法解析这些 handle；Phase 1 不发起 profile 网络请求",
                details={"unresolved": unresolved},
            )

        body_dict = envelope(
            request_id=request_id,
            data=resolved,
            meta={
                "operation": OPERATION_PROFILES_RESOLVE,
                "resolved_from": "local_creator_store",
                "network_calls": 0,
                "requested": len(handles),
                "resolved": len(resolved),
            },
        )
        self._store_replay(idempotency_key, payload, body_dict)
        self._audit(
            request_id,
            OPERATION_PROFILES_RESOLVE,
            OUTCOME_OK,
            {"requested": len(handles), "resolved": len(resolved)},
            context.actor,
        )
        return self._json_response(200, body_dict, request_id)

    def _session_status(
        self, context: _CallContext, headers: Any, body: bytes, session_id: str, request_id: str
    ) -> ApiResponse:
        context.actor = self._auth.authenticate(headers)
        assert_no_forbidden_input(_read_payload(body))
        self._consume_budget(context)
        status = self._require_sessions().status(session_id)

        body_dict = envelope(
            request_id=request_id,
            data=status,
            pagination=_single_item_pagination(),
            meta={"operation": OPERATION_SESSION_STATUS},
        )
        self._audit(
            request_id,
            OPERATION_SESSION_STATUS,
            OUTCOME_OK,
            {"status": status.get("status")},
            context.actor,
        )
        return self._json_response(200, body_dict, request_id)

    def _session_revoke(
        self, context: _CallContext, headers: Any, body: bytes, session_id: str, request_id: str
    ) -> ApiResponse:
        context.actor = self._auth.authenticate(headers)
        assert_no_forbidden_input(_read_payload(body))
        self._consume_budget(context)
        status = self._require_sessions().revoke(session_id)

        purged: dict[str, int] = {}
        session_id_hash = status.get("session_id_hash")
        if session_id_hash:
            report = self._repository.purge_session(session_id_hash)
            purged = {
                "cache_rows": report.cache_rows,
                "videos": report.videos,
                "provenance_rows": report.provenance_rows,
                "sessions": report.sessions,
            }

        body_dict = envelope(
            request_id=request_id,
            data={**status, "purged": purged},
            pagination=_single_item_pagination(),
            meta={"operation": OPERATION_SESSION_REVOKE},
        )
        self._audit(
            request_id, OPERATION_SESSION_REVOKE, OUTCOME_OK, {"purged": purged}, context.actor
        )
        return self._json_response(200, body_dict, request_id)

    # ---------------------------------------------------------------- 内部件

    def _require_sessions(self) -> Any:
        if self._sessions is None:
            raise ApiError(NOT_FOUND, "未配置会话登记簿：会话路由不可用")
        return self._sessions

    def _consume_budget(self, context: _CallContext) -> None:
        self._budget.consume(context.actor or "anonymous")

    def _idempotency_key_from(self, headers: Any, body_key: str | None) -> str | None:
        header_key = header_value(headers, IDEMPOTENCY_HEADER)
        if header_key:
            return validate_idempotency_key(header_key)
        return body_key

    def _raise_for_upstream(self, response: SearchResponse) -> None:
        """上游完全失败且没有可用结果时升级成错误码；有数据则保持 200 + partial。"""
        if response.data:
            return
        errors = tuple(response.meta.get("transport", {}).get("errors", ()))
        if not errors:
            return
        code = upstream_code_for_kind(str(errors[0].get("code")))
        if code is None:
            return
        raise ApiError(
            code,
            "本轮搜索没有可用结果：上游失败，不把失败说成「没有达人」",
            details={
                "transport_errors": [dict(error) for error in errors],
                "stops": [
                    dict(stop) for stop in response.meta.get("transport", {}).get("stops", ())
                ],
                "result_kind": response.meta.get("result_kind"),
            },
        )

    @contextmanager
    def _audit_scope(self, operation: str, request_id: str) -> Iterator[_CallContext]:
        """保证每次调用都有审计 outcome：成功由处理器自己写，失败在这里写。"""
        context = _CallContext()
        try:
            yield context
        except ApiError as exc:
            outcome = OUTCOME_DENIED if exc.code in DENIAL_CODES else OUTCOME_ERROR
            self._audit(request_id, operation, outcome, {"error_code": exc.code}, context.actor)
            raise
        except Exception as exc:  # noqa: BLE001 - 统一翻译成稳定错误码再记审计
            api_error = map_exception(exc)
            self._audit(
                request_id, operation, OUTCOME_ERROR, {"error_code": api_error.code}, context.actor
            )
            raise api_error from exc

    def _audit(
        self,
        request_id: str,
        operation: str,
        outcome: str,
        detail: Mapping[str, Any] | None = None,
        actor: str | None = None,
    ) -> None:
        # append 语义：api_audit 以 request_id 为主键，DiscoveryService 会用裸
        # request_id 写自己的审计行；API 行改用 ``{request_id}:{operation}``，
        # 两者不冲突，一次调用保留两条审计，关联查询按 ``request_id`` 前缀匹配。
        self._repository.record_audit(
            f"{request_id}:{operation}",
            operation,
            outcome,
            detail=json.dumps(detail, ensure_ascii=False, default=str) if detail else None,
            actor=actor,
        )

    def _request_id(self, headers: Any) -> str:
        provided = header_value(headers, "X-Request-Id")
        if provided and _REQUEST_ID_RE.match(provided):
            return provided
        return self._request_id_factory()

    def _replay(
        self,
        operation: str,
        idempotency_key: str | None,
        payload: Mapping[str, Any],
        request_id: str,
        context: _CallContext,
    ) -> ApiResponse | None:
        if idempotency_key is None:
            return None
        stored = self._idempotency.get(idempotency_key)
        if stored is None:
            return None
        fingerprint, body_dict = stored
        if fingerprint != _payload_fingerprint(payload):
            raise ApiError(
                IDEMPOTENCY_CONFLICT,
                "同一个 idempotency key 不能用于不同的请求体",
                details={"idempotency_key": idempotency_key},
            )
        self._audit(request_id, operation, OUTCOME_OK, {"replay": True}, context.actor)
        return self._json_response(
            200, body_dict, request_id, extra_headers={"Idempotency-Replay": "true"}
        )

    def _store_replay(
        self, idempotency_key: str | None, payload: Mapping[str, Any], body_dict: Mapping[str, Any]
    ) -> None:
        if idempotency_key is None:
            return
        self._idempotency[idempotency_key] = (_payload_fingerprint(payload), dict(body_dict))

    def _json_response(
        self,
        status_code: int,
        payload: Mapping[str, Any],
        request_id: str,
        extra_headers: Mapping[str, str] | None = None,
    ) -> ApiResponse:
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "X-Request-Id": request_id,
            "Cache-Control": "no-store",
        }
        headers.update(extra_headers or {})
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        return ApiResponse(status_code, body, headers)

    def _error_response(self, error: ApiError, request_id: str) -> ApiResponse:
        headers: dict[str, str] = {}
        if error.code == AUTH_REQUIRED:
            headers["WWW-Authenticate"] = "Bearer"
        retry_after = error.details.get("retry_after_seconds")
        if retry_after is not None:
            headers["Retry-After"] = str(retry_after)
        return self._json_response(error.http_status, error.body(request_id), request_id, headers)


# ------------------------------------------------------------------ 工具函数


def _read_payload(body: bytes) -> Any:
    if len(body) > MAX_BODY_BYTES:
        raise ApiError(
            PAYLOAD_TOO_LARGE,
            f"请求体超过 {MAX_BODY_BYTES} 字节上限",
            details={"max_body_bytes": MAX_BODY_BYTES},
        )
    if not body.strip():
        return {}
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ApiError(INVALID_QUERY, "请求体不是合法 JSON") from None
    if not isinstance(payload, Mapping):
        raise ApiError(INVALID_QUERY, "请求体必须是 JSON 对象")
    return payload


def _content_length_violation(headers: Any) -> ApiError | None:
    """Content-Length 读取前闸门：返回 None 表示可以安全读取请求体。

    缺失头放行（GET / 无 body 请求合法）；非整数或负数 → ``INVALID_QUERY``；
    超过 32KiB 上限 → ``PAYLOAD_TOO_LARGE``。校验发生在读取任何字节之前，
    请求体永远不会被超限读入内存。
    """
    raw = header_value(headers, "Content-Length")
    if not raw:
        return None
    try:
        length = int(raw)
    except ValueError:
        length = -1
    if length < 0:
        return ApiError(
            INVALID_QUERY,
            "Content-Length 必须是非负整数",
            details={"field": "content_length"},
        )
    if length > MAX_BODY_BYTES:
        return ApiError(
            PAYLOAD_TOO_LARGE,
            f"Content-Length 超过 {MAX_BODY_BYTES} 字节上限：拒绝读取请求体",
            details={"max_body_bytes": MAX_BODY_BYTES},
        )
    return None


def _payload_fingerprint(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _single_item_pagination() -> dict[str, Any]:
    return {
        "limit": 1,
        "returned": 1,
        "total_matched": 1,
        "has_more": False,
        "cursor": None,
        "next_cursor": None,
    }


def _new_request_id() -> str:
    return f"req-{uuid.uuid4().hex[:16]}"


def _operation_for(path: str) -> str:
    if "creator-search" in path:
        return OPERATION_CREATOR_SEARCH
    if "profiles/resolve" in path:
        return OPERATION_PROFILES_RESOLVE
    if path.endswith("/revoke"):
        return OPERATION_SESSION_REVOKE
    if path.endswith("/status"):
        return OPERATION_SESSION_STATUS
    return OPERATION_UNKNOWN


# --------------------------------------------------------------- http.server


def make_http_handler(app: ApiApp) -> type[BaseHTTPRequestHandler]:
    """生成标准库 ``http.server`` 处理器；日志整体关闭，避免请求行/Key 落盘。"""

    class ScoutApiHandler(BaseHTTPRequestHandler):
        server_version = "ScoutInternalAPI/1.0"
        protocol_version = "HTTP/1.1"

        def _dispatch(self) -> None:
            if _content_length_violation(self.headers) is not None:
                # 闸门判定违规：一个字节都不读，带空 body 走 app.handle，
                # 由统一闸门在 dispatch 内生成 4xx 错误响应并留审计。
                body = b""
            else:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length > 0 else b""
            response = app.handle(self.command, self.path, headers=self.headers, body=body)
            self.send_response(response.status_code)
            for name, value in response.headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(response.body)))
            self.end_headers()
            if response.body:
                self.wfile.write(response.body)

        def do_GET(self) -> None:  # noqa: N802 - http.server 的命名约定
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802 - http.server 的命名约定
            self._dispatch()

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            """不写访问日志：请求行可能带 Key、会话 id 等敏感片段。"""

    return ScoutApiHandler


__all__ = [
    "ApiApp",
    "ApiResponse",
    "RepositoryProfileResolver",
    "VaultSessionProvider",
    "make_http_handler",
]
