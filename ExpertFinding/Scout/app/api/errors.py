"""稳定错误码与 HTTP 状态映射。

错误码是 API 的公共契约：上游实现的任何变化都不得改变这里的取值。
上游异常（transport / 会话撤回）一律在这里翻译成错误码，避免把
``WafChallengeError``、``sqlite3.Error`` 之类的实现细节泄漏给调用方。
"""

from __future__ import annotations

from typing import Any, Mapping

SCHEMA_VERSION = "1.0"

# ------------------------------------------------------------------ 契约错误码

AUTH_REQUIRED = "AUTH_REQUIRED"
FORBIDDEN_INPUT = "FORBIDDEN_INPUT"
INVALID_QUERY = "INVALID_QUERY"
SESSION_REAUTH_REQUIRED = "SESSION_REAUTH_REQUIRED"
SESSION_REVOKED = "SESSION_REVOKED"
UPSTREAM_WAF_CHALLENGE = "UPSTREAM_WAF_CHALLENGE"
UPSTREAM_RATE_LIMITED = "UPSTREAM_RATE_LIMITED"
UPSTREAM_SCHEMA_CHANGED = "UPSTREAM_SCHEMA_CHANGED"
REQUEST_BUDGET_EXCEEDED = "REQUEST_BUDGET_EXCEEDED"

# ------------------------------------------------------------ 契约之外的补充码

NOT_FOUND = "NOT_FOUND"
METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
PROFILE_UNRESOLVED = "PROFILE_UNRESOLVED"
UPSTREAM_REJECTED = "UPSTREAM_REJECTED"
UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
INTERNAL_ERROR = "INTERNAL_ERROR"

STATUS_BY_CODE: Mapping[str, int] = {
    AUTH_REQUIRED: 401,
    FORBIDDEN_INPUT: 400,
    INVALID_QUERY: 400,
    SESSION_REAUTH_REQUIRED: 401,
    SESSION_REVOKED: 409,
    UPSTREAM_WAF_CHALLENGE: 503,
    UPSTREAM_RATE_LIMITED: 429,
    UPSTREAM_SCHEMA_CHANGED: 502,
    REQUEST_BUDGET_EXCEEDED: 429,
    NOT_FOUND: 404,
    METHOD_NOT_ALLOWED: 405,
    PAYLOAD_TOO_LARGE: 413,
    IDEMPOTENCY_CONFLICT: 409,
    PROFILE_UNRESOLVED: 404,
    UPSTREAM_REJECTED: 502,
    UPSTREAM_UNAVAILABLE: 503,
    INTERNAL_ERROR: 500,
}

# 被拒绝的调用（鉴权/输入/预算）单独记 outcome，便于和上游故障区分
DENIAL_CODES = frozenset(
    {
        AUTH_REQUIRED,
        FORBIDDEN_INPUT,
        INVALID_QUERY,
        REQUEST_BUDGET_EXCEEDED,
        PAYLOAD_TOO_LARGE,
        IDEMPOTENCY_CONFLICT,
        METHOD_NOT_ALLOWED,
    }
)

CODE_BY_TRANSPORT_KIND: dict[str, str] = {}


class ApiError(Exception):
    """带稳定错误码的 API 异常；``details`` 只放可公开的结构化信息。"""

    def __init__(
        self, code: str, message: str, *, details: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})
        self.http_status = STATUS_BY_CODE.get(code, 500)

    def body(self, request_id: str = "") -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "request_id": request_id,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }


