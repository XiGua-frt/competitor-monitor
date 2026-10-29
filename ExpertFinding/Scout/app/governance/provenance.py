"""来源、授权依据、采集时间与 payload hash 的治理层（auxiliary / validation only）。

职责边界：
- parser（app/domain/parser.py）**生成** Provenance，包括 raw payload 的 SHA-256 指纹；
- 本模块**不重复生成**，只对既有记录做校验、归一化描述和敏感字段拦截，
  供 storage 在写入前调用。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Mapping

from app.domain.normalization import is_hashed_session_id
from app.domain.parser import canonical_payload_hash

# 采集来源：Web 只读搜索通道（与 app/transport/public_readonly.py 的 name 一致）
SOURCE_TIKTOK_WEB = "tiktok_web"

# 授权依据：只采集公开搜索结果自爬，不含登录态私有数据
LEGAL_BASIS_PUBLIC_SEARCH_SELF_CRAWL = "public_search_result_self_crawl"

# 与 app/signing/versions.py NoopSigner.version 保持一致（此处不反向依赖 signing）
DEFAULT_SIGNER_VERSION = "none-v1"
DEFAULT_SCORE_VERSION = "none-v1"

PAYLOAD_HASH_HEX_LENGTH = 64

# 任何写库路径都不允许出现的字段/子串。session 只允许以不可逆 hash 形式出现，
# 因此这里匹配的是明文形态（sessionid），不含 `session_id_hash` 这类列名。
SENSITIVE_FIELD_MARKERS = (
    "sessionid",
    "session_id_value",
    "mstoken",
    "ms_token",
    "mstokens",
    "set-cookie",
    "set_cookie",
    "cookie",
    "cookies",
    "cookies_json",
    "cookie_jar",
    "cookiejar",
    "storage_state",
    "storagestate",
    "authorization",
    "password",
    "secret",
    "verifyfp",
    "verify_fp",
    "x_bogus",
    "raw_token",
    "token_value",
)

# ``cookie`` 是普通英文单词的一部分（cookiequeen / cookieless），子串匹配会误伤
# 正常数据；但 `Cookie:` / `Set-Cookie:` / `"cookie": "..."` 必须拦下，所以这个
# 标记改按整词匹配（``-``、``:``、``,``、``"`` 都算边界，字母数字不算）。
# 代价：``cookiejar`` 这类连写变体匹配不到，因此上面额外保留了它的子串标记。
WORD_BOUNDARY_MARKERS = ("cookie",)


class ProvenanceError(ValueError):
    """来源记录不合法或被检出敏感字段。"""


def describe_source(source: str) -> tuple[str, str]:
    """返回 (通道家系, 具体端点)。端点来源统一归到 ``tiktok_web`` 通道下。"""
    if not source:
        return SOURCE_TIKTOK_WEB, ""
    if source.startswith("tiktok"):
        return SOURCE_TIKTOK_WEB, source
    return source, source


def is_payload_hash(value: str) -> bool:
    if not isinstance(value, str) or len(value) != PAYLOAD_HASH_HEX_LENGTH:
        return False
    return all(char in "0123456789abcdefABCDEF" for char in value)


def is_collected_at(value: str) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def payload_hash_matches(provenance: Any, payload: Any) -> bool:
    """用原始 payload 复核 Provenance 上的指纹，防止记录被改写。"""
    return canonical_payload_hash(payload) == provenance.raw_payload_hash


def verify_provenance(provenance: Any) -> bool:
    """单次来源记录的自洽性校验：hash 形态、会话引用形态、采集时间。"""
    try:
        return (
            is_payload_hash(provenance.raw_payload_hash)
            and is_hashed_session_id(provenance.session_id_hash)
            and is_collected_at(provenance.collected_at)
            and bool(provenance.source)
            and bool(provenance.entity_id)
        )
    except AttributeError:
        return False


def fingerprint_session(session_id_hash: str) -> str:
    """会话指纹二次压缩（用于展示），输入必须已经是 sha256: 形式。"""
    if not is_hashed_session_id(session_id_hash):
        raise ProvenanceError("session reference must already be hashed")
    return hashlib.sha256(session_id_hash.encode("utf-8")).hexdigest()[:12]


def find_sensitive_fields(record: Mapping[str, Any] | str) -> tuple[str, ...]:
    """扫描待写库的 mapping 或 JSON 文本，返回命中的敏感标记。"""
    if isinstance(record, str):
        return find_sensitive_fields_in_text(record)
    return find_sensitive_fields_in_mapping(record)


def find_sensitive_fields_in_mapping(record: Mapping[str, Any]) -> tuple[str, ...]:
    hits: list[str] = []
    stack: list[Any] = [record]
    while stack:
        node = stack.pop()
        if isinstance(node, Mapping):
            for key, value in node.items():
                if _matches_marker(str(key)):
                    hits.append(str(key))
                stack.append(value)
        elif isinstance(node, (list, tuple)):
            stack.extend(node)
    return tuple(dict.fromkeys(hits))


def find_sensitive_fields_in_text(text: str) -> tuple[str, ...]:
    lowered = text.lower()
    hits = [marker for marker in SENSITIVE_FIELD_MARKERS if _marker_matches(lowered, marker)]

    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, Mapping):
        hits.extend(find_sensitive_fields_in_mapping(parsed))
    elif isinstance(parsed, list):
        hits.extend(find_sensitive_fields_in_mapping({"payload": parsed}))
    return tuple(dict.fromkeys(hits))


def assert_clean_record(record: Mapping[str, Any] | str, *, context: str = "record") -> None:
    """写入前的最后一道闸门：命中敏感字段直接拒绝，不静默清洗。"""
    hits = find_sensitive_fields(record)
    if hits:
        raise ProvenanceError(
            f"{context} carries sensitive session material: {', '.join(hits)}"
        )


def _matches_marker(key: str) -> bool:
    return any(_marker_matches(key, marker) for marker in SENSITIVE_FIELD_MARKERS)


def _marker_matches(text: str, marker: str) -> bool:
    """子串匹配，除非该标记声明了整词匹配（见 ``WORD_BOUNDARY_MARKERS``）。"""
    pattern = _WORD_BOUNDARY_PATTERNS.get(marker)
    if pattern is None:
        return marker.lower() in text.lower()
    return bool(pattern.search(text))


def _compile_word_boundary_markers() -> dict[str, Any]:
    return {
        marker: re.compile(
            rf"(?<![0-9A-Za-z]){re.escape(marker)}(?![0-9A-Za-z])", re.IGNORECASE
        )
        for marker in WORD_BOUNDARY_MARKERS
    }


_WORD_BOUNDARY_PATTERNS = _compile_word_boundary_markers()


__all__ = [
    "DEFAULT_SCORE_VERSION",
    "DEFAULT_SIGNER_VERSION",
    "LEGAL_BASIS_PUBLIC_SEARCH_SELF_CRAWL",
    "PAYLOAD_HASH_HEX_LENGTH",
    "ProvenanceError",
    "SENSITIVE_FIELD_MARKERS",
    "SOURCE_TIKTOK_WEB",
    "WORD_BOUNDARY_MARKERS",
    "assert_clean_record",
    "canonical_payload_hash",
    "describe_source",
    "find_sensitive_fields",
    "is_collected_at",
    "is_payload_hash",
    "payload_hash_matches",
    "verify_provenance",
]
