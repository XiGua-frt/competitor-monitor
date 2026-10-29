"""受控只读适配层：把已实机验证的 web_client 包装成明确的只读搜索能力。

边界原则：
- 只暴露 READONLY_OPERATIONS 里的操作（当前只有 search），写操作一律 PermissionError；
- 会话材料、代理、Cookie 由 web_client 内部持有，本层不读取、不落盘、不外泄；
- describe()/repr() 只输出策略元数据，不含任何凭证或会话字段；
- 所有请求都经由 pagination 策略（分页上限、停滞检测、冷却预算、WAF 即停）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Protocol

from .pagination import (
    DEFAULT_MAX_PAGES,
    DEFAULT_MAX_RESULTS,
    HARD_MAX_RESULTS,
    ReadonlySearchPaginator,
    SearchRun,
)
from .response_classifier import RawReply

READONLY_OPERATIONS = frozenset({"search"})


class SearchTransport(Protocol):
    """只有一个只读页面读取动作的传输协议。"""

    def fetch_search_page(self, keyword: str, cursor: str | None = None) -> RawReply: ...


@dataclass(frozen=True)
class ReadonlyPolicy:
    """对外可描述的策略元数据（不含凭证、代理、Cookie）。"""

    transport: str
    source_scope: str
    operations: tuple[str, ...]
    max_results_hard_cap: int
    waf_policy: str
    session_handling: str


class PublicReadonlySearch:
    """TikTok 外网只读搜索入口。transport 可注入，便于离线测试。"""

    name = "tiktok_web"
    source_scope = "tiktok_web_signed_api_self_crawler"
    waf_policy = "stop_and_escalate_no_bypass"
    session_handling = "injected_only_never_exposed"

    def __init__(
        self,
        transport: SearchTransport | Callable[[str, str | None], RawReply],
        *,
        max_results: int = DEFAULT_MAX_RESULTS,
        max_pages: int = DEFAULT_MAX_PAGES,
    ):
        fetch = transport if callable(transport) else transport.fetch_search_page
        self._transport = transport
        self._default_max_results = max_results
        self._paginator = ReadonlySearchPaginator(
            fetch, max_results=max_results, max_pages=max_pages)

    @classmethod
    def from_verified_client(cls, session_file: str | None = None,
                             proxy: str | None = None, **kwargs) -> "PublicReadonlySearch":
        """使用已验证出口（本机代理 127.0.0.1:7892）构造真实客户端。

        延迟导入以避免依赖 curl_cffi 的测试被真实客户端拖入。
        """
        from app.discovery.web_client import TikTokWebClient

        return cls(TikTokWebClient(session_file=session_file, proxy=proxy), **kwargs)

    def search(self, keyword: str | Iterable[str], *,
               max_results: int | None = None) -> SearchRun:
        return self._paginator.run(keyword, max_results=max_results)

    def ensure_readonly(self, operation: str) -> None:
        if operation not in READONLY_OPERATIONS:
            raise PermissionError(f"只读适配层禁止操作：{operation}")

    def describe(self) -> Mapping[str, object]:
        policy = ReadonlyPolicy(
            transport=self.name,
            source_scope=self.source_scope,
            operations=tuple(sorted(READONLY_OPERATIONS)),
            max_results_hard_cap=HARD_MAX_RESULTS,
            waf_policy=self.waf_policy,
            session_handling=self.session_handling,
        )
        return vars(policy) | {"default_max_results": self._default_max_results}

    def __repr__(self) -> str:  # 不反射底层会话材料
        return (f"PublicReadonlySearch(transport={type(self._transport).__name__}, "
                f"readonly_only=True)")
