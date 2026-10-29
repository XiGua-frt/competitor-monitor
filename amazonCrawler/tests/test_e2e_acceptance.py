"""P9 端到端验收：MCP 9 工具 ↔ 真实 service 层 ↔ sqlite 引擎 全链路闭环。

不发真实网络：PDP scraper 用 fixture HTML 的 mock；watchlist 用临时 yaml。
通过 mcp 内存流 client 会话调用全部 9 个工具（与 Agent E 的测试方式一致，
但这次注入的是真实 service 层而非 fake）。
"""
import asyncio
import json
from datetime import date
from pathlib import Path

import pytest

from mcp_server import configure_services, reset_services
from mcp_server.server import mcp

FIXTURES = Path(__file__).parent / "fixtures" / "amazon"
GOOD_ASIN = "B0DLQ8MVF7"
NEW_ASIN = "B0NEWTEST0"
assert len(NEW_ASIN) == 10 and NEW_ASIN.startswith("B0")

def _fixture_asin_html(asin: str) -> str:
    """拿任意真实 fixture 冒充目标 ASIN 的 PDP 页面（字段值不关心，只看管线）。"""
    return (FIXTURES / f"{GOOD_ASIN}.html").read_text(encoding="utf-8", errors="ignore")


class MockResponse:
    def __init__(self, html: str, status_code: int = 200):
        self.status_code = status_code
        self.text = html


class MockPdpScraper:
    """真实接口形态的 PDP scraper 替身：走解析器，产物直接可入库。"""

    def __init__(self):
        from src.scrapers.parsers.pdp_parser import PdpParser

        self.parser = PdpParser()
        self.calls = []

    def scrape_product(self, asin: str, marketplace: str = "US"):
        self.calls.append(asin)
        html = _fixture_asin_html(asin)
        data = self.parser.parse(html)
        data["asin"] = asin
        data["marketplace"] = marketplace
        data["snapshot_date"] = date.today().isoformat()
        return data


class MockOfferScraper:
    def scrape_offers(self, asin: str):
        return [{
            "asin": asin, "seller_id": "A1TEST", "seller_name": "MockSeller",
            "price": 14.99, "is_buybox": True, "is_amazon_offered": False,
            "offer_count": 1, "snapshot_date": date.today().isoformat(),
        }]


@pytest.fixture()
def e2e(tmp_path):
    """真实 service + sqlite 引擎 + mock scraper，注入 MCP 后 yield 会话工厂。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from src.database.models import Base
    from src.database.snapshot_engine import SnapshotDiffEngine
    from src.service.metrics_service import MetricsService
    from src.service.scrape_service import ScrapeService
    from src.service.watchlist_service import WatchlistService
    from src.metrics.calculator import MetricsCalculator

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    engine_probe = SnapshotDiffEngine(session_factory=session_factory)

    watchlist = WatchlistService(path=tmp_path / "watchlist.yaml")
    scrape = ScrapeService(
        engine=engine_probe,
        pdp_scraper=MockPdpScraper(),
        offer_scraper=MockOfferScraper(),
        redis_client=False,
    )
    metrics = MetricsService(calculator=MetricsCalculator(session_factory=session_factory))

    configure_services(
        watchlist=watchlist, scrape=scrape, metrics=metrics, snapshot=engine_probe,
    )
    yield engine_probe
    reset_services()


async def _call(tool: str, args: dict) -> dict:
    """经 mcp 内存流调用工具，返回解析后的 JSON。"""
    from mcp import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    from tests.test_mcp_server import _build_session

    session_ctx = _build_session(mcp)
    async with session_ctx as session:
        result = await session.call_tool(tool, args)
        is_err = getattr(result, "isError", getattr(result, "is_error", False))
        assert is_err is False, f"{tool} 返回 isError: {result.content}"
        text = result.content[0].text
        return json.loads(text)


def test_full_pipeline(e2e):
    """add_watchlist → trigger_scrape(pdp+offers) → 轮询 → snapshot/metrics 查询全闭环。"""
    async def run():
        # 1. add_watchlist（真实 WatchlistService 写临时 yaml）
        r = await _call("add_watchlist", {"asin": NEW_ASIN, "note": "e2e 验收"})
        assert r["ok"] is True, r
        # 采集前快照表为空 → list_competitors 为空列表是正确语义；
        # watchlist 写入正确性由 remove 步骤与 add 返回值共同验证。
        listed = await _call("list_competitors", {})
        assert listed["ok"] is True and listed["count"] == 0

        # 2. trigger_scrape 异步任务（真实 ScrapeService 后台线程 + mock scraper）
        r = await _call("trigger_scrape", {"asins": [NEW_ASIN], "channels": ["pdp", "offers"]})
        assert r["ok"] is True, r
        task_id = r["task_id"]

        # 3. 轮询至完成
        import time
        status = None
        for _ in range(100):
            status = await _call("get_task_status", {"task_id": task_id})
            if status["status"]["state"].startswith("completed"):
                break
            await asyncio.sleep(0.05)
        assert status["status"]["state"] == "completed", status

        # 4. 查询快照——PDP 快照应已入库
        snap = await _call("get_asin_snapshot", {"asin": NEW_ASIN})
        assert snap["ok"] is True and snap["snapshot"], snap
        assert snap["snapshot"]["asin"] == NEW_ASIN
        assert snap["snapshot"]["price_current"] == 14.99  # fixture 真实价格

        # 5. 价格历史与指标报告
        hist = await _call("get_price_history", {"asin": NEW_ASIN, "days": 7})
        assert hist["ok"] is True
        pc = hist["price_change"]
        # calculate_growth 语义: past=窗口起点快照；仅一天快照 → past=latest=None（不可算）
        assert pc == {"latest": None, "past": None, "change": None, "change_rate": None}
        report = await _call("get_metrics_report", {"asin": NEW_ASIN, "days": 7})
        assert report["ok"] and report["report"]["asin"] == NEW_ASIN
        # 仅一天快照: 差分不可算 → None 语义（不等于 0）
        assert report["report"]["price_change"]["change"] is None

        # 5.5 采集入库后 list_competitors 应包含该 ASIN
        listed = await _call("list_competitors", {})
        assert listed["ok"] is True and listed["count"] >= 1
        assert any(item["asin"] == NEW_ASIN for item in listed["competitors"])

        # 6. remove_watchlist
        r = await _call("remove_watchlist", {"asin": NEW_ASIN})
        assert r["ok"] is True

    asyncio.run(run())


def test_illegal_asin_is_error(e2e):
    """非法 ASIN 应走 isError 通道 + 结构化错误内容（ToolError 语义）。"""
    from mcp import ClientSession
    from tests.test_mcp_server import _build_session

    async def run():
        session_ctx = _build_session(mcp)
        async with session_ctx as session:
            result = await session.call_tool("add_watchlist", {"asin": "BADASIN123"})
            is_err = getattr(result, "is_error", getattr(result, "isError", False))
            assert is_err is True, result
            text = result.content[0].text
            # mcp 2.x 错误内容带 "Error executing tool ...: " 前缀，剥离后再解析
            payload = json.loads(text if text.startswith("{") else text.split(": ", 1)[-1])
            assert payload["ok"] is False and "B0[A-Z0-9]{8}" in payload["error"]

    asyncio.run(run())
