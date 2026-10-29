"""API Key 校验、敏感输入拒绝与调用预算。

三条硬约束：

* **Key 只从环境/内部配置读取**，比较走 :func:`hmac.compare_digest`（常量时间），
  且比较的是 SHA-256 摘要——长度也恒定。Key 明文不写日志、不进审计、不回显；
  审计里只有 ``key:<摘要前 12 位>`` 这样的指纹。
* **调用方永远不能提供会话材料或任意上游目标**：键名黑名单 + 凭证形状的值
  黑名单，命中即 ``FORBIDDEN_INPUT``。这是"内部 API 不等于任意抓取代理"的边界。
* **预算只防误用**：按 API Key 限流，请求体里没有任何字段能调高额度或抓取量。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from collections import deque
from typing import Any, Iterable, Mapping

from .errors import AUTH_REQUIRED, FORBIDDEN_INPUT, REQUEST_BUDGET_EXCEEDED, ApiError

ENV_API_KEYS = "SCOUT_API_KEYS"
ENV_API_KEY = "SCOUT_API_KEY"

AUTHORIZATION_HEADER = "Authorization"
BEARER_SCHEME = "bearer"

ACTOR_PREFIX = "key:"

DEFAULT_MAX_CALLS = 60
DEFAULT_WINDOW_SECONDS = 60.0

# ---------------------------------------------------------------- 敏感输入黑名单

FORBIDDEN_FIELD_NAMES = frozenset(
    {
        # 会话材料
        "sessionid",
        "session_id",
        "sessionid_ss",
        "sid_tt",
        "mstoken",
        "ms_token",
        "mstokens",
        "cookie",
        "cookies",
        "cookie_header",
        "cookie_jar",
        "cookiejar",
        "set_cookie",
        "authorization",
        "bearer_token",
        "access_token",
        "refresh_token",
        "raw_token",
        "token_value",
        "storage",
        "storage_state",
        "storagestate",
        # 任意上游目标 / 通道控制
        "arbitrary_url",
        "url",
        "uri",
        "endpoint",
        "endpoint_url",
        "target_url",
        "request_url",
        "api_url",
        "proxy",
        "proxies",
        "proxy_url",
        "headers",
        "raw_headers",
        "http_headers",
        "user_agent",
        "device_id",
        # 签名与指纹
        "signature",
        "sign",
        "x_bogus",
        "xbogus",
        "verifyfp",
        "verify_fp",
        # 其他凭证
        "password",
        "secret",
        "private_key",
    }
)

# 键名里含这些子串同样拒绝（覆盖 ``session_id_value`` 这类变体）
FORBIDDEN_FIELD_MARKERS = (
    "cookie",
    "sessionid",
    "mstoken",
    "ms_token",
    "token",
    "secret",
    "password",
    "x_bogus",
    "verifyfp",
    "proxy",
)

# 值长得像凭证/请求头也拒绝：防止把材料塞进允许的字段里
FORBIDDEN_VALUE_MARKERS = (
    "sessionid=",
    "sessionid_ss=",
    "sid_tt=",
    "mstoken=",
    "ms_token=",
    "set-cookie:",
    "cookie:",
)


# ------------------------------------------------------------------------ 工具


def header_value(headers: Any, name: str) -> str:
    """大小写不敏感地取请求头；兼容 dict 与 ``email.message.Message``。"""
    if not headers:
        return ""
    getter = getattr(headers, "get", None)
    if getter is None:
        return ""
    for candidate in (name, name.lower(), name.title()):
        value = getter(candidate)
        if value:
            return str(value).strip()
    return ""


def actor_fingerprint(api_key: str) -> str:
    """Key 的稳定指纹：只用于审计 ``actor``，不可逆推明文。"""
    digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    return f"{ACTOR_PREFIX}{digest[:12]}"


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def _keys_from_env(environ: Mapping[str, str]) -> tuple[str, ...]:
    keys: list[str] = []
    for raw in str(environ.get(ENV_API_KEYS) or "").split(","):
        key = raw.strip()
        if key:
            keys.append(key)
    single = str(environ.get(ENV_API_KEY) or "").strip()
    if single:
        keys.append(single)
    return tuple(dict.fromkeys(keys))


# ------------------------------------------------------------------- API Key


class ApiKeyAuth:
    """``Authorization: Bearer <key>`` 的常量时间校验（唯一鉴权入口）。"""

    def __init__(
        self,
        api_keys: Iterable[str] | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        keys = tuple(api_keys) if api_keys is not None else _keys_from_env(
            environ if environ is not None else os.environ
        )
        if not keys:
            raise ValueError(
                f"未配置 API Key：设置 {ENV_API_KEYS}（逗号分隔）或 {ENV_API_KEY}，"
                "或显式传入 api_keys"
            )
        self._digests = tuple(_digest(key) for key in keys)

    @property
    def key_count(self) -> int:
        return len(self._digests)

    def authenticate(self, headers: Any) -> str:
        """校验通过返回审计用的 actor 指纹；失败抛 ``AUTH_REQUIRED``。"""
        presented = self._presented_key(headers)
        if not presented:
            raise ApiError(AUTH_REQUIRED, "缺少 API Key：需要 Authorization: Bearer <key>")
        candidate = _digest(presented)
        if not any(hmac.compare_digest(candidate, known) for known in self._digests):
            raise ApiError(AUTH_REQUIRED, "API Key 无效")
        return actor_fingerprint(presented)

    def _presented_key(self, headers: Any) -> str:
        authorization = header_value(headers, AUTHORIZATION_HEADER)
        if authorization:
            scheme, _, token = authorization.partition(" ")
            if scheme.strip().lower() != BEARER_SCHEME or not token.strip():
                raise ApiError(AUTH_REQUIRED, "Authorization 必须是 Bearer <key> 形式")
            return token.strip()
        return ""


# ------------------------------------------------------------- 敏感输入拒绝


def scan_forbidden(payload: Any) -> tuple[str, ...]:
    """递归扫描请求体，返回命中的敏感键名。"""
    hits: list[str] = []
    stack: list[Any] = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, Mapping):
            for key, value in node.items():
                if _forbidden_key(key):
                    hits.append(str(key))
                stack.append(value)
        elif isinstance(node, (list, tuple)):
            stack.extend(node)
        elif isinstance(node, str) and _forbidden_value(node):
            hits.append("<value>")
    return tuple(dict.fromkeys(hits))


def assert_no_forbidden_input(payload: Any) -> None:
    hits = scan_forbidden(payload)
    if hits:
        raise ApiError(
            FORBIDDEN_INPUT,
            "请求包含被禁止的字段：会话材料、任意上游目标与签名输入不接受外部传入",
            details={
                "forbidden_fields": [hit for hit in hits if hit != "<value>"],
                "forbidden_values": any(hit == "<value>" for hit in hits),
            },
        )


def _forbidden_key(key: Any) -> bool:
    normalized = str(key).strip().lower().replace("-", "_")
    if normalized in FORBIDDEN_FIELD_NAMES:
        return True
    return any(marker in normalized for marker in FORBIDDEN_FIELD_MARKERS)


def _forbidden_value(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in FORBIDDEN_VALUE_MARKERS)


# ------------------------------------------------------------------------- 预算


class RequestBudget:
    """按 actor 的滑动窗口预算：只限制调用次数，不提供任何调高抓取量的入口。"""

    def __init__(
        self,
        max_calls: int = DEFAULT_MAX_CALLS,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        *,
        clock: Any = None,
    ) -> None:
        if max_calls < 1 or window_seconds <= 0:
            raise ValueError("max_calls 至少为 1，window_seconds 必须为正")
        self.max_calls = int(max_calls)
        self.window_seconds = float(window_seconds)
        self._now = clock or time.monotonic
        self._hits: dict[str, deque[float]] = {}

    def consume(self, actor: str) -> None:
        now = float(self._now())
        hits = self._hits.setdefault(actor, deque())
        while hits and now - hits[0] >= self.window_seconds:
            hits.popleft()
        if len(hits) >= self.max_calls:
            retry_after = max(1, int(self.window_seconds - (now - hits[0])))
            raise ApiError(
                REQUEST_BUDGET_EXCEEDED,
                "调用预算已用尽：本 API 仅用于内部低频调用，不提供提额入口",
                details={
                    "max_calls": self.max_calls,
                    "window_seconds": self.window_seconds,
                    "retry_after_seconds": retry_after,
                },
            )
        hits.append(now)


__all__ = [
    "AUTHORIZATION_HEADER",
    "ApiKeyAuth",
    "FORBIDDEN_FIELD_MARKERS",
    "FORBIDDEN_FIELD_NAMES",
    "FORBIDDEN_VALUE_MARKERS",
    "RequestBudget",
    "actor_fingerprint",
    "assert_no_forbidden_input",
    "header_value",
    "scan_forbidden",
]
