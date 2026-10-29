"""Signer 版本实现：none-v1（后备，不做任何签名计算）。

仅用于已验证无需 X-Bogus 的只读模板。构造与调用都会拒绝写路径、
白名单外的任意 URL/path，以及外部传入的签名字段。
"""

from __future__ import annotations

from typing import Iterable

from app.sessions.models import SessionContext, SessionStatus

from .base import (
    ALLOWED_HOSTS,
    READONLY_METHODS,
    ReadonlyRequest,
    SignedRequest,
    SignerError,
    UrlNotAllowlisted,
    WritePathRejected,
    assert_no_external_signature,
    assert_readonly_url,
    contains_write_path,
    split_url,
)

# 已实机验证无需 X-Bogus 的只读端点（app/discovery/web_client.py 的 SEARCH_URL）
SEARCH_READONLY_URL = "https://www.tiktok.com/api/search/general/full/"
DEFAULT_ALLOWED_READONLY_URLS = (SEARCH_READONLY_URL,)


class NoopSigner:
    """none-v1：只读透传，不添加任何签名字段。"""

    version = "none-v1"

    def __init__(self,
                 allowed_urls: Iterable[str] = DEFAULT_ALLOWED_READONLY_URLS,
                 allowed_methods: Iterable[str] = ("GET",)):
        methods = frozenset(str(m).upper() for m in allowed_methods)
        if not methods or not methods <= READONLY_METHODS:
            raise WritePathRejected(
                f"Signer 只允许只读方法 {sorted(READONLY_METHODS)}，收到 {sorted(methods)}")

        urls = tuple(allowed_urls)
        if not urls:
            raise UrlNotAllowlisted("只读白名单不能为空")
        normalized = []
        for url in urls:
            scheme, host, path = split_url(url)
            if scheme != "https" or host not in ALLOWED_HOSTS:
                raise UrlNotAllowlisted(f"白名单 URL 不在已验证域名内: {url!r}")
            if contains_write_path(url):
                raise WritePathRejected(f"Signer 不接受写路径: {url!r}")
            normalized.append((scheme, host, path))

        self._allowed_urls = tuple(normalized)
        self._allowed_methods = methods

    def sign_readonly(self, request: ReadonlyRequest,
                      session: SessionContext) -> SignedRequest:
        method = str(request.method or "GET").upper()
        if method not in self._allowed_methods:
            raise WritePathRejected(f"Signer 不接受非只读方法: {method}")
        assert_readonly_url(request.url, self._allowed_urls)
        assert_no_external_signature(request)
        if session is not None and getattr(session, "status", None) != SessionStatus.ACTIVE:
            raise SignerError(f"会话不可用（status={getattr(session, 'status', None)}），拒绝签名")
        return SignedRequest(url=request.url,
                             params=dict(request.params or {}),
                             headers=dict(request.headers or {}),
                             signer_version=self.version)

    def __repr__(self) -> str:
        return f"NoopSigner(version={self.version}, urls={len(self._allowed_urls)})"


__all__ = ["NoopSigner", "SEARCH_READONLY_URL", "DEFAULT_ALLOWED_READONLY_URLS"]
