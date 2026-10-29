"""外部请求/响应 DTO：校验与映射，和数据库模型分离。

请求侧只接受白名单字段，所有边界（请求大小、关键词数量、limit、idempotency
key）在这里收敛成 ``INVALID_QUERY``；响应侧只做信封拼装，领域对象由
``DiscoveryService`` 自己投影成公共行。
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from app.discovery.service import DEFAULT_LIMIT, SearchQuery
from app.domain.normalization import normalize_username

from .errors import INVALID_QUERY, ApiError

SCHEMA_VERSION = "1.0"

# ------------------------------------------------------------------- 边界常量

MAX_BODY_BYTES = 32 * 1024
MAX_KEYWORDS = 10
MAX_KEYWORD_LENGTH = 100
MAX_REGION_CODES = 10
MIN_LIMIT = 1
MAX_LIMIT = 50
MAX_RESOLVE_INPUTS = 25
MIN_IDEMPOTENCY_KEY_LENGTH = 8
MAX_IDEMPOTENCY_KEY_LENGTH = 128
MAX_CURSOR_LENGTH = 512
MAX_SESSION_ID_LENGTH = 128

SEARCH_REQUEST_FIELDS = frozenset(
    {
        "keywords",
        "hashtags",
        "exclude_keywords",
        "region_codes",
        "min_followers",
        "max_followers",
        "min_views",
        "min_relevance_score",
        "limit",
        "cursor",
        "idempotency_key",
        # 兼容分页包装：Phase 1 只认 page.limit，不提供跨请求续传
        "page",
    }
)

PAGE_REQUEST_FIELDS = frozenset({"limit"})

RESOLVE_REQUEST_FIELDS = frozenset({"usernames", "profiles", "idempotency_key"})

_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
_REGION_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{2,8}$")


# ------------------------------------------------------------------- 请求解析


def parse_search_request(payload: Mapping[str, Any]) -> tuple[SearchQuery, str | None]:
    """HTTP 请求体 → :class:`SearchQuery` + idempotency key。"""
    if not isinstance(payload, Mapping):
        raise ApiError(INVALID_QUERY, "请求体必须是 JSON 对象")
    _reject_unknown_fields(payload, SEARCH_REQUEST_FIELDS)

    keywords = _string_list(
        payload.get("keywords"),
        field="keywords",
        required=True,
        max_items=MAX_KEYWORDS,
        max_length=MAX_KEYWORD_LENGTH,
    )
    hashtags = _string_list(
        payload.get("hashtags"), field="hashtags", max_items=MAX_KEYWORDS
    )
    exclude_keywords = _string_list(
        payload.get("exclude_keywords"), field="exclude_keywords", max_items=MAX_KEYWORDS
    )
    region_codes = _region_codes(payload.get("region_codes"))

    query = SearchQuery(
        keywords=keywords,
        hashtags=hashtags,
        exclude_keywords=exclude_keywords,
        region_codes=region_codes,
        min_followers=_optional_int(payload.get("min_followers"), field="min_followers"),
        max_followers=_optional_int(payload.get("max_followers"), field="max_followers"),
        min_views=_optional_int(payload.get("min_views"), field="min_views"),
        min_relevance_score=_optional_score(
            payload.get("min_relevance_score"), field="min_relevance_score"
        ),
        limit=_limit(_flatten_page_limit(payload)),
        cursor=_optional_str(payload.get("cursor"), field="cursor", max_length=MAX_CURSOR_LENGTH),
    )
    return query, _idempotency_key(payload.get("idempotency_key"))


def parse_resolve_request(payload: Mapping[str, Any]) -> tuple[tuple[str, ...], str | None]:
    """解析 username / profile URL，返回规范化后的 handle 列表。

    只做本地补全：解析不出 handle 直接报错，绝不把 URL 变成上游请求。
    """
    if not isinstance(payload, Mapping):
        raise ApiError(INVALID_QUERY, "请求体必须是 JSON 对象")
    _reject_unknown_fields(payload, RESOLVE_REQUEST_FIELDS)

    usernames = _string_list(
        payload.get("usernames"), field="usernames", max_items=MAX_RESOLVE_INPUTS
    )
    profiles = _string_list(
        payload.get("profiles"), field="profiles", max_items=MAX_RESOLVE_INPUTS
    )
    if not usernames and not profiles:
        raise ApiError(
            INVALID_QUERY, "usernames 或 profiles 至少要有一个", details={"field": "usernames"}
        )

    handles: list[str] = []
    for source, field in ((usernames, "usernames"), (profiles, "profiles")):
        for value in source:
            handle = normalize_username(value)
            if not handle:
                raise ApiError(
                    INVALID_QUERY,
                    f"{field} 里有无法解析为 TikTok username 的取值",
                    details={"field": field},
                )
            handles.append(handle)
    if len(handles) > MAX_RESOLVE_INPUTS:
        raise ApiError(
            INVALID_QUERY,
            f"单次最多解析 {MAX_RESOLVE_INPUTS} 个 handle",
            details={"field": "usernames", "max": MAX_RESOLVE_INPUTS},
        )
    return tuple(dict.fromkeys(handles)), _idempotency_key(payload.get("idempotency_key"))


def validate_session_id(value: str) -> str:
    """路径里的 session_id：不做百分号解码，只接受安全字符集。"""
    if not isinstance(value, str) or not value:
        raise ApiError(INVALID_QUERY, "session_id 缺失", details={"field": "session_id"})
    if len(value) > MAX_SESSION_ID_LENGTH or not _SESSION_ID_RE.match(value):
        raise ApiError(
            INVALID_QUERY,
            "session_id 只允许 [A-Za-z0-9._:-]",
            details={"field": "session_id"},
        )
    return value


def validate_idempotency_key(value: Any) -> str:
    return _idempotency_key(value)


# ------------------------------------------------------------------- 响应信封


def envelope(
    *,
    request_id: str,
    data: Any,
    pagination: Mapping[str, Any] | None = None,
    meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """所有 2xx 响应共用 ``schema_version / request_id / data / pagination / meta``。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "request_id": request_id,
        "data": data,
        "pagination": dict(pagination or _pagination(len(data) if isinstance(data, (list, tuple)) else 0)),
        "meta": dict(meta or {}),
    }


