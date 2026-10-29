"""TikTok Web 自建爬虫客户端（已实机验证，2026-09-29）。

验证结论（详见 docs/2026-09-26_tiktok-creator-discovery-execution-plan.md §20）：
- /api/search/general/full/ 无需 X-Bogus 签名，只要：真实浏览器会话 cookie
  （ttwid/msToken 等）+ Chrome TLS 指纹（curl_cffi impersonate）+ 干净出口 IP。
- profile SSR 页面即使带会话也被 Slardar WAF JS 挑战拦截，不可用；
  作者资料从搜索结果的 item.author / authorStats 获取。
- 出口必须走用户本机代理（系统代理 127.0.0.1:7892），沙箱共享出口会被 WAF 拦。

凭证：从 Chrome 提取的会话存于 .secrets/tiktok_session.json（chmod 600，不入库）。
提取工具：tools/extract_tiktok_session.py。
"""

from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from curl_cffi import requests as creq

from app.transport.pagination import (
    COOLING_STOPPED,
    DEFAULT_BACKOFF_BASE,
    DEFAULT_BACKOFF_CAP,
    DEFAULT_EMPTY_BODY_ATTEMPTS,
    DEFAULT_MAX_PAGES,
    PAGINATION_STALLED,
    PAGINATION_STOPPED,
    backoff_delay,
)
from app.transport.response_classifier import (
    RawReply,
    SUCCESS,
    TransportError,
    UpstreamCoolingError,
    WafChallengeError,  # 由 transport 提供，此处仍可从本模块导入
    classify_payload,
    classify_response,
    decode_json,
    raise_for_outcome,
)

DEFAULT_SESSION_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    ".secrets", "tiktok_session.json")
SEARCH_URL = "https://www.tiktok.com/api/search/general/full/"
IMPERSONATE = "chrome131"
MIN_INTERVAL = (3.0, 6.0)  # 请求间隔下限/上限（秒），保守限速

# 停止策略：与 app/transport/pagination.py 共用同一组默认值，避免两处漂移
MAX_PAGES = DEFAULT_MAX_PAGES                    # 单关键词单轮翻页硬上限
COOLING_MAX_ATTEMPTS = DEFAULT_EMPTY_BODY_ATTEMPTS  # 软冷却退避次数上限
COOLING_BACKOFF_BASE = DEFAULT_BACKOFF_BASE
COOLING_BACKOFF_CAP = DEFAULT_BACKOFF_CAP

_sleep = time.sleep  # 退避等待点：离线用例注入 recorder，不真的等待


@dataclass(frozen=True)
class SearchRequest:
    keywords: tuple[str, ...]
    hashtags: tuple[str, ...] = ()
    region_codes: tuple[str, ...] = ()
    start_date: str | None = None
    end_date: str | None = None
    min_views: int | None = None
    max_results: int = 100
    cursor: str | None = None


def default_proxy() -> str | None:
    """代理优先级：SCOUT_PROXY > 本机系统代理 127.0.0.1:7892 > 无。"""
    if os.environ.get("SCOUT_PROXY"):
        p = os.environ["SCOUT_PROXY"]
        return p if p.startswith("http") else f"http://{p}"
    # 实测沙箱/共享出口会被 WAF 拦；本机代理是当前唯一验证过的出口
    return "http://127.0.0.1:7892"


