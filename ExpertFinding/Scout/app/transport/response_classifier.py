"""Transport 响应分类：成功 / 软冷却 / WAF 挑战 / 认证失败 / schema 变化。

输入只有合成的 ``status`` + ``body`` 文本，不触网、不接触会话与凭证。

分类顺序（Phase 1 实测约束）：
1. 非 JSON 的挑战页（含 slardar/captcha 标记）→ ``waf_challenge``，绝不重试；
2. 按状态码给出稳定语义：401/403/429/5xx；
3. 2xx + 空 body → ``cooling``（软冷却，可有限退避重试，不是成功）；
4. 其余 → ``success``，由下游解码；解码不了则由 :func:`decode_json`
   抛出 :class:`SchemaChangedError`。

``TransportOutcome.detail`` 只描述类别，**不回显 body**，避免日志泄漏会话材料或挑战页内容。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

SUCCESS = "success"
COOLING = "cooling"
WAF_CHALLENGE = "waf_challenge"
AUTH_FAILED = "auth_failed"
FORBIDDEN = "forbidden"
RATE_LIMITED = "rate_limited"
UPSTREAM_UNAVAILABLE = "upstream_unavailable"
UPSTREAM_REJECTED = "upstream_rejected"
SCHEMA_MISMATCH = "schema_mismatch"
UNEXPECTED_STATUS = "unexpected_status"

# 只对非 JSON 响应体生效：正常搜索结果是 JSON，不会被误判为挑战页
WAF_BODY_MARKERS = ("slardar", "captcha")

DETAIL_WAF = "WAF 挑战页：停止本轮搜索并交人工处理，禁止重试绕过"
DETAIL_COOLING = "响应体为空：判定为软冷却，进入有限退避"
DETAIL_AUTH = "会话失效或被登出（401），需重新提取会话"
DETAIL_FORBIDDEN = "出口/指纹被拒绝（403），停止本轮搜索"
DETAIL_RATE_LIMITED = "触发限流（429），退避后再决定是否重试"
DETAIL_UPSTREAM = "上游服务不可用（5xx），稍后重试"
DETAIL_UNEXPECTED = "未预期的状态码，停止本轮搜索"


@dataclass(frozen=True)
class RawReply:
    """一次 HTTP 响应的合成表示：只有状态码和响应体文本。"""

    status: int
    body: str = ""


@dataclass(frozen=True)
class TransportOutcome:
    kind: str
    retryable: bool
    detail: str = ""


class TransportError(RuntimeError):
    """所有 transport 层异常的基类，便于上层统一捕获。"""

    kind = UNEXPECTED_STATUS
    retryable = False


class WafChallengeError(TransportError):
    kind = WAF_CHALLENGE
    retryable = False


class UpstreamCoolingError(TransportError):
    kind = COOLING
    retryable = True


class AuthFailedError(TransportError):
    kind = AUTH_FAILED
    retryable = False


class ForbiddenError(TransportError):
    kind = FORBIDDEN
    retryable = False


class RateLimitedError(TransportError):
    kind = RATE_LIMITED
    retryable = True


class UpstreamUnavailableError(TransportError):
    kind = UPSTREAM_UNAVAILABLE
    retryable = True


class UpstreamRejectedError(TransportError):
    """HTTP 层成功，但上游业务 status_code 非 0。"""

    kind = UPSTREAM_REJECTED
    retryable = False


class SchemaChangedError(TransportError):
    kind = SCHEMA_MISMATCH
    retryable = False


EXCEPTION_BY_KIND: Mapping[str, type[TransportError]] = {
    WAF_CHALLENGE: WafChallengeError,
    COOLING: UpstreamCoolingError,
    AUTH_FAILED: AuthFailedError,
    FORBIDDEN: ForbiddenError,
    RATE_LIMITED: RateLimitedError,
    UPSTREAM_UNAVAILABLE: UpstreamUnavailableError,
    UPSTREAM_REJECTED: UpstreamRejectedError,
    SCHEMA_MISMATCH: SchemaChangedError,
    UNEXPECTED_STATUS: TransportError,
}


def raise_for_outcome(outcome: TransportOutcome) -> None:
    """把分类结论翻译成稳定异常。success 不应调用本函数。"""
    exc_cls = EXCEPTION_BY_KIND.get(outcome.kind, TransportError)
    raise exc_cls(outcome.detail or outcome.kind)


def classify_response(status: int, body: str | bytes = "") -> TransportOutcome:
    """把 (status, body) 映射成稳定的 TransportOutcome。"""
    text = body if isinstance(body, str) else body.decode("utf-8", "replace")
    stripped = text.strip()

    if stripped and not _looks_like_json(stripped) and _has_waf_marker(stripped):
        return TransportOutcome(WAF_CHALLENGE, False, DETAIL_WAF)

    if status == 401:
        return TransportOutcome(AUTH_FAILED, False, DETAIL_AUTH)
    if status == 403:
        return TransportOutcome(FORBIDDEN, False, DETAIL_FORBIDDEN)
    if status == 429:
        return TransportOutcome(RATE_LIMITED, True, DETAIL_RATE_LIMITED)
    if status >= 500:
        return TransportOutcome(UPSTREAM_UNAVAILABLE, True, DETAIL_UPSTREAM)

    if status in (200, 204) and not stripped:
        return TransportOutcome(COOLING, True, DETAIL_COOLING)

    if 200 <= status < 300:
        return TransportOutcome(SUCCESS, False, "响应可用")

    return TransportOutcome(UNEXPECTED_STATUS, False, DETAIL_UNEXPECTED)


def decode_json(body: str) -> Any:
    """解码 JSON；非 JSON 响应体视为上游 schema 变化。"""
    try:
        return json.loads(body)
    except (json.JSONDecodeError, TypeError) as exc:
        raise SchemaChangedError("响应体不是合法 JSON：上游 schema 变化") from exc


def classify_payload(payload: Any) -> TransportOutcome:
    """检查已解码 payload 的契约：必须有 status_code 与 data。"""
    if not isinstance(payload, Mapping):
        return TransportOutcome(SCHEMA_MISMATCH, False,
                                "响应体顶层不是对象：上游 schema 变化")
    if "status_code" not in payload or "data" not in payload:
        return TransportOutcome(SCHEMA_MISMATCH, False,
                                "响应缺少 data/status_code 字段：上游 schema 变化")
    if payload.get("status_code") != 0:
        return TransportOutcome(
            UPSTREAM_REJECTED, False,
            f"上游返回非零 status_code={payload.get('status_code')}")
    return TransportOutcome(SUCCESS, False, "响应可用")


def _looks_like_json(text: str) -> bool:
    return text[0] in "{[" if text else False


def _has_waf_marker(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in WAF_BODY_MARKERS)