def map_exception(exc: BaseException) -> ApiError:
    """把上游异常翻译成稳定错误码；无法识别的一律降级为 INTERNAL_ERROR。

    翻译时不回显异常原文：上游细节可能含会话材料或挑战页内容。
    """
    from app.governance.deletion import SessionRevokedError as GovernanceRevokedError
    from app.sessions.revoke import (
        SessionExpiredError,
        SessionNotFoundError,
        SessionRevokedError,
    )
    from app.transport.response_classifier import (
        AuthFailedError,
        ForbiddenError,
        RateLimitedError,
        SchemaChangedError,
        UpstreamCoolingError,
        UpstreamUnavailableError,
        UpstreamRejectedError,
        WafChallengeError,
    )

    if isinstance(exc, ApiError):
        return exc
    if isinstance(exc, (SessionRevokedError, GovernanceRevokedError)):
        return ApiError(SESSION_REVOKED, "会话已撤回：拒绝继续采集，请重新提取会话")
    if isinstance(exc, (SessionExpiredError, AuthFailedError)):
        return ApiError(SESSION_REAUTH_REQUIRED, "会话已失效：需要重新提取后才能继续")
    if isinstance(exc, SessionNotFoundError):
        return ApiError(NOT_FOUND, "会话未登记")
    if isinstance(exc, WafChallengeError):
        return ApiError(UPSTREAM_WAF_CHALLENGE, "上游返回 WAF 挑战：本轮停止，不重试绕过")
    if isinstance(exc, (RateLimitedError, UpstreamCoolingError)):
        return ApiError(UPSTREAM_RATE_LIMITED, "上游限流或软冷却：请稍后再试")
    if isinstance(exc, SchemaChangedError):
        return ApiError(UPSTREAM_SCHEMA_CHANGED, "上游响应结构变化：需重新核对解析契约")
    if isinstance(exc, ForbiddenError):
        return ApiError(UPSTREAM_REJECTED, "上游拒绝了本次请求（出口/指纹），本轮停止")
    if isinstance(exc, UpstreamRejectedError):
        return ApiError(UPSTREAM_REJECTED, "上游返回非零状态：本轮停止")
    if isinstance(exc, UpstreamUnavailableError):
        return ApiError(UPSTREAM_UNAVAILABLE, "上游不可用：请稍后再试")
    return ApiError(INTERNAL_ERROR, "内部错误：已记录审计，详情不回显")


def _register_transport_mapping() -> None:
    from app.transport.response_classifier import (
        AUTH_FAILED,
        COOLING,
        FORBIDDEN,
        RATE_LIMITED,
        SCHEMA_MISMATCH,
        UNEXPECTED_STATUS,
        UPSTREAM_REJECTED as KIND_REJECTED,
        UPSTREAM_UNAVAILABLE as KIND_UNAVAILABLE,
        WAF_CHALLENGE,
    )

    mapping: dict[str, str] = {
        WAF_CHALLENGE: UPSTREAM_WAF_CHALLENGE,
        RATE_LIMITED: UPSTREAM_RATE_LIMITED,
        COOLING: UPSTREAM_RATE_LIMITED,
        SCHEMA_MISMATCH: UPSTREAM_SCHEMA_CHANGED,
        # 基类 TransportError 的默认 kind：上游返回了分类器无法归类的状态码，
        # 响应已超出已识别契约，语义最接近「契约变化需人工核对」，同样不可重试。
        UNEXPECTED_STATUS: UPSTREAM_SCHEMA_CHANGED,
        AUTH_FAILED: SESSION_REAUTH_REQUIRED,
        FORBIDDEN: UPSTREAM_REJECTED,
        KIND_REJECTED: UPSTREAM_REJECTED,
        KIND_UNAVAILABLE: UPSTREAM_UNAVAILABLE,
    }
    CODE_BY_TRANSPORT_KIND.update(mapping)


_register_transport_mapping()


def upstream_code_for_kind(kind: str) -> str | None:
    """transport 的失败类别 → API 错误码；未知类别返回 None（保持 200 + partial）。"""
    return CODE_BY_TRANSPORT_KIND.get(kind)


__all__ = [
    "AUTH_REQUIRED",
    "CODE_BY_TRANSPORT_KIND",
    "DENIAL_CODES",
    "FORBIDDEN_INPUT",
    "IDEMPOTENCY_CONFLICT",
    "INTERNAL_ERROR",
    "INVALID_QUERY",
    "METHOD_NOT_ALLOWED",
    "NOT_FOUND",
    "PAYLOAD_TOO_LARGE",
    "PROFILE_UNRESOLVED",
    "REQUEST_BUDGET_EXCEEDED",
    "SCHEMA_VERSION",
    "SESSION_REAUTH_REQUIRED",
    "SESSION_REVOKED",
    "STATUS_BY_CODE",
    "UPSTREAM_RATE_LIMITED",
    "UPSTREAM_REJECTED",
    "UPSTREAM_SCHEMA_CHANGED",
    "UPSTREAM_UNAVAILABLE",
    "UPSTREAM_WAF_CHALLENGE",
    "ApiError",
    "map_exception",
    "upstream_code_for_kind",
]
