"""离线可测的 discovery 搜索用例编排。

一次 :meth:`DiscoveryService.search` 的完整链路：

    规范化 query + cache_key → 撤回检查 + 缓存读取
    → transport.search（每个关键词一次只读搜索）
    → parse_search_response → 去重 → aggregate_creator_results → score_creators
    → save_search + record_audit
    → 映射公共 DTO（排除 raw payload 与会话材料）

边界与诚实性约束：

* **不触网**。transport 由调用方注入，接口以
  :class:`app.transport.public_readonly.PublicReadonlySearch` 为准，即
  ``search(keyword, *, max_results=None) -> SearchRun``。
* **会话材料只以哈希出现**。公共 DTO 里的 provenance 只有
  ``session_id_hash``；raw payload、Cookie、签名输入一律不出现在响应里。
* **不完整的采集必须自证**。WAF / 软冷却 / 分页停滞 / 零记录都会得到
  ``partial=True`` 与稳定停止原因，绝不会把「没采到数据」说成「没有达人」；
  不完整的轮次**不写缓存**，避免把故障固化成事实。
* 签名说明如实标注：``none-v1`` 表示"未使用动态签名的已验证只读请求模板"，
  不虚构任何签名结果。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from app.domain.aggregation import aggregate_creator_results
from app.domain.deduplication import deduplicate_videos
from app.domain.models import (
    ENTITY_TYPE_CREATOR,
    SOURCE_SCOPE_PUBLIC_SEARCH_RESULT,
    SOURCE_TIKTOK_SEARCH_GENERAL_FULL,
    ParsedSearch,
    Provenance,
)
from app.domain.normalization import (
    hash_session_id,
    is_hashed_session_id,
    normalize_hashtag,
    normalize_keyword,
)
from app.domain.parser import parse_search_response
from app.domain.scoring import SCORE_VERSION, score_creators
from app.governance.deletion import SessionRevokedError, assert_session_usable
from app.governance.provenance import ProvenanceError
from app.signing.versions import NoopSigner
from app.storage.repositories import DEFAULT_CACHE_TTL_SECONDS, Repository
from app.transport.pagination import (
    COMPLETED,
    COOLING_STOPPED,
    PAGINATION_STALLED,
    PAGINATION_STOPPED,
)
from app.transport.response_classifier import TransportError

SCHEMA_VERSION = "1.0"
SCHEMA_NAME = "tiktok_creator_discovery_search_response"

SIGNER_VERSION = NoopSigner.version  # "none-v1"

# 对当前唯一实现 none-v1 的如实说明：不计算 X-Bogus，只透传已验证的只读请求模板。
SIGNER_NOTES: Mapping[str, str] = {
    "none-v1": "未使用动态签名的已验证只读请求模板（none-v1 不计算任何签名字段）",
}
_UNKNOWN_SIGNER_NOTE = "该 signer 版本未提供说明，按未标注处理"

DEFAULT_LIMIT = 20

RESULT_KIND_CREATORS_FOUND = "creators_found"
RESULT_KIND_NO_MATCHING_CREATORS = "no_matching_creators"
RESULT_KIND_NO_CREATORS_AFTER_FILTERS = "no_creators_after_filters"
RESULT_KIND_NO_DATA_COLLECTED = "no_data_collected"

STOP_NO_RECORDS = "no_records_collected"

# 分页停止原因的完成度语义（与 app.transport.pagination 对齐）：
# * PAGINATION_STOPPED — 达到 max_results/max_pages 上限、上游无记录或无下一页
#   cursor，是**正常终止**，已采数据完整，照常落缓存；
# * COOLING_STOPPED / PAGINATION_STALLED — 退避预算耗尽或分页停滞，采集被提前
#   掐断，完整度受损 → partial=True 且不落缓存。
NORMAL_STOP_STATUSES = (COMPLETED, PAGINATION_STOPPED)
DAMAGING_STOP_STATUSES = (COOLING_STOPPED, PAGINATION_STALLED)
# 两张表互不重叠，且并集 = 上游全部 SearchRun 状态常量（由
# ``test_stop_status_tables_cover_upstream_statuses`` 钉住）；上游新增状态而这里
# 没登记时，并集覆盖断言会先失败，不会让新状态被静默当成「完整」。
KNOWN_STOP_STATUSES = NORMAL_STOP_STATUSES + DAMAGING_STOP_STATUSES

CACHE_SKIP_INCOMPLETE = "collection_incomplete"
CACHE_SKIP_SEED_REJECTED = "cache_seed_rejected"

PAGINATION_NOTE = (
    "Phase 1 不提供跨请求续传：transport 层（PublicReadonlySearch.search）"
    "不接受 cursor，因此 next_cursor 恒为 None，has_more 也恒为 false；"
    "命中总数见 pagination.total_matched，被 limit 截断的行数见 meta.counts.rows_truncated"
)

AUDIT_OPERATION = "search"
AUDIT_OUTCOME_OK = "ok"
AUDIT_OUTCOME_PARTIAL = "partial"
AUDIT_OUTCOME_ERROR = "error"


# --------------------------------------------------------------------------- query


@dataclass(frozen=True)
class SearchQuery:
    """一次搜索的输入。规范化只做字符串折叠，不做语义猜测。"""

    keywords: tuple[str, ...]
    hashtags: tuple[str, ...] = ()
    exclude_keywords: tuple[str, ...] = ()
    region_codes: tuple[str, ...] = ()
    min_followers: int | None = None
    max_followers: int | None = None
    min_views: int | None = None
    min_relevance_score: float | None = None
    limit: int = DEFAULT_LIMIT
    cursor: str | None = None

    def normalized(self) -> "SearchQuery":
        """返回规范化后的等价 query：折叠空白/大小写、去重、limit 至少为 1。"""
        return replace(
            self,
            keywords=_unique(normalize_keyword(item) for item in self.keywords),
            hashtags=_unique(normalize_hashtag(item) for item in self.hashtags),
            exclude_keywords=_unique(normalize_keyword(item) for item in self.exclude_keywords),
            region_codes=_unique(_region_code(item) for item in self.region_codes),
            limit=max(1, int(self.limit)),
        )

    def cache_identity(
        self,
        *,
        session_id_hash: str,
        signer_version: str,
        schema_version: str = SCHEMA_VERSION,
    ) -> dict[str, Any]:
        """缓存身份：只有这些字段变化才算是"另一次搜索"。"""
        return {
            "schema_version": schema_version,
            "session_id_hash": session_id_hash,
            "signer_version": signer_version,
            "keywords": list(self.keywords),
            "hashtags": list(self.hashtags),
            "exclude_keywords": list(self.exclude_keywords),
            "region_codes": list(self.region_codes),
            "min_followers": self.min_followers,
            "max_followers": self.max_followers,
            "min_views": self.min_views,
            "min_relevance_score": self.min_relevance_score,
            "limit": self.limit,
            "cursor": self.cursor,
        }


# ------------------------------------------------------------------------ response

_PUBLIC_ROW_FIELDS = (
    "creator_id",
    "username",
    "profile_url",
    "keywords",
    "total_score",
    "relevance_score",
    "quality_score",
    "freshness_score",
    "score_version",
    "rank",
    "matched_video_count",
    "matched_keywords",
    "match_reasons",
    "unavailable_metrics",
    "follower_count",
    "region_code",
    "total_view_count",
    "collected_at",
    "source",
    "source_scope",
    "provenance",
)


@dataclass(frozen=True)
class CreatorRow:
    """对外的一行达人结果：领域 SearchResult 字段 + 过滤维度 + provenance。"""

    creator_id: str
    username: str
    profile_url: str
    keywords: tuple[str, ...]
    total_score: float | None
    relevance_score: float | None
    quality_score: float | None
    freshness_score: float | None
    score_version: str
    rank: int | None
    matched_video_count: int
    matched_keywords: tuple[str, ...]
    match_reasons: tuple[str, ...]
    unavailable_metrics: tuple[str, ...]
    follower_count: int | None
    region_code: str | None
    total_view_count: int | None
    collected_at: str
    source: str
    source_scope: str
    provenance: Mapping[str, Any]

    def to_public_dict(self) -> dict[str, Any]:
        """只输出白名单字段；raw payload 与会话材料不在白名单里。"""
        return {name: _public_value(getattr(self, name)) for name in _PUBLIC_ROW_FIELDS}


@dataclass(frozen=True)
class SearchResponse:
    """稳定形状的搜索响应：schema_version / data / pagination / meta / partial。"""

    schema_version: str
    request_id: str
    data: tuple[CreatorRow, ...]
    pagination: Mapping[str, Any]
    meta: Mapping[str, Any]
    partial: bool

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "partial": self.partial,
            "data": [row.to_public_dict() for row in self.data],
            "pagination": dict(self.pagination),
            "meta": _public_value(self.meta),
        }


# ------------------------------------------------------------------------- service


class DiscoveryService:
    """搜索用例编排。transport 与 repository 都从外部注入，便于离线测试。"""

    def __init__(
        self,
        transport: Any,
        repository: Repository,
        *,
        session_id_hash: str | None = None,
        signer_version: str = SIGNER_VERSION,
        schema_version: str = SCHEMA_VERSION,
        cache_ttl_seconds: int | None = None,
        clock: Callable[[], str] | None = None,
    ):
        self._transport = transport
        self._repository = repository
        self._session_id_hash = _coerce_session_id_hash(session_id_hash)
        self._signer_version = signer_version
        self._schema_version = schema_version
        self._cache_ttl_seconds = (
            DEFAULT_CACHE_TTL_SECONDS if cache_ttl_seconds is None else cache_ttl_seconds
        )
        self._clock = clock or _utc_now

    # ------------------------------------------------------------------ public API

    @property
    def session_id_hash(self) -> str:
        return self._session_id_hash

    @property
    def signer_version(self) -> str:
        return self._signer_version

    @property
    def schema_version(self) -> str:
        return self._schema_version

    def cache_key(self, query: SearchQuery) -> str:
        """规范化 query + 会话作用域 + signer 版本 + schema 版本的稳定指纹。"""
        identity = query.normalized().cache_identity(
            session_id_hash=self._session_id_hash,
            signer_version=self._signer_version,
            schema_version=self._schema_version,
        )
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        return f"discovery:v1:{digest[:32]}"

    def search(self, query: SearchQuery, *, request_id: str) -> SearchResponse:
        if not request_id:
            raise ValueError("request_id is required")

        normalized = query.normalized()
        if not normalized.keywords:
            raise ValueError("SearchQuery.keywords 不能为空：只读搜索按关键词发起")

        collected_at = self._clock()
        cache_key = self.cache_key(normalized)

        try:
            assert_session_usable(self._repository, self._session_id_hash)
        except SessionRevokedError:
            self._repository.record_audit(
                request_id,
                AUDIT_OPERATION,
                AUDIT_OUTCOME_ERROR,
                detail="session_revoked",
            )
            raise

        cached = self._repository.get_cached(cache_key, now=collected_at)
        if cached is not None:
            loaded = self._repository.get_search(cached.request_id) if cached.request_id else None
            if loaded is not None:
                # 与实采路径同一套重建流程，并同样取过滤后计数：命中缓存不能把
                # 「被 limit 截断后的行数」当成命中总数（total_matched /
                # rows_truncated 必须与实采一致）。
                cached_rows, cached_matched, cached_filtered_out = self._rows_from(
                    creators=loaded.creators,
                    videos=loaded.videos,
                    provenances=loaded.provenance,
                    query=normalized,
                    collected_at=cached.collected_at,
                    return_filtered=True,
                )
                response = self._build_response(
                    request_id=request_id,
                    rows=cached_rows,
                    query=normalized,
                    collection=_Collection(records=(), runs=(), errors=(), stops=()),
                    parsed_records=len(loaded.videos),
                    matched_count=cached_matched,
                    collected_at=cached.collected_at,
                    cache_info={
                        "key": cache_key,
                        "hit": True,
                        "ttl_seconds": self._cache_ttl_seconds,
                        "expires_at": cached.expires_at,
                        "source_request_id": cached.request_id,
                        "cache_skip_reason": None,
                    },
                    warnings=(),
                    filtered_out=cached_filtered_out,
                )
                self._audit(request_id, response, cache_hit=True)
                return response

        collection = self._collect(normalized)

        parsed = parse_search_response(
            {"status_code": 0, "data": collection.records},
            keywords=normalized.keywords,
            session_id_hash=self._session_id_hash,
            collected_at=collected_at,
        )

        rows, matched_count, filtered_out = self._rows_from(
            creators=parsed.creators,
            videos=parsed.videos,
            provenances=parsed.provenances,
            query=normalized,
            collected_at=collected_at,
            return_filtered=True,
        )

        incomplete = bool(collection.errors or collection.stops or not collection.records)
        skip_reason = (
            self._persist(
                parsed=parsed,
                request_id=request_id,
                cache_key=None if incomplete else cache_key,
                query=normalized,
                collected_at=collected_at,
            )
            or (CACHE_SKIP_INCOMPLETE if incomplete else None)
        )

        response = self._build_response(
            request_id=request_id,
            query=normalized,
            rows=rows,
            collection=collection,
            parsed_records=len(collection.records),
            matched_count=matched_count,
            collected_at=collected_at,
            cache_info={
                "key": cache_key,
                "hit": False,
                "ttl_seconds": self._cache_ttl_seconds,
                "expires_at": None,
                "source_request_id": None,
                "cache_skip_reason": skip_reason,
            },
            warnings=(f"cursor={normalized.cursor!r} 未生效：{PAGINATION_NOTE}",)
            if normalized.cursor
            else (),
            filtered_out=filtered_out,
        )
        self._audit(request_id, response, cache_hit=False)
        return response

    # -------------------------------------------------------------------- collection

    def _collect(self, query: SearchQuery) -> "_Collection":
        """逐关键词发起只读搜索；单个关键词失败不影响已采到的数据。"""
        records: list[Mapping[str, Any]] = []
        runs: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        stops: list[dict[str, str]] = []

        for keyword in query.keywords:
            try:
                run = self._transport.search(keyword, max_results=query.limit)
            except TransportError as exc:
                errors.append(
                    {
                        "keyword": keyword,
                        "code": getattr(exc, "kind", "transport_error"),
                        "detail": str(exc),
                    }
                )
                continue

            runs.append(
                {
                    "keyword": getattr(run, "keyword", keyword),
                    "status": run.status,
                    "detail": run.detail,
                    "pages": run.pages,
                    "attempts": run.attempts,
                    "records": len(run.records),
                }
            )
            if _completeness_damaged(run):
                stops.append(
                    {"keyword": getattr(run, "keyword", keyword), "status": run.status,
                     "detail": run.detail}
                )
            records.extend(run.records)

        if not records and not errors:
            stops.append(
                {
                    "keyword": "*",
                    "status": STOP_NO_RECORDS,
                    "detail": "本轮搜索没有采集到任何记录，不能判定为「没有达人」",
                }
            )

        return _Collection(
            records=tuple(records), runs=tuple(runs), errors=tuple(errors), stops=tuple(stops)
        )

    # ----------------------------------------------------------------- persistence

    def _persist(
        self,
        *,
        parsed: ParsedSearch,
        request_id: str,
        cache_key: str | None,
        query: SearchQuery,
        collected_at: str,
    ) -> str | None:
        """落库并播种缓存，返回 ``cache_skip_reason``（None 表示缓存已写）。

        缓存播种是 best-effort：``save_search`` 先提交数据再播种缓存，播种时若被
        敏感闸门拒收（例如 username 恰为整词 ``cookie``，让派生指纹命中标记），
        数据其实已经落库。此时不能让整次搜索失败，只能如实标注缓存未写。
        """
        try:
            self._repository.save_search(
                parsed,
                request_id,
                cache_key,
                keywords=query.keywords,
                signer_version=self._signer_version,
                score_version=SCORE_VERSION,
                collected_at=collected_at,
                cache_ttl_seconds=self._cache_ttl_seconds,
            )
            self._attach_cache_session(cache_key, request_id, collected_at)
        except ProvenanceError as exc:
            return f"{CACHE_SKIP_SEED_REJECTED}:{exc}"
        return None

    def _attach_cache_session(
        self, cache_key: str | None, request_id: str, collected_at: str
    ) -> None:
        """把会话作用域补到缓存行上。

        ``save_search`` 写入缓存时不带 ``session_id_hash``，若不补，
        ``purge_session`` 就删不掉该会话的缓存。这里读回上游算好的过期时间再重写，
        不重复实现 TTL 计算。
        """
        if not cache_key:
            return
        entry = self._repository.get_cached(cache_key, now=collected_at)
        if entry is None or entry.session_id_hash == self._session_id_hash:
            return
        self._repository.put_cached(
            cache_key,
            entry.response_json,
            entry.collected_at,
            entry.expires_at,
            request_id=request_id,
            session_id_hash=self._session_id_hash,
        )

    def _audit(self, request_id: str, response: SearchResponse, *, cache_hit: bool) -> None:
        outcome = AUDIT_OUTCOME_PARTIAL if response.partial else AUDIT_OUTCOME_OK
        detail = json.dumps(
            {
                "result_kind": response.meta["result_kind"],
                "rows": len(response.data),
                "cache": "hit" if cache_hit else "miss",
                "cache_skip_reason": response.meta["cache"]["cache_skip_reason"],
                "errors": len(response.meta["transport"]["errors"]),
                "stops": len(response.meta["transport"]["stops"]),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self._repository.record_audit(request_id, AUDIT_OPERATION, outcome, detail=detail)

    # ------------------------------------------------------------------- projection

    def _rows_from(
        self,
        *,
        creators: Sequence[Any],
        videos: Sequence[Any],
        provenances: Sequence[Provenance],
        query: SearchQuery,
        collected_at: str,
        return_filtered: bool = False,
    ) -> Any:
        """去重 → 聚合 → 评分 → 过滤 → 公共行。

        创作者层面的合并必须留在 ``aggregate_creator_results`` 内部：它要用**原始**
        创作者列表建立 id 重映射表，先在外层去重会让视频指向已被合并掉的 id。
        视频层面去重是幂等且不改变 ``creator_id``，所以放在这里显式执行。
        """
        aggregates = aggregate_creator_results(
            creators=creators,
            videos=deduplicate_videos(videos),
            keywords=query.keywords,
            hashtags=query.hashtags,
            exclude_keywords=query.exclude_keywords,
            now=collected_at,
        )
        scored = score_creators(aggregates, now=collected_at)

        by_creator = {
            record.entity_id: record
            for record in provenances or ()
            if record.entity_type == ENTITY_TYPE_CREATOR
        }

        rows: list[CreatorRow] = []
        filtered_out: dict[str, int] = {}
        for scored_creator in scored:
            reason = _filter_reason(scored_creator, query)
            if reason is not None:
                filtered_out[reason] = filtered_out.get(reason, 0) + 1
                continue
            rows.append(
                self._row(
                    scored_creator,
                    rank=len(rows) + 1,
                    provenance=by_creator.get(scored_creator.creator.id),
                    keywords=query.keywords,
                    collected_at=collected_at,
                )
            )

        visible = tuple(rows[: query.limit])
        if return_filtered:
            # 命中数只认**过滤后**的行数：过滤前的 len(scored) 会让「0 行」配上
            # 「total_matched>0」这种自相矛盾的响应。
            return visible, len(rows), filtered_out
        return visible

    def _row(
        self,
        scored_creator: Any,
        *,
        rank: int,
        provenance: Provenance | None,
        keywords: Sequence[str],
        collected_at: str,
    ) -> CreatorRow:
        creator = scored_creator.creator
        aggregate = scored_creator.aggregate
        return CreatorRow(
            creator_id=creator.id,
            username=creator.username,
            profile_url=creator.profile_url,
            keywords=tuple(aggregate.keywords or ()),
            total_score=scored_creator.total_score,
            relevance_score=scored_creator.relevance_score,
            quality_score=scored_creator.quality_score,
            freshness_score=scored_creator.freshness_score,
            score_version=scored_creator.score_version,
            rank=rank,
            matched_video_count=scored_creator.matched_video_count,
            matched_keywords=tuple(scored_creator.matched_keywords),
            match_reasons=tuple(scored_creator.match_reasons),
            unavailable_metrics=tuple(scored_creator.unavailable_metrics),
            follower_count=creator.follower_count,
            region_code=creator.region_code,
            total_view_count=aggregate.total_view_count,
            collected_at=collected_at,
            source=SOURCE_TIKTOK_SEARCH_GENERAL_FULL,
            source_scope=SOURCE_SCOPE_PUBLIC_SEARCH_RESULT,
            provenance=self._provenance(provenance, keywords, collected_at, creator.id),
        )

    def _provenance(
        self,
        record: Provenance | None,
        keywords: Sequence[str],
        collected_at: str,
        entity_id: str,
    ) -> dict[str, Any]:
        """source / source_scope / signer_version / collected_at / session_id_hash。

        ``signer_note`` 如实说明 none-v1 的含义：已验证的只读请求模板，未使用动态签名。
        """
        return {
            "provenance_id": record.id if record else None,
            "entity_id": entity_id,
            "source": record.source if record else SOURCE_TIKTOK_SEARCH_GENERAL_FULL,
            "source_scope": record.source_scope if record else SOURCE_SCOPE_PUBLIC_SEARCH_RESULT,
            "signer_version": self._signer_version,
            "signer_note": SIGNER_NOTES.get(self._signer_version, _UNKNOWN_SIGNER_NOTE),
            "collected_at": record.collected_at if record else collected_at,
            "session_id_hash": record.session_id_hash if record else self._session_id_hash,
            "raw_payload_hash": record.raw_payload_hash if record else None,
            "keywords": list(record.keywords) if record and record.keywords else list(keywords),
        }

    # ---------------------------------------------------------------------- response

    def _build_response(
        self,
        *,
        request_id: str,
        query: SearchQuery,
        rows: Sequence[CreatorRow],
        collection: "_Collection",
        parsed_records: int,
        matched_count: int | None,
        collected_at: str,
        cache_info: Mapping[str, Any],
        warnings: Sequence[str] = (),
        filtered_out: Mapping[str, int] | None = None,
    ) -> SearchResponse:
        data = tuple(rows)
        partial_reasons = [
            f"transport_error:{error['code']}" for error in collection.errors
        ] + [f"run_stopped:{stop['status']}" for stop in collection.stops]

        result_kind = _result_kind(
            rows=data,
            records=parsed_records,
            filtered=bool(filtered_out),
        )
        after_filters = len(data) if matched_count is None else matched_count

        meta: dict[str, Any] = {
            "schema": SCHEMA_NAME,
            "request_id": request_id,
            "partial": bool(partial_reasons),
            "partial_reasons": tuple(dict.fromkeys(partial_reasons)),
            "result_kind": result_kind,
            "keywords": query.keywords,
            "hashtags": query.hashtags,
            "exclude_keywords": query.exclude_keywords,
            "filters": {
                "region_codes": query.region_codes,
                "min_followers": query.min_followers,
                "max_followers": query.max_followers,
                "min_views": query.min_views,
                "min_relevance_score": query.min_relevance_score,
            },
            "collected_at": collected_at,
            "source": SOURCE_TIKTOK_SEARCH_GENERAL_FULL,
            "source_scope": SOURCE_SCOPE_PUBLIC_SEARCH_RESULT,
            "signer": {
                "version": self._signer_version,
                "note": SIGNER_NOTES.get(self._signer_version, _UNKNOWN_SIGNER_NOTE),
            },
            "score_version": SCORE_VERSION,
            "cache": dict(cache_info),
            "transport": {
                "runs": tuple(collection.runs),
                "errors": tuple(collection.errors),
                "stops": tuple(collection.stops),
            },
            "counts": {
                "records_collected": parsed_records,
                "creators_matched": after_filters,
                "creators_filtered_out": dict(filtered_out or {}),
                "rows_returned": len(data),
                "rows_truncated": max(0, after_filters - len(data)),
            },
            "pagination_note": PAGINATION_NOTE,
            "warnings": tuple(warnings),
        }

        return SearchResponse(
            schema_version=self._schema_version,
            request_id=request_id,
            data=data,
            pagination={
                "limit": query.limit,
                "returned": len(data),
                "total_matched": after_filters,
                # Phase 1 给不出续传 cursor（见 PAGINATION_NOTE），所以不声称
                # 「还有更多」——has_more 必须与 next_cursor 同真同假。
                "has_more": False,
                "cursor": query.cursor,
                "next_cursor": None,
            },
            meta=meta,
            partial=bool(partial_reasons),
        )


# --------------------------------------------------------------------------- helpers


@dataclass(frozen=True)
class _Collection:
    records: tuple[Mapping[str, Any], ...]
    runs: tuple[Mapping[str, Any], ...]
    errors: tuple[Mapping[str, str], ...]
    stops: tuple[Mapping[str, str], ...]


def _result_kind(*, rows: Sequence[CreatorRow], records: int, filtered: bool) -> str:
    """空结果的语义必须区分：没采到数据 ≠ 没有达人 ≠ 过滤后没有达人。"""
    if rows:
        return RESULT_KIND_CREATORS_FOUND
    if records <= 0:
        return RESULT_KIND_NO_DATA_COLLECTED
    # 命中数按过滤后计，所以「全部被过滤」必须先判：它不是「没有匹配达人」。
    if filtered:
        return RESULT_KIND_NO_CREATORS_AFTER_FILTERS
    return RESULT_KIND_NO_MATCHING_CREATORS


def _completeness_damaged(run: Any) -> bool:
    """这次 run 是否损伤了完整度（决定 partial 与是否落缓存）。

    判定只看 ``run.status`` 一张状态表，不看 ``SearchRun.stalled`` /
    ``.stopped_softly``（那两个属性把 ``pagination_stopped`` 也算作软停止，与本模块
    的「正常终止」口径不一致）：

    * ``NORMAL_STOP_STATUSES``（completed / pagination_stopped）→ 数据完整；
    * ``DAMAGING_STOP_STATUSES``（cooling_stopped / pagination_stalled）→ 受损；
    * **两张表都没登记的状态** → 同上，按受损处理，不把「不确定」说成「完整」。

    即「受损 = 不是已登记的正常终止」，未知状态与已知受损状态走同一支。
    """
    status = getattr(run, "status", COMPLETED)
    if status in NORMAL_STOP_STATUSES:
        return False
    return status in DAMAGING_STOP_STATUSES or status not in KNOWN_STOP_STATUSES


def _filter_reason(scored_creator: Any, query: SearchQuery) -> str | None:
    """返回第一个不满足的过滤条件；缺失的指标视为不满足（不静默当成 0）。"""
    creator = scored_creator.creator
    aggregate = scored_creator.aggregate

    if query.region_codes:
        region = _region_code(creator.region_code)
        if region not in query.region_codes:
            return "region_not_matched"

    if query.min_followers is not None:
        if creator.follower_count is None or creator.follower_count < query.min_followers:
            return "followers_below_min"

    if query.max_followers is not None:
        if creator.follower_count is None or creator.follower_count > query.max_followers:
            return "followers_above_max"

    if query.min_views is not None:
        if aggregate.total_view_count is None or aggregate.total_view_count < query.min_views:
            return "views_below_min"

    if query.min_relevance_score is not None:
        score = scored_creator.relevance_score
        if score is None or score < query.min_relevance_score:
            return "relevance_below_min"

    return None


def _coerce_session_id_hash(value: Any) -> str:
    """未注入会话时使用匿名只读引用（空会话 id 的不可逆哈希），不臆造具体会话。"""
    if isinstance(value, str) and is_hashed_session_id(value):
        return value
    return hash_session_id(value)


def _region_code(value: Any) -> str:
    return str(value).strip().upper() if value else ""


def _unique(values: Any) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for item in values if item))


def _public_value(value: Any) -> Any:
    if isinstance(value, (tuple, list)):
        return [_public_value(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _public_value(item) for key, item in value.items()}
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "AUDIT_OPERATION",
    "CreatorRow",
    "DEFAULT_LIMIT",
    "DiscoveryService",
    "SCHEMA_VERSION",
    "SIGNER_NOTES",
    "SIGNER_VERSION",
    "SearchQuery",
    "SearchResponse",
]
