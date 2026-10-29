"""Signer 边界：Protocol、只读请求/已签名请求类型与校验工具。

约定（依据 2026-09/10 实测）：
- /api/search/general/full/ 当前无需 X-Bogus，签名层只做"只读 + 白名单"把关；
- 签名层拒绝写路径、任意 URL/path，以及外部传入的签名字段；
- 任何会话凭证（Cookie / msToken）都不进入签名输入输出。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Protocol, runtime_checkable
from urllib.parse import parse_qsl, urlsplit

from app.sessions.models import SessionContext

# 只读方法白名单：其它方法（含 POST/PUT/DELETE）一律视为写路径
READONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# 已验证可直连的 TikTok 域名
ALLOWED_HOSTS = frozenset({"www.tiktok.com", "tiktok.com"})

# 写路径特征（出现在 path 中即判定为写操作）
WRITE_PATH_MARKERS = (
    "publish", "upload", "commit", "create", "update", "delete", "remove",
    "comment", "like", "follow", "unfollow", "share", "send", "message",
    "submit", "edit", "report",
)

# 外部传入的签名字段：一律拒绝（签名只能由 Signer 自己产生）
# 以"小写 + 去掉 -/_ 分隔符"的紧凑形存储（配合 _norm_field），
# 覆盖大小写、驼峰与下划线/连字符变体：xbogus / abogus / verifyfp / mstoken
FORBIDDEN_SIGNATURE_FIELDS = frozenset({
    "xbogus", "xgnarly", "xttparams", "abogus",
    "signature", "verifyfp", "mstoken",
})


class SignerError(RuntimeError):
    """签名层基类错误。"""


class WritePathRejected(SignerError):
    """写路径 / 非只读方法：Signer 不参与任何写操作。"""


class UrlNotAllowlisted(SignerError):
    """URL、host 或 path 不在已验证的只读白名单内。"""


class ExternalSignatureRejected(SignerError):
    """调用方外部传入了签名字段。"""


def _norm_field(name: str) -> str:
    """归一化字段名：小写并移除全部 -/_ 分隔符。

    这样 verifyFp / verify_fp / VerifyFp 都归为 verifyfp，X-Bogus / XBogus
    都归为 xbogus，避免仅靠大小写或驼峰拼写就能绕过签名字段拦截。
    """
    return str(name).strip().lower().replace("-", "").replace("_", "")


def signature_fields_present(keys) -> tuple[str, ...]:
    return tuple(sorted({k for k in keys if _norm_field(k) in FORBIDDEN_SIGNATURE_FIELDS}))


def split_url(url: str) -> tuple[str, str, str]:
    """返回 (scheme, host, path)，统一小写并补尾斜杠，去掉 query/fragment。"""
    parts = urlsplit(str(url or ""))
    scheme = parts.scheme.lower()
    host = parts.netloc.lower()
    path = parts.path or "/"
    if not path.endswith("/"):
        path += "/"
    return scheme, host, path


def query_keys(url: str) -> tuple[str, ...]:
    return tuple(k for k, _ in parse_qsl(urlsplit(str(url or "")).query))


def assert_no_external_signature(request: ReadonlyRequest) -> None:
    """拒绝调用方在 URL query、params 或 headers 里自带签名字段。"""
    found = signature_fields_present(
        list(request.params or {}) + list(request.headers or {}) + list(query_keys(request.url)))
    if found:
        raise ExternalSignatureRejected(
            "签名只能由 Signer 产生，禁止外部传入: " + ", ".join(found))


def assert_readonly_url(url: str, allowed_urls: tuple[str, ...]) -> None:
    """URL 必须在只读白名单内：https + 已知 host + 完全匹配的 path。"""
    scheme, host, path = split_url(url)
    if scheme != "https" or host not in ALLOWED_HOSTS:
        raise UrlNotAllowlisted(f"URL 不在只读白名单内: {url!r}")
    if (scheme, host, path) not in allowed_urls:
        raise UrlNotAllowlisted(f"URL 不在只读白名单内: {url!r}")


def contains_write_path(url: str) -> bool:
    _, _, path = split_url(url)
    lowered = path.lower()
    return any(marker in lowered for marker in WRITE_PATH_MARKERS)


@dataclass(frozen=True)
class ReadonlyRequest:
    """待签名的只读请求。"""

    url: str
    params: Mapping[str, str] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)
    method: str = "GET"


@dataclass(frozen=True)
class SignedRequest:
    """签名结果：只有 URL、params、headers 和签名版本，没有会话凭证。"""

    url: str
    params: Mapping[str, str]
    headers: Mapping[str, str]
    signer_version: str = "none-v1"


@runtime_checkable
class Signer(Protocol):
    """签名器接口：未来接入真实 X-Bogus 实现时只需满足此 Protocol。"""

    version: str

    def sign_readonly(self, request: ReadonlyRequest,
                      session: SessionContext) -> SignedRequest: ...
