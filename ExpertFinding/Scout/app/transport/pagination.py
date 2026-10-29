"""只读搜索分页：cursor/has_more 推进、重复页停止、软冷却有限退避。

停止策略（Phase 1 实测约束）：
- WAF 挑战：立即抛 :class:`WafChallengeError`，不在本轮重试、不做任何绕过；
- 认证失败/拒绝/限流/上游错误：抛对应稳定异常，由上层决定改期还是放弃；
- 软冷却（200 + 空 body）：同一轮内做**有上限**的指数退避（base×2^n，封顶 cap），
  预算耗尽则以 ``cooling_stopped`` 停止并返回已得数据，绝不无限重试；
- 相同 cursor 或重复页指纹：判定 ``pagination_stalled`` 并停止；
- 达到 max_pages / max_results / 缺少下一页 cursor：``pagination_stopped``；
- 自然翻到末页：``completed``；
- 单关键词单搜：一次 :meth:`ReadonlySearchPaginator.run` 只处理一个关键词；
  该约束**只在单次调用内**成立，同一实例可再次对同一关键词发起新调用
  （分页状态每次调用独立，跨调用不去重）。

``fetch`` 的契约是 ``(keyword, cursor) -> RawReply``，便于离线注入合成响应测试。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Sequence

from .response_classifier import (
    COOLING,
    SUCCESS,
    WAF_CHALLENGE,
    RawReply,
    WafChallengeError,
    classify_payload,
    classify_response,
    decode_json,
    raise_for_outcome,
)

COMPLETED = "completed"
PAGINATION_STOPPED = "pagination_stopped"
PAGINATION_STALLED = "pagination_stalled"
COOLING_STOPPED = "cooling_stopped"

# 单次搜索硬上限：防止无界拉取（即使调用方要更大的 max_results）
HARD_MAX_RESULTS = 200
DEFAULT_MAX_RESULTS = 100
DEFAULT_MAX_PAGES = 8
DEFAULT_EMPTY_BODY_ATTEMPTS = 3
DEFAULT_BACKOFF_BASE = 1.0
DEFAULT_BACKOFF_CAP = 8.0


@dataclass(frozen=True)
class SearchPage:
    records: tuple[Mapping, ...]
    cursor: str | None
    has_more: bool
    fingerprint: str


@dataclass(frozen=True)
class SearchRun:
    keyword: str
    records: tuple[Mapping, ...] = ()
    pages: int = 0
    status: str = COMPLETED
    detail: str = ""
    attempts: int = field(default=0)

    @property
    def stalled(self) -> bool:
        return self.status == PAGINATION_STALLED

    @property
    def stopped_softly(self) -> bool:
        """软停止：数据可用但本轮提前结束（不含停滞）。"""
        return self.status in (PAGINATION_STOPPED, COOLING_STOPPED)


def backoff_delay(attempt: int, *, base: float, cap: float) -> float:
    """第 n 次软冷却的等待时长：base, 2base, 4base … 封顶 cap（无抖动，可预测）。"""
    return min(base * (2 ** max(0, attempt - 1)), cap)


def page_fingerprint(records: Sequence[Mapping]) -> str:
    """页指纹：优先用记录 id；无 id 时退化为整页规范化序列化。"""
    ids = sorted(str(_record_id(r)) for r in records)
    basis = "|".join(ids) if any(ids) else json.dumps(
        [_canonical(r) for r in records], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


class ReadonlySearchPaginator:
    """按上述策略跑完一个关键词的搜索；不发起网络请求，网络由 fetch 承担。"""

    def __init__(
        self,
        fetch: Callable[[str, str | None], RawReply],
        *,
        max_results: int = DEFAULT_MAX_RESULTS,
        max_pages: int = DEFAULT_MAX_PAGES,
        empty_body_attempts: int = DEFAULT_EMPTY_BODY_ATTEMPTS,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        backoff_cap: float = DEFAULT_BACKOFF_CAP,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self._fetch = fetch
        self._max_results = max_results
        self._max_pages = max_pages
        self._empty_body_attempts = empty_body_attempts
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._sleep = sleeper

    def run(self, keyword: str | Iterable[str], *, max_results: int | None = None) -> SearchRun:
        kw = self._single_keyword(keyword)
        limit = max(0, min(max_results or self._max_results, HARD_MAX_RESULTS))

        records: list[Mapping] = []
        seen_fingerprints: set[str] = set()
        cursor: str | None = None
        pages = 0
        empty_attempts = 0
        attempts = 0
        status, detail = COMPLETED, "到达末页，数据完整"

        while True:
            if pages >= self._max_pages:
                return self._result(kw, records, pages, attempts, PAGINATION_STOPPED,
                                    f"达到 max_pages 上限 {self._max_pages}")
            if limit and len(records) >= limit:
                return self._result(kw, records, pages, attempts, PAGINATION_STOPPED,
                                    f"达到 max_results 上限 {limit}")

            reply = self._fetch(kw, cursor)
            attempts += 1
            outcome = classify_response(reply.status, reply.body)

            if outcome.kind == WAF_CHALLENGE:
                raise WafChallengeError(outcome.detail)
            if outcome.kind == COOLING:
                empty_attempts += 1
                if empty_attempts >= self._empty_body_attempts:
                    return self._result(
                        kw, records, pages, attempts, COOLING_STOPPED,
                        f"空响应体连续 {empty_attempts} 次，退避已达上限 "
                        f"{self._backoff_cap}s，停止本轮搜索")
                delay = backoff_delay(empty_attempts, base=self._backoff_base,
                                      cap=self._backoff_cap)
                self._sleep(delay)
                continue
            if outcome.kind != SUCCESS:
                raise_for_outcome(outcome)

            payload = decode_json(reply.body)
            payload_outcome = classify_payload(payload)
            if payload_outcome.kind != SUCCESS:
                raise_for_outcome(payload_outcome)

            page = _extract_page(payload)
            if page.records and page.fingerprint in seen_fingerprints:
                return self._result(kw, records, pages, attempts, PAGINATION_STALLED,
                                    "重复页指纹：上游返回同一批记录")
            if page.cursor is not None and page.cursor == cursor:
                return self._result(kw, records, pages, attempts, PAGINATION_STALLED,
                                    f"cursor 未推进（{cursor}），判定为停滞")

            seen_fingerprints.add(page.fingerprint)
            remaining = limit - len(records) if limit else len(page.records)
            records.extend(list(page.records)[:max(0, remaining)])
            pages += 1

            if not page.records:
                return self._result(kw, records, pages, attempts, PAGINATION_STOPPED,
                                    "响应页无记录，停止翻页")
            if not page.has_more:
                return self._result(kw, records, pages, attempts, COMPLETED,
                                    "到达末页，数据完整")
            if page.cursor is None:
                return self._result(kw, records, pages, attempts, PAGINATION_STOPPED,
                                    "上游未返回 cursor，无法继续翻页")
            cursor = page.cursor

    def _single_keyword(self, keyword: str | Iterable[str]) -> str:
        """单关键词单搜：一次调用只处理一个非空关键词。

        黑名单不跨调用保留 —— 分页状态（cursor / 页指纹 / 冷却预算）都在
        :meth:`run` 的局部作用域内，同一实例再次搜索同一关键词是全新的调用。
        """
        if isinstance(keyword, str):
            candidates = [keyword]
        else:
            candidates = list(keyword)
        meaningful = [k for k in candidates if str(k).strip()]
        if len(meaningful) != 1:
            raise ValueError("单次搜索只允许一个关键词（单关键词单搜）")
        return str(meaningful[0]).strip()

    @staticmethod
    def _result(keyword, records, pages, attempts, status, detail) -> SearchRun:
        return SearchRun(keyword=keyword, records=tuple(records), pages=pages,
                         status=status, detail=detail, attempts=attempts)


def _extract_page(payload: Mapping) -> SearchPage:
    data = payload.get("data")
    raw_records = tuple(data) if isinstance(data, (list, tuple)) else ()
    records = tuple(r for r in raw_records if isinstance(r, Mapping))
    cursor = payload.get("cursor")
    return SearchPage(
        records=records,
        cursor=str(cursor) if cursor is not None else None,
        has_more=bool(payload.get("has_more")),
        fingerprint=page_fingerprint(records),
    )


def _record_id(record: Mapping) -> str:
    rid = record.get("id")
    if rid:
        return str(rid)
    item = record.get("item")
    if isinstance(item, Mapping) and item.get("id"):
        return str(item["id"])
    return ""


def _canonical(record: Mapping) -> str:
    return json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
