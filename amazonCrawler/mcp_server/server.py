"""amazonCrawler MCP Server — Agent E 交付物。

把 CONTRACT_V2.md 定义的 9 个工具暴露给 MCP 客户端（Claude Code / Claude Desktop）。
所有业务逻辑走 src/service/*（Agent D 交付的业务服务层，CLI 与 MCP 的唯一入口）；
snapshot/价格历史直查 src/database 的 SnapshotDiffEngine（已就绪的真实接口）。

为什么用兼容导入：环境安装的是 mcp 2.x，FastMCP 已更名为 MCPServer
（mcp.server.fastmcp 在 2.x 下 import 即抛 ModuleNotFoundError）。
这里先尝试 1.x 的 FastMCP，失败则回落 2.x 的 MCPServer，两者 tool() 装饰器与
run(transport='stdio') 行为一致；ToolError 同理做双路径导入。

口径声明（引用 src/scrapers/parsers/pdp_parser.py 顶部 docstring 与解析产物）：
- price_scope 固定为 'display_page_price'：价格是商品展示页抓到的 BuyBox 显示价，
  不是 offer 列表最低价；跨商品对比时必须保证口径一致。
- variant_status='fixed' 表示解析器已从变体下拉提取 variant_asin 并固定了商品身份；
  'none' 表示该 ASIN 未固定变体，跨期对比可能混入变体漂移。
- comparison_ready=True 表示字段口径一致、可横向对比。
- 所有指标 None = 不可算（缺快照/除零），语义上不等于 0，禁止当 0 参与计算。
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

# --- mcp 1.x / 2.x 兼容层 ---------------------------------------------------
# 2.x 把 FastMCP 更名为 MCPServer，且 mcp.server.fastmcp 顶部直接 raise。
try:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP  # type: ignore[attr-defined]
except Exception:  # mcp 2.x（当前环境）
    from mcp.server.mcpserver import MCPServer as FastMCP  # type: ignore[no-redef]

try:  # ToolError 双路径：抛给框架后客户端会收到 isError=True 的工具结果
    from mcp.server.fastmcp.exceptions import ToolError  # type: ignore[attr-defined]
except Exception:
    from mcp.server.mcpserver.exceptions import ToolError  # type: ignore[no-redef]

ASIN_RE = re.compile(r"^B0[A-Z0-9]{8}$")

SERVICE_IMPORT_STATUS: dict[str, str] = {
    "watchlist": "not_imported",
    "scrape": "not_imported",
    "metrics": "not_imported",
    "snapshot": "not_imported",
}
"""每个 service 槽的对接状态，便于报告与运行时诊断：ok / injected / unavailable:<err>"""

# 依赖注入槽。configure_services() 注入的优先；否则惰性 import 真实实现。
# Agent D 未交付时 import 失败 → 记录到 SERVICE_IMPORT_STATUS，工具调用时报
# {"ok": false, "error": ...}，而不是让进程启动就挂。
_services: dict[str, Any | None] = {
    "watchlist": None,
    "scrape": None,
    "metrics": None,
    "snapshot": None,
}

mcp = FastMCP(
    "amazon-crawler",
    instructions=(
        "amazonCrawler 竞品监控：查询 ASIN 快照/价格历史/指标报告，管理 watchlist，"
        "提交并轮询 Amazon 采集任务。价格口径 price_scope=display_page_price（展示页价，"
        "非 offer 最低价）；指标 None=不可算，不等于 0。详细用法见 skills/amazon-crawler/SKILL.md。"
    ),
)


# --- 依赖注入与惰性 service 构造 ---------------------------------------------
def configure_services(
    watchlist: Any | None = None,
    scrape: Any | None = None,
    metrics: Any | None = None,
    snapshot: Any | None = None,
) -> None:
    """注入 service 实现供测试或自定义部署使用；传 None 的槽保持不变。

    - watchlist: WatchlistService（list_items/add_item/remove_item）
    - scrape:    ScrapeService（submit_scrape/get_task_status/scrape_pdp_now/...）
    - metrics:   MetricsService（asin_report/list_reports/find_surging）
    - snapshot:  SnapshotDiffEngine（get_latest_snapshot/calculate_growth）——第四个
                 可选槽，供 get_asin_snapshot/get_price_history 注入；默认槽仍按契约
                 只要求前三个。
    """
    for name, obj in (
        ("watchlist", watchlist),
        ("scrape", scrape),
        ("metrics", metrics),
        ("snapshot", snapshot),
    ):
        if obj is not None:
            _services[name] = obj
            SERVICE_IMPORT_STATUS[name] = "injected"


def reset_services() -> None:
    """测试用：清空全部注入，恢复惰性 import 默认行为。"""
    for name in _services:
        _services[name] = None
    for name in SERVICE_IMPORT_STATUS:
        SERVICE_IMPORT_STATUS[name] = "not_imported"


def _default_watchlist() -> Any:
    from src.service.watchlist_service import WatchlistService

    return WatchlistService()


def _default_scrape() -> Any:
    from src.service.scrape_service import ScrapeService

    return ScrapeService()


def _default_metrics() -> Any:
    from src.service.metrics_service import MetricsService

    return MetricsService()


def _default_snapshot() -> Any:
    from src.database import SnapshotDiffEngine

    return SnapshotDiffEngine()


_DEFAULT_FACTORIES = {
    "watchlist": _default_watchlist,
    "scrape": _default_scrape,
    "metrics": _default_metrics,
    "snapshot": _default_snapshot,
}


def _get_service(name: str) -> Any:
    """取 service：优先注入对象；否则惰性 import（失败只报错不炸进程）。"""
    if _services[name] is not None:
        return _services[name]
    try:
        obj = _DEFAULT_FACTORIES[name]()
    except Exception as exc:  # ImportError / 依赖缺失（如 redis、sqlalchemy 配置）
        SERVICE_IMPORT_STATUS[name] = f"unavailable:{exc}"
        raise ToolError(
            json.dumps(
                {
                    "ok": False,
                    "error": (
                        f"service '{name}' unavailable: {exc}. "
                        "src/service 层（Agent D）可能未交付或依赖未安装；"
                        "可用 configure_services() 注入 fake 实现。"
                    ),
                },
                ensure_ascii=False,
            )
        ) from exc
    _services[name] = obj
    SERVICE_IMPORT_STATUS[name] = "ok"
    return obj


def _dump(payload: Any) -> str:
    """统一 JSON 输出：ensure_ascii=False；Decimal/date 等对象退化为 str。"""
    return json.dumps(payload, ensure_ascii=False, default=str)


def _check_asin(asin: str, param: str = "asin") -> None:
    """ASIN 校验：非法 raise ToolError → 客户端收到 isError=True + 结构化错误。"""
    if not isinstance(asin, str) or not ASIN_RE.match(asin):
        raise ToolError(
            _dump(
                {
                    "ok": False,
                    "error": f"invalid {param}: {asin!r}. ASIN 必须匹配 ^B0[A-Z0-9]{{8}}$（10 位，B0 开头）",
                }
            )
        )


def _check_asin_list(asins: list[str], param: str = "asins") -> None:
    if not isinstance(asins, list) or not asins:
        raise ToolError(
            _dump({"ok": False, "error": f"{param} 必须是非空 ASIN 列表"})
        )
    for a in asins:
        _check_asin(a, param)


# --- 9 个工具 ----------------------------------------------------------------
# 工具 docstring 是 Agent 选工具的依据，写清用途/参数/返回。


@mcp.tool()
def get_asin_snapshot(asin: str, marketplace: str = "US", date: str | None = None) -> str:
    """查询单个 ASIN 的最新（或指定日期）商品快照。

    参数:
      asin: 10 位 ASIN，格式 ^B0[A-Z0-9]{8}$
      marketplace: 站点标识，默认 "US"
      date: 可选 ISO 日期（YYYY-MM-DD）取历史某天快照；引擎未提供按日期查询时
            返回最新快照并附 note。默认 None = 最新快照。
    返回: {"ok": true, "snapshot": {...}} — 字段即 asin_snapshots 列名
      （price_current/price_scope/variant_status/comparison_ready/bsr_ranks 等）。
      注意 price_scope='display_page_price'（展示页价）与 None=缺数据语义。
    """
    _check_asin(asin)
    engine = _get_service("snapshot")
    try:
        snap: dict | None = None
        note: str | None = None
        if date is not None:
            by_date = getattr(engine, "get_snapshot_at", None)
            if callable(by_date):
                snap = by_date(asin, date, marketplace=marketplace)
            else:
                note = (
                    f"snapshot engine 无按日期查询接口，date={date} 被忽略，返回最新快照"
                )
        if snap is None:
            snap = engine.get_latest_snapshot(asin, "asin_snapshots")
        if snap is None:
            return _dump(
                {
                    "ok": False,
                    "error": f"no snapshot found for asin={asin} marketplace={marketplace}",
                }
            )
        payload: dict[str, Any] = {"ok": True, "snapshot": snap}
        if note:
            payload["note"] = note
        return _dump(payload)
    except ToolError:
        raise
    except Exception as exc:
        return _dump({"ok": False, "error": str(exc)})


@mcp.tool()
def get_price_history(asin: str, days: int = 30) -> str:
    """查询 ASIN 近 N 天价格变动（基于快照差分）。

    参数:
      asin: 10 位 ASIN，格式 ^B0[A-Z0-9]{8}$
      days: 回看窗口天数，默认 30
    返回: {"ok": true, "price_change": {latest, past, change, change_rate}}
      latest/past 为窗口首尾快照价格；任一端缺快照或除零时字段为 None
      （None=不可算，不是 0，禁止当 0 参与计算）。
      价格口径 price_scope='display_page_price'：展示页 BuyBox 价，非 offer 最低价。
    """
    _check_asin(asin)
    if not isinstance(days, int) or days <= 0:
        raise ToolError(_dump({"ok": False, "error": f"invalid days: {days!r}，必须为正整数"}))
    engine = _get_service("snapshot")
    try:
        change = engine.calculate_growth(asin, days, field="price_current")
        return _dump({"ok": True, "asin": asin, "days": days, "price_change": change})
    except ToolError:
        raise
    except Exception as exc:
        return _dump({"ok": False, "error": str(exc)})


@mcp.tool()
def get_metrics_report(asin: str, days: int = 30) -> str:
    """获取单个 ASIN 的综合指标报告（价格变动/BSR 变动/评论增速/缺货天数/BuyBox 易手）。

    参数:
      asin: 10 位 ASIN，格式 ^B0[A-Z0-9]{8}$
      days: 统计窗口天数，默认 30
    返回: {"ok": true, "report": {asin, window_days, price_change, bsr_change,
      review_velocity, availability_downtime, buybox_flips}}
      字段解读：bsr_change 基于 bsr_ranks 首条 rank 对比（BSR 是 JSON，不做数值差分）；
      review_velocity 为每日评论增速；任何字段 None=窗口内数据不足以计算，不等于 0。
    """
    _check_asin(asin)
    if not isinstance(days, int) or days <= 0:
        raise ToolError(_dump({"ok": False, "error": f"invalid days: {days!r}，必须为正整数"}))
    metrics = _get_service("metrics")
    try:
        report = metrics.asin_report(asin, days=days)
        return _dump({"ok": True, "report": report})
    except ToolError:
        raise
    except Exception as exc:
        return _dump({"ok": False, "error": str(exc)})


@mcp.tool()
def list_competitors(category: str | None = None, sort_by: str = "review_velocity") -> str:
    """列出 watchlist 内竞品的指标报告，按指定指标排序。

    参数:
      category: 可选，按 BSR 根类目名过滤（子串匹配）；None = 不过滤
      sort_by: 排序字段，默认 "review_velocity"
    返回: {"ok": true, "competitors": [report, ...]}，每个 report 结构同
      get_metrics_report 的 report。排序字段缺失/为 None 的项排在最后，不会被当 0。
    """
    metrics = _get_service("metrics")
    try:
        reports = metrics.list_reports(sort_by=sort_by)
        if category:
            reports = [
                r for r in reports
                if category in str((r or {}).get("bsr_change", {}).get("category", "")
                                   or (r or {}).get("bsr_root_category", ""))
            ]
        return _dump({"ok": True, "count": len(reports), "competitors": reports})
    except ToolError:
        raise
    except Exception as exc:
        return _dump({"ok": False, "error": str(exc)})


@mcp.tool()
def find_surging_products(min_review_velocity: float = 5, window_days: int = 7) -> str:
    """发现评论增速超阈值的爆品（扫 watchlist 全部 ASIN 按增速排序）。

    参数:
      min_review_velocity: 每日评论数增速下限，默认 5
      window_days: 统计窗口天数，默认 7
    返回: {"ok": true, "surging": [{asin, review_velocity, ...}, ...]}
      review_velocity=None 的 ASIN 视为不可算而不入选（不是增速 0）。
    """
    if not isinstance(window_days, int) or window_days <= 0:
        raise ToolError(_dump({"ok": False, "error": f"invalid window_days: {window_days!r}"}))
    metrics = _get_service("metrics")
    try:
        items = metrics.find_surging(
            min_review_velocity=min_review_velocity, window_days=window_days
        )
        return _dump({"ok": True, "count": len(items), "surging": items})
    except ToolError:
        raise
    except Exception as exc:
        return _dump({"ok": False, "error": str(exc)})


@mcp.tool()
def add_watchlist(asin: str, note: str = "", variant_asin: str | None = None) -> str:
    """把 ASIN 加入 watchlist（写入 src/scheduler/config/watchlist.yaml）。

    参数:
      asin: 10 位 ASIN，格式 ^B0[A-Z0-9]{8}$
      note: 备注，默认 ""
      variant_asin: 可选，要固定的变体 ASIN（固定后 variant_status='fixed'，
        跨期对比才不会被变体漂移污染）
    返回: {"ok": true, "item": {asin, note, variant_asin}}
    """
    _check_asin(asin)
    watchlist = _get_service("watchlist")
    try:
        item = watchlist.add_item(asin, note=note, variant_asin=variant_asin)
        return _dump({"ok": True, "item": item})
    except ToolError:
        raise
    except Exception as exc:
        return _dump({"ok": False, "error": str(exc)})


@mcp.tool()
def remove_watchlist(asin: str) -> str:
    """把 ASIN 从 watchlist 移除（同步写回 watchlist.yaml）。

    参数:
      asin: 10 位 ASIN，格式 ^B0[A-Z0-9]{8}$
    返回: {"ok": true, "removed": true/false} — false 表示原列表中不存在。
    """
    _check_asin(asin)
    watchlist = _get_service("watchlist")
    try:
        removed = watchlist.remove_item(asin)
        return _dump({"ok": True, "asin": asin, "removed": bool(removed)})
    except ToolError:
        raise
    except Exception as exc:
        return _dump({"ok": False, "error": str(exc)})


@mcp.tool()
def trigger_scrape(asins: list[str], channels: list[str] | None = None) -> str:
    """提交批量采集任务（异步，后台执行），返回 task_id 后用 get_task_status 轮询。

    参数:
      asins: ASIN 列表（每个都必须匹配 ^B0[A-Z0-9]{8}$），至少 1 个
      channels: 采集通道，默认 ["pdp"]；后续可扩展 keyword/offers
    返回: {"ok": true, "task_id": "..."} — 提交成功≠采集成功，务必轮询状态。
    [!] 采集会真实请求 Amazon；触发反爬（BotBlocked）会进入 1 小时软冷却，
    冷却期间任务会记 error 而不是立刻重试，应降低触发频率。
    """
    channels = channels or ["pdp"]
    _check_asin_list(asins)
    scrape = _get_service("scrape")
    try:
        task_id = scrape.submit_scrape(asins, channels=channels)
        return _dump({"ok": True, "task_id": task_id, "asins": asins, "channels": channels})
    except ToolError:
        raise
    except Exception as exc:
        return _dump({"ok": False, "error": str(exc)})


@mcp.tool()
def get_task_status(task_id: str) -> str:
    """查询 trigger_scrape 提交的采集任务状态（轮询用）。

    参数:
      task_id: trigger_scrape 返回的任务 ID
    返回: {"ok": true, "status": {state, progress, errors}}
      state ∈ pending/running/completed/failed；progress 含完成进度；
      errors 含逐 ASIN 失败原因（如 BotBlocked）。
      轮询建议：提交后每 30-60s 查一次，直到 state 进入终态。
    """
    if not task_id or not isinstance(task_id, str):
        raise ToolError(_dump({"ok": False, "error": "task_id 必须是非空字符串"}))
    scrape = _get_service("scrape")
    try:
        status = scrape.get_task_status(task_id)
        if status is None:
            return _dump({"ok": False, "error": f"unknown task_id: {task_id}"})
        return _dump({"ok": True, "task_id": task_id, "status": status})
    except ToolError:
        raise
    except Exception as exc:
        return _dump({"ok": False, "error": str(exc)})


def main() -> None:
    """stdio 入口：python -m mcp_server。Agent D 未交付时不阻塞启动（惰性 import）。"""
    mcp.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
