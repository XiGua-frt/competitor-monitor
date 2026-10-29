"""app.transport：TikTok 外网只读搜索的传输策略层。

- response_classifier：成功 / 软冷却 / WAF / 认证 / schema 的稳定分类与异常；
- pagination：cursor/has_more 推进、重复页停滞检测、有上限的冷却退避；
- public_readonly：对外受控适配层，只暴露只读搜索，不泄漏会话材料。
"""

from .response_classifier import (
    COOLING,
    SUCCESS,
    WAF_CHALLENGE,
    RawReply,
    TransportError,
    TransportOutcome,
    WafChallengeError,
    classify_payload,
    classify_response,
    decode_json,
    raise_for_outcome,
)
from .pagination import (
    COOLING_STOPPED,
    COMPLETED,
    HARD_MAX_RESULTS,
    PAGINATION_STALLED,
    PAGINATION_STOPPED,
    ReadonlySearchPaginator,
    SearchPage,
    SearchRun,
)
from .public_readonly import READONLY_OPERATIONS, PublicReadonlySearch

__all__ = [
    "COOLING",
    "SUCCESS",
    "WAF_CHALLENGE",
    "RawReply",
    "TransportError",
    "TransportOutcome",
    "WafChallengeError",
    "classify_payload",
    "classify_response",
    "decode_json",
    "raise_for_outcome",
    "COOLING_STOPPED",
    "COMPLETED",
    "HARD_MAX_RESULTS",
    "PAGINATION_STALLED",
    "PAGINATION_STOPPED",
    "ReadonlySearchPaginator",
    "SearchPage",
    "SearchRun",
    "READONLY_OPERATIONS",
    "PublicReadonlySearch",
]
