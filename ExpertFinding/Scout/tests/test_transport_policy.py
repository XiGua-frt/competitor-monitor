"""Task 6：Transport 响应分类、冷却与分页停止策略。

约束：全程离线。所有 status/body 输入均为合成数据，不发起真实网络请求，
不读取 .secrets 下任何会话材料。fixture 定义在本文件内部（不依赖 conftest.py）。
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest

from app.transport.pagination import (
    COOLING_STOPPED,
    COMPLETED,
    HARD_MAX_RESULTS,
    PAGINATION_STALLED,
    PAGINATION_STOPPED,
    ReadonlySearchPaginator,
    SearchRun,
    backoff_delay,
    page_fingerprint,
)
from app.transport.public_readonly import READONLY_OPERATIONS, PublicReadonlySearch
from app.transport.response_classifier import (
    AUTH_FAILED,
    COOLING,
    FORBIDDEN,
    RATE_LIMITED,
    SCHEMA_MISMATCH,
    SUCCESS,
    UNEXPECTED_STATUS,
    UPSTREAM_REJECTED,
    UPSTREAM_UNAVAILABLE,
    WAF_CHALLENGE,
    AuthFailedError,
    ForbiddenError,
    RateLimitedError,
    RawReply,
    SchemaChangedError,
    TransportError,
    UpstreamCoolingError,
    UpstreamRejectedError,
    UpstreamUnavailableError,
    WafChallengeError,
    classify_payload,
    classify_response,
    decode_json,
    raise_for_outcome,
)

# --------------------------------------------------------------------------
# 合成输入构造：只产生 status/body，绝不触网
# --------------------------------------------------------------------------

WAF_BODY = "<html><body>slardar challenge: please verify</body></html>"


def ok_page(
    item_ids: tuple[str, ...],
    *,
    cursor: str = "12",
    has_more: bool = True,
    status_code: int = 0,
) -> RawReply:
    """合成一次成功的搜索响应：data 内含 item 视频记录。"""
    return RawReply(
        status=200,
        body=json.dumps({
            "status_code": status_code,
            "cursor": cursor,
            "has_more": has_more,
            "data": [{"type": 1, "item": {"id": i}} for i in item_ids],
        }),
    )


class ScriptedTransport:
    """按脚本返回合成 RawReply 的假传输层；记录每次调用便于断言。"""

    def __init__(self, replies: list[RawReply]):
        self._replies = list(replies)
        self.calls: list[tuple[str, str | None]] = []

    def fetch_search_page(self, keyword: str, cursor: str | None = None) -> RawReply:
        self.calls.append((keyword, cursor))
        if not self._replies:
            return RawReply(status=200, body="")
        return self._replies.pop(0)


class RecordingSleeper:
    """替换 time.sleep：记录退避时长，不真的等待。"""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def make_paginator(replies, sleeper: RecordingSleeper | None = None, **kwargs):
    transport = ScriptedTransport(replies)
    paginator = ReadonlySearchPaginator(
        transport.fetch_search_page,
        sleeper=sleeper or RecordingSleeper(),
        **kwargs,
    )
    return paginator, transport


# --------------------------------------------------------------------------
# Step 1：响应分类（brief 断言原样采用）
# --------------------------------------------------------------------------


def test_empty_body_is_cooling_not_success():
    assert classify_response(status=200, body="").kind == "cooling"


def test_slardar_body_is_waf_and_never_retryable():
    result = classify_response(status=200, body="<html>slardar challenge</html>")
    assert result.kind == "waf_challenge"
    assert result.retryable is False


def test_401_403_429_and_5xx_have_stable_kinds():
    assert classify_response(401, "").kind == "auth_failed"
    assert classify_response(403, "").kind == "forbidden"
    assert classify_response(429, "").kind == "rate_limited"
    assert classify_response(503, "").kind == "upstream_unavailable"


def test_json_body_with_records_is_success():
    outcome = classify_response(200, ok_page(("7301",)).body)
    assert outcome.kind == SUCCESS
    assert outcome.retryable is False


def test_waf_body_overrides_blocking_status():
    """403 + 挑战页同样归类为 WAF，避免被误判成普通权限问题而重试。"""
    assert classify_response(403, WAF_BODY).kind == WAF_CHALLENGE


def test_cooling_is_soft_and_retryable_while_auth_is_not():
    assert classify_response(200, "").retryable is True
    assert classify_response(401, "").retryable is False
    assert classify_response(429, "").retryable is True
    assert classify_response(500, "").retryable is True


def test_unknown_status_is_not_silently_treated_as_success():
    outcome = classify_response(302, "http/1.1 moved")
    assert outcome.kind == UNEXPECTED_STATUS
    assert outcome.retryable is False


def test_classification_detail_never_echoes_body():
    """边界要求：detail 用于日志与异常消息，不能回显响应体。"""
    marker = "SLARDAR-SECRET-TOKEN"
    outcome = classify_response(200, f"<html>{marker}</html>")
    assert marker not in outcome.detail
    assert outcome.detail


def test_decode_json_raises_schema_changed_on_garbage_body():
    with pytest.raises(SchemaChangedError):
        decode_json("not-json-at-all")


def test_classify_payload_requires_contract_keys():
    missing_data = classify_payload({"status_code": 0})
    assert missing_data.kind == SCHEMA_MISMATCH
    assert classify_payload({"data": []}).kind == SCHEMA_MISMATCH
    assert classify_payload(["not", "a", "mapping"]).kind == SCHEMA_MISMATCH


def test_classify_payload_nonzero_status_code_is_upstream_rejected():
    outcome = classify_payload({"status_code": 20005, "data": []})
    assert outcome.kind == UPSTREAM_REJECTED
    assert outcome.retryable is False


def test_classify_payload_sunny_path_is_success():
    assert classify_payload(json.loads(ok_page(("1",)).body)).kind == SUCCESS


def test_raise_for_outcome_maps_every_kind_to_stable_exception():
    cases = {
        WAF_CHALLENGE: WafChallengeError,
        AUTH_FAILED: AuthFailedError,
        FORBIDDEN: ForbiddenError,
        RATE_LIMITED: RateLimitedError,
        UPSTREAM_UNAVAILABLE: UpstreamUnavailableError,
        UPSTREAM_REJECTED: UpstreamRejectedError,
        SCHEMA_MISMATCH: SchemaChangedError,
        COOLING: UpstreamCoolingError,
    }
    for kind, exc in cases.items():
        outcome = dataclasses.replace(classify_response(200, ""), kind=kind)
        with pytest.raises(exc):
            raise_for_outcome(outcome)
        with pytest.raises(TransportError):
            raise_for_outcome(outcome)


# --------------------------------------------------------------------------
# Step 2：分页推进、重复页停止、软冷却退避
# --------------------------------------------------------------------------


def test_paginator_advances_with_cursor_and_has_more():
    replies = [
        ok_page(("a1", "a2"), cursor="12"),
        ok_page(("b1", "b2"), cursor="24", has_more=False),
    ]
    paginator, transport = make_paginator(replies)
    run = paginator.run("fitness coach")

    assert [r["item"]["id"] for r in run.records] == ["a1", "a2", "b1", "b2"]
    assert run.pages == 2
    assert run.status == COMPLETED
    assert transport.calls == [("fitness coach", None), ("fitness coach", "12")]


def test_repeated_cursor_is_pagination_stalled():
    replies = [ok_page(("a1",), cursor="12"), ok_page(("b1",), cursor="12")]
    paginator, transport = make_paginator(replies)
    run = paginator.run("fitness coach")

    assert run.status == PAGINATION_STALLED
    assert run.stalled is True
    assert len(transport.calls) == 2  # 停滞即停止，不再继续拉取


def test_duplicate_page_fingerprint_is_pagination_stalled():
    """cursor 不同但记录完全相同 —— 仍判定停滞并停止。"""
    replies = [ok_page(("a1",), cursor="12"), ok_page(("a1",), cursor="24")]
    paginator, transport = make_paginator(replies)
    run = paginator.run("fitness coach")

    assert run.status == PAGINATION_STALLED
    assert len(transport.calls) == 2


def test_fingerprint_changes_when_records_change():
    assert page_fingerprint(({"item": {"id": "1"}},)) != page_fingerprint(
        ({"item": {"id": "2"}},))
    assert page_fingerprint(({"item": {"id": "1"}},)) == page_fingerprint(
        ({"item": {"id": "1"}},))


def test_single_keyword_rule_is_scoped_to_one_call():
    """单关键词单搜只在单次调用内成立；同一实例可再次对同一关键词发起新调用。

    长生命周期调用方（如 PublicReadonlySearch 持有单个 paginator）复用实例时
    不得因“该关键词搜过”而炸；每次 run() 的分页状态互相独立。
    """
    paginator, transport = make_paginator(
        [ok_page(("a1",), has_more=False), ok_page(("b1",), has_more=False)])
    first = paginator.run("fitness coach")
    second = paginator.run("fitness coach")

    assert first.status == COMPLETED and second.status == COMPLETED
    assert [r["item"]["id"] for r in first.records] == ["a1"]
    assert [r["item"]["id"] for r in second.records] == ["b1"]
    # 第二次调用从干净的分页状态开始（cursor=None）
    assert transport.calls == [("fitness coach", None), ("fitness coach", None)]


def test_single_keyword_single_search_rejects_multiple_keywords():
    paginator, _ = make_paginator([ok_page(("a1",), has_more=False)])
    with pytest.raises(ValueError):
        paginator.run(["fitness coach", "yoga"])


def test_empty_keyword_is_rejected():
    paginator, transport = make_paginator([ok_page(("a1",), has_more=False)])
    with pytest.raises(ValueError):
        paginator.run("   ")
    assert transport.calls == []


def test_empty_body_exponential_backoff_is_capped_and_finite():
    sleeper = RecordingSleeper()
    paginator, transport = make_paginator(
        [RawReply(status=200, body="")],
        sleeper=sleeper,
        empty_body_attempts=4,
        backoff_base=1.0,
        backoff_cap=4.0,
    )
    run = paginator.run("fitness coach")

    assert run.status == COOLING_STOPPED
    assert run.records == ()
    assert sleeper.delays == [1.0, 2.0, 4.0]  # 第 4 次已耗尽预算，不再退避
    assert len(transport.calls) == 4  # 不无限重试


def test_backoff_delay_grows_then_hits_cap():
    assert backoff_delay(1, base=1.0, cap=8.0) == 1.0
    assert backoff_delay(2, base=1.0, cap=8.0) == 2.0
    assert backoff_delay(3, base=1.0, cap=8.0) == 4.0
    assert backoff_delay(9, base=1.0, cap=8.0) == 8.0


def test_cooling_then_recovering_page_returns_partial_records():
    sleeper = RecordingSleeper()
    replies = [
        RawReply(status=200, body=""),
        ok_page(("a1",), cursor="12", has_more=False),
    ]
    paginator, _ = make_paginator(replies, sleeper=sleeper)
    run = paginator.run("fitness coach")

    assert run.status == COMPLETED
    assert [r["item"]["id"] for r in run.records] == ["a1"]
    assert sleeper.delays == [1.0]


def test_waf_body_raises_immediately_and_stops_fetching():
    paginator, transport = make_paginator([RawReply(status=200, body=WAF_BODY)])
    with pytest.raises(WafChallengeError):
        paginator.run("fitness coach")
    assert len(transport.calls) == 1


def test_auth_failure_raises_stable_exception():
    paginator, _ = make_paginator([RawReply(status=401, body="")])
    with pytest.raises(AuthFailedError):
        paginator.run("fitness coach")


def test_forbidden_and_rate_limited_raise_stable_exceptions():
    forbidden, _ = make_paginator([RawReply(status=403, body="")])
    with pytest.raises(ForbiddenError):
        forbidden.run("fitness coach")

    limited, _ = make_paginator([RawReply(status=429, body="")])
    with pytest.raises(RateLimitedError):
        limited.run("fitness coach")


def test_upstream_5xx_raises_stable_exception():
    paginator, _ = make_paginator([RawReply(status=503, body="")])
    with pytest.raises(UpstreamUnavailableError):
        paginator.run("fitness coach")


def test_unparsable_body_is_schema_changed_not_success():
    paginator, _ = make_paginator([RawReply(status=200, body="<!doctype html>oops")])
    with pytest.raises(SchemaChangedError):
        paginator.run("fitness coach")


def test_missing_contract_keys_is_schema_changed():
    paginator, _ = make_paginator([RawReply(status=200, body=json.dumps({"cursor": "1"}))])
    with pytest.raises(SchemaChangedError):
        paginator.run("fitness coach")


def test_nonzero_status_code_stops_with_upstream_rejected():
    paginator, _ = make_paginator([ok_page(("a1",), status_code=20005)])
    with pytest.raises(UpstreamRejectedError):
        paginator.run("fitness coach")


def test_max_results_caps_records_and_marks_stopped():
    replies = [
        ok_page(("a1", "a2"), cursor="12"),
        ok_page(("b1", "b2"), cursor="24"),
        ok_page(("c1", "c2"), cursor="36"),
    ]
    paginator, _ = make_paginator(replies, max_pages=10)
    run = paginator.run("fitness coach", max_results=3)

    assert [r["item"]["id"] for r in run.records] == ["a1", "a2", "b1"]
    assert run.status == PAGINATION_STOPPED


def test_max_results_is_clamped_to_hard_cap():
    paginator, _ = make_paginator([ok_page(("a1",), has_more=False)])
    run = paginator.run("fitness coach", max_results=HARD_MAX_RESULTS + 500)
    assert len(run.records) <= HARD_MAX_RESULTS


def test_max_pages_stops_the_loop():
    replies = [ok_page((f"p{i}{j}",), cursor=str(i * 12)) for i in range(1, 6)
               for j in range(1)]
    paginator, transport = make_paginator(replies, max_pages=2)
    run = paginator.run("fitness coach")

    assert run.pages == 2
    assert run.status == PAGINATION_STOPPED
    assert len(transport.calls) == 2


def test_missing_next_cursor_stops_the_loop():
    paginator, _ = make_paginator([ok_page(("a1",), cursor=None, has_more=True)])
    run = paginator.run("fitness coach")

    assert run.status == PAGINATION_STOPPED
    assert len(run.records) == 1


def test_empty_first_page_stops_the_loop():
    paginator, _ = make_paginator([ok_page((), cursor="12")])
    run = paginator.run("fitness coach")

    assert run.records == ()
    assert run.status == PAGINATION_STOPPED


def test_run_result_is_immutable_and_carries_keyword():
    paginator, _ = make_paginator([ok_page(("a1",), has_more=False)])
    run = paginator.run("fitness coach")
    assert isinstance(run, SearchRun)
    assert run.keyword == "fitness coach"
    with pytest.raises(Exception):
        run.status = "tampered"  # type: ignore[misc]


# --------------------------------------------------------------------------
# Step 3 / 边界：受控只读适配层不泄漏会话材料
# --------------------------------------------------------------------------

FORBIDDEN_TOKENS = ("msToken", "sessionid", "sid_tt", "ttwid", "cookie",
                    "Cookie", "127.0.0.1", "password")


def test_readonly_adapter_only_allows_search():
    view = PublicReadonlySearch(ScriptedTransport([]))
    view.ensure_readonly("search")
    for op in ("follow_user", "post_comment", "like_video", "upload"):
        with pytest.raises(PermissionError):
            view.ensure_readonly(op)
    assert "search" in READONLY_OPERATIONS


def test_adapter_describe_leaks_no_session_material():
    view = PublicReadonlySearch(ScriptedTransport([]))
    blob = json.dumps(view.describe(), ensure_ascii=False)
    for token in FORBIDDEN_TOKENS:
        assert token not in blob


def test_adapter_repr_leaks_no_session_material():
    class DirtyTransport:
        def __init__(self):
            self._session = {"msToken": "synthetic-not-real", "sessionid": "none"}

        def fetch_search_page(self, keyword, cursor=None):
            return RawReply(status=200, body="")

    view = PublicReadonlySearch(DirtyTransport())
    for token in ("msToken", "sessionid"):
        assert token not in repr(view)


def test_adapter_search_returns_pagination_run():
    transport = ScriptedTransport([ok_page(("a1",), has_more=False)])
    view = PublicReadonlySearch(transport.fetch_search_page)
    run = view.search("fitness coach", max_results=10)
    assert run.status == COMPLETED
    assert [r["item"]["id"] for r in run.records] == ["a1"]


def test_adapter_instance_can_search_same_keyword_repeatedly():
    """长生命周期的只读适配层（单 paginator 实例）可反复搜同一关键词。"""
    transport = ScriptedTransport(
        [ok_page(("a1",), has_more=False), ok_page(("a2",), has_more=False)])
    view = PublicReadonlySearch(transport.fetch_search_page)

    first = view.search("fitness coach", max_results=10)
    second = view.search("fitness coach", max_results=10)

    assert [r["item"]["id"] for r in first.records] == ["a1"]
    assert [r["item"]["id"] for r in second.records] == ["a2"]


def test_adapter_rejects_blank_keyword_before_any_fetch():
    transport = ScriptedTransport([ok_page(("a1",), has_more=False)])
    view = PublicReadonlySearch(transport.fetch_search_page)
    with pytest.raises(ValueError):
        view.search("")
    assert transport.calls == []


# --------------------------------------------------------------------------
# Step 3 回归：web_client 接入分类器后仍可导入且异常稳定
# --------------------------------------------------------------------------


def test_web_client_imports_and_shares_the_same_waf_error():
    from app.discovery import web_client

    assert web_client.WafChallengeError is WafChallengeError


def test_web_client_docstring_points_at_current_spec_section():
    from app.discovery import web_client

    doc = web_client.__doc__ or ""
    assert "§20" in doc
    assert "§4.4" not in doc


def build_offline_client(monkeypatch, status: int, body: str, seen: list | None = None):
    """构造恒定返回同一 (status, body) 的离线客户端（等价于单条脚本）。"""
    return build_scripted_client(monkeypatch, [(status, body)], seen)


def build_scripted_client(monkeypatch, replies, seen: list | None = None):
    """构造不触网、不读 .secrets 的 TikTokWebClient，按脚本依次返回合成响应。

    与既有手法一致：只替换 curl_cffi Session，`_get` 保持真实实现，因此
    “先分类再解码”这条端到端链路被完整验证。脚本耗尽后重复最后一页。

    观测点（均挂在 client 上，供断言使用）：
    - `offline_calls`：每次真实发出的请求参数；
    - `offline_json_calls`：`r.json()` 被调用的原文 —— 非空即证明走了未分类的
      解码路径，正是这些用例要守的反例。
    """
    from app.discovery import web_client

    seen = seen if seen is not None else []
    script = [tuple(item) for item in replies]
    calls: list = []
    json_calls: list = []

    class FakeResponse:
        def __init__(self, response_status: int, response_body: str) -> None:
            self.status_code = response_status
            self.text = response_body

        def json(self):
            json_calls.append(self.text)
            raise AssertionError("必须先经分类器/解码器，不允许直接调用 r.json()")

    class FakeSession:
        def __init__(self, **kwargs) -> None:
            seen.append(kwargs)
            self.cookies: dict[str, str] = {}

        def get(self, url, params=None):
            calls.append(params)
            response_status, response_body = (
                script.pop(0) if len(script) > 1 else script[0])
            return FakeResponse(response_status, response_body)

    monkeypatch.setattr(web_client.creq, "Session", FakeSession)
    client = web_client.TikTokWebClient.__new__(web_client.TikTokWebClient)
    client._proxy = None
    client._session = {}  # 合成空会话；离线测试绝不读取 .secrets
    client._last_request_at = 0.0
    client._throttle = lambda: None  # 不真的 sleep 3-6 秒
    client.offline_calls = calls
    client.offline_json_calls = json_calls
    return client


def test_web_client_keeps_verified_success_conditions(monkeypatch):
    from app.discovery import web_client

    assert web_client.IMPERSONATE == "chrome131"
    assert web_client.MIN_INTERVAL == (3.0, 6.0)
    assert web_client.SEARCH_URL.endswith("/api/search/general/full/")
    assert web_client.default_proxy() == "http://127.0.0.1:7892"

    seen: list = []
    client = build_offline_client(monkeypatch, 200,
                                  ok_page(("x1",), has_more=False).body, seen)
    records = client.search(web_client.SearchRequest(keywords=("fitness coach",)))

    assert [rec["raw_id"] for rec in records] == ["x1"]
    assert seen[0]["impersonate"] == "chrome131"
    assert seen[0]["timeout"] == 25


def test_web_client_search_classifies_empty_body_before_json_decode(monkeypatch):
    """空 body 在 r.json() 之前就被分类为冷却（不是 schema 变化），且退避有上限。"""
    from app.discovery import web_client

    sleeper = RecordingSleeper()
    monkeypatch.setattr(web_client, "_sleep", sleeper)
    client = build_offline_client(monkeypatch, 200, "")
    request = web_client.SearchRequest(keywords=("fitness coach",))

    records = client.search(request)

    assert records == []  # 退避预算耗尽 → 软停止并返回已收集结果
    assert client.offline_json_calls == []  # 从未走到 r.json()
    assert len(client.offline_calls) == web_client.COOLING_MAX_ATTEMPTS
    assert sleeper.delays == [1.0, 2.0]  # 第 3 次耗尽预算，不再退避
    assert client.last_stop_reason == COOLING_STOPPED


def test_web_client_search_cooling_backoff_keeps_already_collected(monkeypatch):
    """先成功一页再连续空 body：退避耗尽后返回已收集结果，不抛异常也不死循环。"""
    from app.discovery import web_client

    sleeper = RecordingSleeper()
    monkeypatch.setattr(web_client, "_sleep", sleeper)
    client = build_scripted_client(monkeypatch, [
        (200, ok_page(("a1",), cursor="12").body),
        (200, ""),
    ])
    records = client.search(web_client.SearchRequest(keywords=("fitness coach",)))

    assert [rec["raw_id"] for rec in records] == ["a1"]
    assert sleeper.delays == [1.0, 2.0]  # 指数退避，且只有 3 次尝试
    assert len(client.offline_calls) == 1 + web_client.COOLING_MAX_ATTEMPTS
    assert client.last_stop_reason == COOLING_STOPPED


def test_web_client_search_waf_stops_immediately_without_second_page(monkeypatch):
    """WAF 挑战页：第一次响应即抛稳定异常，不再发起第二页。"""
    from app.discovery import web_client

    client = build_offline_client(monkeypatch, 200, WAF_BODY)

    with pytest.raises(WafChallengeError):
        client.search(web_client.SearchRequest(keywords=("fitness coach",)))

    assert len(client.offline_calls) == 1  # 立即停止，不重试、不绕过
    assert client.last_stop_reason is None  # 是异常中止，不是软停止


def test_web_client_search_stops_when_cursor_stops_advancing(monkeypatch):
    """cursor 恒定时必须停止并返回已收集结果（原实现此处会死循环）。"""
    from app.discovery import web_client

    client = build_scripted_client(monkeypatch, [
        (200, ok_page(("a1",), cursor="12").body),
        (200, ok_page(("b1",), cursor="12").body),  # cursor 未推进
        (200, ok_page(("c1",), cursor="36").body),  # 永远不该被拉取
    ])
    records = client.search(web_client.SearchRequest(keywords=("fitness coach",)))

    assert [rec["raw_id"] for rec in records] == ["a1"]  # 重复页不并入结果
    assert len(client.offline_calls) == 2
    assert client.last_stop_reason == PAGINATION_STALLED


def test_web_client_search_stops_at_max_pages(monkeypatch):
    """has_more 恒为真且 cursor 一直推进时，max_pages 是最后一道硬上限。"""
    from app.discovery import web_client

    replies = [(200, ok_page((f"id{i}",), cursor=str((i + 1) * 12)).body)
               for i in range(web_client.MAX_PAGES + 4)]
    client = build_scripted_client(monkeypatch, replies)
    records = client.search(web_client.SearchRequest(keywords=("fitness coach",)))

    assert len(records) == web_client.MAX_PAGES
    assert len(client.offline_calls) == web_client.MAX_PAGES
    assert client.last_stop_reason == PAGINATION_STOPPED


def test_web_client_search_raises_waf_on_slardar_body(monkeypatch):
    from app.discovery import web_client

    client = build_offline_client(monkeypatch, 200, WAF_BODY)
    request = web_client.SearchRequest(keywords=("fitness coach",))

    with pytest.raises(WafChallengeError):
        client.search(request)


def test_web_client_search_raises_auth_failed_on_401(monkeypatch):
    from app.discovery import web_client

    client = build_offline_client(monkeypatch, 401, "")
    with pytest.raises(AuthFailedError):
        client.search(web_client.SearchRequest(keywords=("fitness coach",)))


def test_web_client_search_raises_schema_changed_on_missing_contract(monkeypatch):
    from app.discovery import web_client

    client = build_offline_client(monkeypatch, 200,
                                  json.dumps({"cursor": "12"}))  # 缺 data / status_code
    with pytest.raises(SchemaChangedError):
        client.search(web_client.SearchRequest(keywords=("fitness coach",)))


def test_web_client_health_reports_false_on_cooling_without_raising(monkeypatch):
    from app.discovery import web_client

    client = build_offline_client(monkeypatch, 200, "")
    health = client.health()

    assert health["ok"] is False
    assert health["status"] == COOLING
    # 探针不得回显 body 或会话材料
    for token in FORBIDDEN_TOKENS:
        assert token not in json.dumps(health, ensure_ascii=False)


def test_web_client_fetch_search_page_returns_raw_reply(monkeypatch):
    from app.discovery import web_client

    client = build_offline_client(
        monkeypatch, 200, json.dumps({"status_code": 0, "data": [], "has_more": False}))

    reply = client.fetch_search_page("fitness coach", "12")
    assert isinstance(reply, RawReply)
    assert reply.status == 200
    assert json.loads(reply.body)["status_code"] == 0