def _pagination(count: int) -> dict[str, Any]:
    return {
        "limit": count,
        "returned": count,
        "total_matched": count,
        "has_more": False,
        "cursor": None,
        "next_cursor": None,
    }


# ---------------------------------------------------------------------- 校验


def _reject_unknown_fields(payload: Mapping[str, Any], allowed: frozenset) -> None:
    unknown = sorted(str(key) for key in payload if str(key) not in allowed)
    if unknown:
        raise ApiError(
            INVALID_QUERY,
            "请求包含未知字段",
            details={"unknown_fields": unknown, "allowed_fields": sorted(allowed)},
        )


def _string_list(
    value: Any,
    *,
    field: str,
    required: bool = False,
    max_items: int,
    max_length: int | None = None,
) -> tuple[str, ...]:
    if value is None:
        if required:
            raise ApiError(INVALID_QUERY, f"{field} 必填", details={"field": field})
        return ()
    if not isinstance(value, (list, tuple)):
        raise ApiError(INVALID_QUERY, f"{field} 必须是数组", details={"field": field})
    if required and not value:
        raise ApiError(INVALID_QUERY, f"{field} 不能为空", details={"field": field})
    if len(value) > max_items:
        raise ApiError(
            INVALID_QUERY,
            f"{field} 最多 {max_items} 项",
            details={"field": field, "max": max_items},
        )
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ApiError(
                INVALID_QUERY, f"{field} 必须是非空字符串数组", details={"field": field}
            )
        text = item.strip()
        if max_length is not None and len(text) > max_length:
            raise ApiError(
                INVALID_QUERY,
                f"{field} 单项最长 {max_length} 个字符",
                details={"field": field, "max_length": max_length},
            )
        items.append(text)
    return tuple(items)