class TikTokWebClient:
    """按计划 §4.2 的 Discovery Adapter 最小接口实现。"""

    name = "tiktok_web"
    source_scope = "tiktok_web_signed_api_self_crawler"
    # 最近一次 search() 的软停止原因（cooling_stopped / pagination_stalled /
    # pagination_stopped）；异常中止时为 None，便于观测与测试
    last_stop_reason: str | None = None

    def __init__(self, session_file: str | None = None, proxy: str | None = None):
        self._session_file = session_file or os.environ.get(
            "TIKTOK_SESSION_FILE", DEFAULT_SESSION_FILE)
        self._proxy = proxy if proxy is not None else default_proxy()
        self._session: dict[str, str] = {}
        self._last_request_at = 0.0
        self.load_session()

    def load_session(self) -> None:
        with open(self._session_file) as f:
            data = json.load(f)
        self._session = {k: v for k, v in data["cookies"].items() if v.isascii()}
        if not (self._session.get("sessionid") or self._session.get("sid_tt")):
            raise RuntimeError("会话材料缺失登录态（sessionid/sid_tt），请重新提取")
        if not self._session.get("msToken"):
            raise RuntimeError("会话材料缺失 msToken，请重新提取")

    def _throttle(self) -> None:
        wait = random.uniform(*MIN_INTERVAL)
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < wait:
            time.sleep(wait - elapsed)
        self._last_request_at = time.monotonic()

    def _get(self, url: str, params: Mapping[str, Any]) -> creq.Response:
        self._throttle()
        proxies = ({"http": self._proxy, "https": self._proxy}
                   if self._proxy else None)
        s = creq.Session(impersonate=IMPERSONATE, proxies=proxies, timeout=25)
        s.cookies.update(self._session)
        r = s.get(url, params=dict(params))
        body = r.text
        # 在 r.json() 之前先分类：空 body/WAF/认证/限流/5xx 一律转为稳定异常
        outcome = classify_response(r.status_code, body)
        if outcome.kind != SUCCESS:
            raise_for_outcome(outcome)
        return r

    def fetch_search_page(self, keyword: str, cursor: str | None = None) -> RawReply:
        """一次只读搜索请求，返回原始 status/body 交给 transport 策略层分类。

        成功条件不变：curl_cffi chrome131 指纹 + 本机代理 + 3-6 秒 jitter + .secrets 会话。
        """
        offset = int(cursor) if (cursor or "").isdigit() else 0
        r = self._get(SEARCH_URL, {"keyword": keyword, "offset": offset})
        return RawReply(status=r.status_code, body=r.text)

    def search(self, request: SearchRequest) -> list[Mapping]:
        """关键词搜索。返回带来源信息的记录列表（计划 §4.2 契约）。

        停止策略（与 app/transport/pagination.py 同语义，端到端接入到本循环）：
        - 拿到响应后先由 ``classify_response`` 分类，再到 ``decode_json``：空 body
          判定为软冷却，指数退避重试，最多 ``COOLING_MAX_ATTEMPTS`` 次，耗尽则软停止
          并返回已收集结果（``last_stop_reason = cooling_stopped``），绝不无限重试；
        - waf_challenge / auth_failed / forbidden / rate_limited /
          upstream_unavailable 由分类器抛对应稳定异常，本循环不吞、不重试；
        - 单关键词翻页超过 ``MAX_PAGES``、或上游返回的 cursor 不再推进（重复
          cursor）：停止该关键词并返回已收集结果。
        """
        collected = datetime.now(timezone.utc).isoformat()
        out: list[Mapping] = []
        self.last_stop_reason = None
        for kw in request.keywords:
            offset = int(request.cursor) if (request.cursor or "").isdigit() else 0
            seen_cursors: set[str] = set()
            empty_attempts = 0
            pages = 0
            while len(out) < request.max_results:
                if pages >= MAX_PAGES:
                    self.last_stop_reason = PAGINATION_STOPPED
                    break
                if str(offset) in seen_cursors:  # 兜底：成功的页不重复请求
                    self.last_stop_reason = PAGINATION_STALLED
                    break
                try:
                    reply = self.fetch_search_page(kw, str(offset))
                except UpstreamCoolingError:
                    empty_attempts += 1
                    if empty_attempts >= COOLING_MAX_ATTEMPTS:
                        self.last_stop_reason = COOLING_STOPPED
                        return out
                    _sleep(backoff_delay(empty_attempts,
                                         base=COOLING_BACKOFF_BASE,
                                         cap=COOLING_BACKOFF_CAP))
                    continue
                pages += 1
                seen_cursors.add(str(offset))
                d = decode_json(reply.body)
                outcome = classify_payload(d)
                if outcome.kind != SUCCESS:
                    raise_for_outcome(outcome)
                next_cursor = str(d.get("cursor") or offset + 12)
                if d.get("has_more") and next_cursor == str(offset):
                    # cursor 未推进：判定停滞并停止，不把重复页并进结果
                    self.last_stop_reason = PAGINATION_STALLED
                    break
                for it in d["data"]:
                    out.append({
                        "source": self.name,
                        "source_scope": self.source_scope,
                        "authorization_basis": "user_own_browser_session",
                        "collected_at": collected,
                        "raw_id": (it.get("item") or {}).get("id"),
                        "payload": it,
                    })
                if not d.get("has_more"):
                    break
                offset = int(next_cursor)
        return out

    def health(self) -> Mapping:
        try:
            reply = self.fetch_search_page("test", "0")
            outcome = classify_payload(decode_json(reply.body))
            return {"ok": outcome.kind == SUCCESS, "http": reply.status,
                    "status": outcome.kind}
        except TransportError as e:
            # 分类结论本身已是稳定的状态码语义，探针只报告不抛出
            return {"ok": False, "http": None, "status": e.kind,
                    "error": str(e)[:120]}
        except Exception as e:  # noqa: BLE001 - 探针不应抛出，只报告状态
            return {"ok": False, "http": None, "status": "unknown",
                    "error": str(e)[:120]}

    def quota(self) -> Mapping:
        # 自建爬虫无配额概念，仅暴露限速策略
        return {"rate_limit": f"{MIN_INTERVAL[0]}-{MIN_INTERVAL[1]}s random jitter",
                "session_file": self._session_file}
