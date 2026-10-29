"""Task 9：真实 Transport 的小规模验收（显式 opt-in，默认永不执行）。

默认态（不设 ``SCOUT_LIVE_SMOKE=1``）下本文件被整体 skip，不会构造
``TikTokWebClient``、不读 ``.secrets/`` 会话、不发出任何网络请求。
``pytest -q`` 全量运行时本文件对网络零影响，可放心纳入回归。

运行红线（执行本文件前必须逐条确认，任一不满足就不要跑）：
- 会话仍是**用户本人**已授权的浏览器登录态（非他人、非购买账号）；
- 出口走用户本机代理 ``127.0.0.1:7892`` 且当前可用，不使用沙箱/共享出口；
- 当前没有软冷却（此前没有连续空 body / 限流）；
- 只查询**一个关键词**（fitness coach），单次运行只发这一个搜索；
- 最多取 5 条（``SearchRequest(max_results=5)``）；
- 遵守 3-6 秒 jitter 限速（由 ``web_client.MIN_INTERVAL`` 保证，不要改小）；
- 出现 WAF 挑战、空 body、登录失效（sessionid/sid_tt/msToken 失效）时
  **立即停止**，不重试、不换池、不换 IP、不绕过；
- 不并发、不循环压测、不批量关键词、不做性能/压力测试。

启用方式（用户确认后由编排方执行，本文件不自动触发）：

    SCOUT_LIVE_SMOKE=1 \
    /Users/wuwenbo/.workbuddy/binaries/python/envs/default/bin/python \
        -m pytest tests/test_live_smoke.py -q
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest

from app.discovery import web_client
from app.transport.pagination import (
    COOLING_STOPPED,
    PAGINATION_STALLED,
    PAGINATION_STOPPED,
)

# search() 返回的每条记录形如：
# {"source", "source_scope", "authorization_basis", "collected_at",
#  "raw_id", "payload"}；payload 是 data[] 里的原始记录（user_list 或 item）。
EXPECTED_RECORD_KEYS = {
    "source",
    "source_scope",
    "authorization_basis",
    "collected_at",
    "raw_id",
    "payload",
}
KNOWN_STOP_REASONS = {None, COOLING_STOPPED, PAGINATION_STALLED, PAGINATION_STOPPED}


@pytest.mark.skipif(
    os.getenv("SCOUT_LIVE_SMOKE") != "1",
    reason="live TikTok smoke test requires explicit opt-in",
)
def test_live_single_keyword_search():
    """单关键词、单搜、最多 5 条的真实端到端验收。"""
    client = web_client.TikTokWebClient()
    request = web_client.SearchRequest(keywords=("fitness coach",), max_results=5)
    rows = client.search(request)

    assert rows, "真实搜索返回空（可能是 WAF/冷却/登录失效）：立即停止，不要重跑"
    assert all(row["source"] == "tiktok_web" for row in rows)
    # 记录形态与 provenance 契约一致，且不含 cookie/token 等敏感材料
    for row in rows:
        assert set(row) == EXPECTED_RECORD_KEYS
        assert row["source_scope"] == "tiktok_web_signed_api_self_crawler"
        assert row["authorization_basis"] == "user_own_browser_session"
        assert row["collected_at"]
    # 软停止原因必须可见（None 表示正常跑满），便于人工判读冷却/翻页停滞
    assert client.last_stop_reason in KNOWN_STOP_REASONS