def _region_codes(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or len(value) > MAX_REGION_CODES:
        raise ApiError(
            INVALID_QUERY,
            f"region_codes 必须是数组且最多 {MAX_REGION_CODES} 项",
            details={"field": "region_codes"},
        )
    codes: list[str] = []
    for item in value:
        if not isinstance(item, str) or not _REGION_CODE_RE.match(item.strip()):
            raise ApiError(
                INVALID_QUERY, "region_codes 取值不合法", details={"field": "region_codes"}
            )
        codes.append(item.strip().upper())
    return tuple(dict.fromkeys(codes))


def _flatten_page_limit(payload: Mapping[str, Any]) -> Any:
    """``{"page": {"limit": 20}}`` 与 ``{"limit": 20}`` 等价；两者同传必须一致。

    ``page`` 里只认 ``limit``：Phase 1 不提供 cursor/offset 续传，出现即拒绝。
    """
    page = payload.get("page")
    if page is None:
        return payload.get("limit")
    if not isinstance(page, Mapping):
        raise ApiError(INVALID_QUERY, "page 必须是对象", details={"field": "page"})
    unknown = sorted(str(key) for key in page if str(key) not in PAGE_REQUEST_FIELDS)
    if unknown:
        raise ApiError(
            INVALID_QUERY,
            "page 只支持 limit：Phase 1 不提供跨请求续传",
            details={"field": "page", "unknown_fields": unknown},
        )
    page_limit = page.get("limit")
    top_level = payload.get("limit")
    if page_limit is not None and top_level is not None and page_limit != top_level:
        raise ApiError(
            INVALID_QUERY, "limit 与 page.limit 不一致", details={"field": "limit"}
        )
    return page_limit if page_limit is not None else top_level


def _limit(value: Any) -> int:
    if value is None:
        return DEFAULT_LIMIT
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError(INVALID_QUERY, "limit 必须是整数", details={"field": "limit"})
    if not MIN_LIMIT <= value <= MAX_LIMIT:
        raise ApiError(
            INVALID_QUERY,
            f"limit 必须在 [{MIN_LIMIT}, {MAX_LIMIT}] 之间，不能通过请求调高抓取量",
            details={"field": "limit", "min": MIN_LIMIT, "max": MAX_LIMIT},
        )
    return value


def _optional_int(value: Any, *, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ApiError(
            INVALID_QUERY, f"{field} 必须是非负整数", details={"field": field}
        )
    return value


def _optional_score(value: Any, *, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ApiError(INVALID_QUERY, f"{field} 必须是数值", details={"field": field})
    score = float(value)
    if not 0.0 <= score <= 1.0:
        raise ApiError(
            INVALID_QUERY, f"{field} 必须在 [0, 1] 之间", details={"field": field}
        )
    return score


def _optional_str(value: Any, *, field: str, max_length: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApiError(INVALID_QUERY, f"{field} 必须是字符串", details={"field": field})
    text = value.strip()
    if len(text) > max_length:
        raise ApiError(
            INVALID_QUERY,
            f"{field} 最长 {max_length} 个字符",
            details={"field": field},
        )
    return text or None


def _idempotency_key(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApiError(
            INVALID_QUERY, "idempotency_key 必须是字符串", details={"field": "idempotency_key"}
        )
    key = value.strip()
    if not MIN_IDEMPOTENCY_KEY_LENGTH <= len(key) <= MAX_IDEMPOTENCY_KEY_LENGTH:
        raise ApiError(
            INVALID_QUERY,
            f"idempotency_key 长度必须在 "
            f"[{MIN_IDEMPOTENCY_KEY_LENGTH}, {MAX_IDEMPOTENCY_KEY_LENGTH}] 之间",
            details={"field": "idempotency_key"},
        )
    if not _IDEMPOTENCY_KEY_RE.match(key):
        raise ApiError(
            INVALID_QUERY,
            "idempotency_key 只允许 [A-Za-z0-9._:-]",
            details={"field": "idempotency_key"},
        )
    return key


__all__ = [
    "MAX_BODY_BYTES",
    "MAX_IDEMPOTENCY_KEY_LENGTH",
    "MAX_KEYWORDS",
    "MAX_KEYWORD_LENGTH",
    "MAX_LIMIT",
    "MAX_RESOLVE_INPUTS",
    "MAX_SESSION_ID_LENGTH",
    "MIN_LIMIT",
    "RESOLVE_REQUEST_FIELDS",
    "SCHEMA_VERSION",
    "SEARCH_REQUEST_FIELDS",
    "envelope",
    "parse_resolve_request",
    "parse_search_request",
    "validate_idempotency_key",
    "validate_session_id",
]
