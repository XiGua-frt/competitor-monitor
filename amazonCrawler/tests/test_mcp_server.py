"""tests/test_mcp_server.py — MCP Server 单测（Agent E）。

不走真实 stdio：直接经 mcp 框架的内存流建 client/server 会话调用工具
（mcp 2.x 环境，用兼容层取 FastMCP/MCPServer 与 ToolError）；
service 层用假注入对象（configure_services），不依赖 Agent D 是否交付、
不发网络、不碰真实 watchlist.yaml（fake watchlist 只在内存里模拟）。

覆盖：
1. get_asin_snapshot 返回结构（snapshot 注入 fake engine）
2. 非法 ASIN → isError=True 且内容是结构化 JSON（含 ok:false）
3. add/remove watchlist 闭环（fake watchlist service）
4. trigger_scrape → get_task_status 轮询闭环（fake scrape service）
5. 9 个工具全部注册（list_tools 对账）
6. None=不可算 语义不被吞成 0
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest

# mcp 1.x / 2.x 兼容（与 mcp_server/server.py 同一套处理）
try:
    from mcp.server.fastmcp import FastMCP
except Exception:
    from mcp.server.mcpserver import MCPServer as FastMCP

try:
    from mcp.server.fastmcp.exceptions import ToolError
except Exception:
    from mcp.server.mcpserver.exceptions import ToolError

from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

import mcp_server.server as server_module
from mcp_server.server import configure_services, reset_services


# ---------------------------------------------------------------------------
# fake service 层（模拟 Agent D 的接口契约）
# ---------------------------------------------------------------------------

SNAPSHOT_FIXTURE = {
    "asin": "B0TEST1234",
    "snapshot_date": "2026-10-25",
    "marketplace": "US",
    "title": "Fixture Product",
    "price_current": "19.99",
    "price_scope": "display_page_price",
    "variant_status": "fixed",
    "comparison_ready": True,
    "bsr_root_category": "Clothing, Shoes & Jewelry",
    "bsr_ranks": [{"category": "Clothing, Shoes & Jewelry", "rank": 729415}],
    "rating": "4.6",
    "rating_count": 104,
}


class FakeSnapshotEngine:
    """SnapshotDiffEngine 契约：get_latest_snapshot / calculate_growth。"""

    def get_latest_snapshot(self, asin, table="asin_snapshots"):
        if asin == SNAPSHOT_FIXTURE["asin"]:
            return dict(SNAPSHOT_FIXTURE)
        return None

    def calculate_growth(self, asin, days, field="price_current"):
        if asin != SNAPSHOT_FIXTURE["asin"]:
            return {"latest": None, "past": None, "change": None, "change_rate": None}
        return {
            "latest": 19.99,
            "past": 24.99,
            "change": -5.0,
            "change_rate": -0.2,
        }


class FakeWatchlistService:
    """WatchlistService 契约：list_items/add_item/remove_item（内存模拟 yaml 读写）。"""

    def __init__(self):
        self._items: dict[str, dict] = {}

    def list_items(self):
        return list(self._items.values())

    def add_item(self, asin, note="", variant_asin=None):
        item = {"asin": asin, "note": note, "variant_asin": variant_asin}
        self._items[asin] = item
        return item

    def remove_item(self, asin):
        return self._items.pop(asin, None) is not None


class FakeScrapeService:
    """ScrapeService 契约：submit_scrape -> task_id；get_task_status -> {state,...}。
    模拟"提交后立即完成"的后台线程语义，供 trigger→status 闭环断言。"""

    def __init__(self):
        self._tasks: dict[str, dict] = {}
        self._seq = 0

    def submit_scrape(self, asins, channels=None):
        self._seq += 1
        task_id = f"task-{self._seq}"
        # 立即完成：每个 asin 一条 ok 结果（mock 语义，无真实请求）
        self._tasks[task_id] = {
            "state": "completed",
            "progress": {"done": len(asins), "total": len(asins)},
            "errors": [],
            "results": [{"asin": a, "ok": True} for a in asins],
        }
        return task_id

    def get_task_status(self, task_id):
        return self._tasks.get(task_id)


class FakeMetricsService:
    """MetricsService 契约：asin_report/list_reports/find_surging。"""

    def asin_report(self, asin, days=30):
        if asin != SNAPSHOT_FIXTURE["asin"]:
            return {
                "asin": asin,
                "window_days": days,
                "price_change": {"latest": None, "past": None, "change": None, "change_rate": None},
                "bsr_change": None,
                "review_velocity": None,  # None = 不可算（窗口内无两次快照），不是 0
                "availability_downtime": None,
                "buybox_flips": None,
            }
        return {
            "asin": asin,
            "window_days": days,
            "price_change": {"latest": 19.99, "past": 24.99, "change": -5.0, "change_rate": -0.2},
            "bsr_change": {"past": 700000, "latest": 729415, "change": 29415},
            "review_velocity": 3.5,
            "availability_downtime": 0,
            "buybox_flips": 2,
        }

    def list_reports(self, sort_by="review_velocity"):
        return [self.asin_report(SNAPSHOT_FIXTURE["asin"])]

    def find_surging(self, min_review_velocity=5, window_days=7):
        return []


# ---------------------------------------------------------------------------
# 测试基建：内存流 client/server 会话
# ---------------------------------------------------------------------------

def _build_session(server: FastMCP):
    """用 mcp 框架内存流把 server 与 ClientSession 连起来（不走 stdio/网络）。"""
    ctx = create_client_server_memory_streams()
    holder: dict = {}

    class _SessionCtx:
        async def __aenter__(self):
            client_streams, server_streams = await ctx.__aenter__()
            self._streams_ctx = ctx
            init_opts = server._lowlevel_server.create_initialization_options()
            self._task = asyncio.create_task(
                server._lowlevel_server.run(server_streams[0], server_streams[1], init_opts)
            )
            session = ClientSession(*client_streams)
            self._session_ctx = session
            s = await session.__aenter__()
            await s.initialize()
            holder["session"] = s
            return s

        async def __aexit__(self, *exc):
            await holder["session"].__aexit__(*exc)
            self._task.cancel()
            return await self._streams_ctx.__aexit__(*exc)

    return _SessionCtx()


@pytest.fixture()
def wired_server():
    """每测：重置注入 → 注入全套 fake service → 内存会话；测后恢复默认惰性 import。"""
    reset_services()
    configure_services(
        watchlist=FakeWatchlistService(),
        scrape=FakeScrapeService(),
        metrics=FakeMetricsService(),
        snapshot=FakeSnapshotEngine(),
    )
    yield server_module.mcp
    reset_services()


def _parse(text: str) -> dict:
    """解析工具结果为 JSON；mcp 框架会给 ToolError 结果加 'Error executing tool <name>: ' 前缀，剥掉再解。"""
    marker = ": {"
    idx = text.find(marker)
    if text.startswith("Error executing tool") and idx != -1:
        text = text[idx + 2:]
    return json.loads(text)


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------

GOOD_ASIN = "B0TEST1234"
BAD_ASIN = "not-an-asin"


def test_nine_tools_registered(wired_server):
    """9 个工具全部注册且名字与契约一致。"""
    expected = {
        "get_asin_snapshot", "get_price_history", "get_metrics_report",
        "list_competitors", "find_surging_products", "add_watchlist",
        "remove_watchlist", "trigger_scrape", "get_task_status",
    }
    tools = asyncio.run(wired_server.list_tools())
    assert {t.name for t in tools} == expected
    for t in tools:
        assert t.description, f"tool {t.name} 缺 docstring（Agent 选工具的依据）"


def test_get_asin_snapshot_structure(wired_server):
    """snapshot 查询返回结构：ok + snapshot 字段与口径声明齐全。"""
    async def run():
        async with _build_session(wired_server) as session:
            res = await session.call_tool("get_asin_snapshot", {"asin": GOOD_ASIN})
            return res

    res = asyncio.run(run())
    assert res.is_error is False
    payload = _parse(res.content[0].text)
    assert payload["ok"] is True
    snap = payload["snapshot"]
    assert snap["asin"] == GOOD_ASIN
    # 口径声明字段必须透传（Agent 判断对比可用性的依据）
    assert snap["price_scope"] == "display_page_price"
    assert snap["variant_status"] == "fixed"
    assert snap["comparison_ready"] is True
    assert snap["bsr_ranks"] == [{"category": "Clothing, Shoes & Jewelry", "rank": 729415}]


def test_snapshot_missing_returns_ok_false(wired_server):
    """查无快照：业务层"没数据"返回 ok:false + 明确 error，而非空对象。"""
    async def run():
        async with _build_session(wired_server) as session:
            return await session.call_tool(
                "get_asin_snapshot", {"asin": "B0NOEXIST9"}
            )

    res = asyncio.run(run())
    assert res.is_error is False
    payload = _parse(res.content[0].text)
    assert payload["ok"] is False
    assert "no snapshot" in payload["error"]


@pytest.mark.parametrize("tool,args", [
    ("get_asin_snapshot", {"asin": BAD_ASIN}),
    ("get_price_history", {"asin": BAD_ASIN}),
    ("get_metrics_report", {"asin": BAD_ASIN}),
    ("add_watchlist", {"asin": BAD_ASIN}),
    ("remove_watchlist", {"asin": BAD_ASIN}),
    ("trigger_scrape", {"asins": [GOOD_ASIN, BAD_ASIN]}),
])
def test_invalid_asin_is_error(wired_server, tool, args):
    """非法 ASIN → isError=True，内容是结构化 JSON（ok:false + 提示正则），不抛栈。"""
    async def run():
        async with _build_session(wired_server) as session:
            return await session.call_tool(tool, args)

    res = asyncio.run(run())
    assert res.is_error is True, f"{tool} 非法 ASIN 应返回 isError"
    payload = _parse(res.content[0].text)
    assert payload["ok"] is False
    assert "B0[A-Z0-9]{8}" in payload["error"]


def test_invalid_days_rejected(wired_server):
    """days 非正整数 → isError。"""
    async def run():
        async with _build_session(wired_server) as session:
            return await session.call_tool("get_price_history", {"asin": GOOD_ASIN, "days": -1})

    res = asyncio.run(run())
    assert res.is_error is True
    assert _parse(res.content[0].text)["ok"] is False


def test_price_history_none_semantics(wired_server):
    """无快照 ASIN 的 price_change 全 None：None=不可算，不得被吞成 0。"""
    async def run():
        async with _build_session(wired_server) as session:
            return await session.call_tool(
                "get_price_history", {"asin": "B0NOEXIST9", "days": 30}
            )

    res = asyncio.run(run())
    assert res.is_error is False
    payload = _parse(res.content[0].text)
    assert payload["ok"] is True
    pc = payload["price_change"]
    assert pc["latest"] is None and pc["change_rate"] is None


def test_metrics_report_none_not_zero(wired_server):
    """无数据 ASIN 的指标报告：review_velocity 等 None 字段原样透传。"""
    async def run():
        async with _build_session(wired_server) as session:
            return await session.call_tool(
                "get_metrics_report", {"asin": "B0NOEXIST9", "days": 7}
            )

    res = asyncio.run(run())
    assert res.is_error is False
    report = _parse(res.content[0].text)["report"]
    assert report["review_velocity"] is None  # 不是 0
    assert report["window_days"] == 7


def test_metrics_report_full(wired_server):
    """有数据的 ASIN：报告含价格/BSR/评论增速/缺货/BuyBox 全字段。"""
    async def run():
        async with _build_session(wired_server) as session:
            return await session.call_tool("get_metrics_report", {"asin": GOOD_ASIN})

    res = asyncio.run(run())
    report = _parse(res.content[0].text)["report"]
    assert report["review_velocity"] == 3.5
    assert report["buybox_flips"] == 2
    assert report["price_change"]["change_rate"] == -0.2


def test_list_competitors_and_surging(wired_server):
    """list_competitors / find_surging_products 走通 fake metrics。"""
    async def run():
        async with _build_session(wired_server) as session:
            r1 = await session.call_tool("list_competitors", {})
            r2 = await session.call_tool(
                "find_surging_products", {"min_review_velocity": 5, "window_days": 7}
            )
            return r1, r2

    r1, r2 = asyncio.run(run())
    assert r1.is_error is False and r2.is_error is False
    p1 = _parse(r1.content[0].text)
    p2 = _parse(r2.content[0].text)
    assert p1["ok"] is True and p1["count"] == 1
    assert p2["ok"] is True and p2["surging"] == []


def test_watchlist_add_remove_cycle(wired_server):
    """add/remove watchlist 闭环：fake watchlist 内存列表随调用增删。"""
    async def run():
        async with _build_session(wired_server) as session:
            add = await session.call_tool(
                "add_watchlist",
                {"asin": GOOD_ASIN, "note": "竞品A", "variant_asin": "B0VAR00001"},
            )
            rm = await session.call_tool("remove_watchlist", {"asin": GOOD_ASIN})
            rm_again = await session.call_tool("remove_watchlist", {"asin": GOOD_ASIN})
            return add, rm, rm_again

    add, rm, rm_again = asyncio.run(run())
    assert all(r.is_error is False for r in (add, rm, rm_again))

    add_p = _parse(add.content[0].text)
    assert add_p["ok"] is True
    assert add_p["item"]["asin"] == GOOD_ASIN
    assert add_p["item"]["note"] == "竞品A"          # ensure_ascii=False 中文原样
    assert add_p["item"]["variant_asin"] == "B0VAR00001"

    assert _parse(rm.content[0].text)["removed"] is True
    assert _parse(rm_again.content[0].text)["removed"] is False  # 二次移除返回 false


def test_add_watchlist_rejects_bad_asin(wired_server):
    async def run():
        async with _build_session(wired_server) as session:
            return await session.call_tool("add_watchlist", {"asin": "B00 Short"})

    res = asyncio.run(run())
    assert res.is_error is True
    assert _parse(res.content[0].text)["ok"] is False


def test_trigger_scrape_then_poll_status(wired_server):
    """trigger_scrape → get_task_status 闭环：拿 task_id 后轮询到 completed。"""
    async def run():
        async with _build_session(wired_server) as session:
            trig = await session.call_tool(
                "trigger_scrape", {"asins": [GOOD_ASIN], "channels": ["pdp"]}
            )
            assert trig.is_error is False
            task_id = _parse(trig.content[0].text)["task_id"]

            # 轮询模式：直到终态（fake 立即完成，这里也按轮询语义循环）
            for _ in range(10):
                st = await session.call_tool("get_task_status", {"task_id": task_id})
                status = _parse(st.content[0].text)["status"]
                if status["state"] in ("completed", "failed"):
                    return trig, st
            raise AssertionError("task 未进入终态")

    trig, st = asyncio.run(run())
    trig_p = _parse(trig.content[0].text)
    assert trig_p["ok"] is True and re.match(r"^task-\d+$", trig_p["task_id"])
    status = _parse(st.content[0].text)["status"]
    assert status["state"] == "completed"
    assert status["progress"] == {"done": 1, "total": 1}
    assert status["errors"] == []


def test_trigger_scrape_empty_list_rejected(wired_server):
    """空 ASIN 列表 → isError。"""
    async def run():
        async with _build_session(wired_server) as session:
            return await session.call_tool("trigger_scrape", {"asins": []})

    res = asyncio.run(run())
    assert res.is_error is True
    assert _parse(res.content[0].text)["ok"] is False


def test_get_task_status_unknown_task(wired_server):
    """未知 task_id → ok:false（业务层返回 None 的路径）。"""
    async def run():
        async with _build_session(wired_server) as session:
            return await session.call_tool("get_task_status", {"task_id": "no-such-task"})

    res = asyncio.run(run())
    assert res.is_error is False
    assert _parse(res.content[0].text)["ok"] is False


def test_service_unavailable_graceful(wired_server):
    """service 缺失：不注入时调用返回 ok:false 的结构化错误，而不是异常栈。"""
    reset_services()
    # 假装 Agent D 未交付：src.service.watchlist_service 不存在 → 惰性 import 失败
    import sys
    monkey_mods = []
    for name in list(sys.modules):
        if name.startswith("src.service"):
            monkey_mods.append((name, sys.modules.pop(name)))
    sys.modules["src.service"] = None  # import 时抛 ImportError
    try:
        async def run():
            async with _build_session(wired_server) as session:
                return await session.call_tool("add_watchlist", {"asin": GOOD_ASIN})

        res = asyncio.run(run())
        assert res.is_error is True
        payload = _parse(res.content[0].text)
        assert payload["ok"] is False
        assert "unavailable" in payload["error"]
    finally:
        del sys.modules["src.service"]
        for name, mod in monkey_mods:
            sys.modules[name] = mod
    reset_services()


def test_json_ensure_ascii_false(wired_server):
    """中文内容不转义：ensure_ascii=False 契约。"""
    async def run():
        async with _build_session(wired_server) as session:
            return await session.call_tool(
                "add_watchlist", {"asin": GOOD_ASIN, "note": "中文备注→"}
            )

    res = asyncio.run(run())
    assert "中文备注→" in res.content[0].text  # 原文直出，无 \\u 转义
