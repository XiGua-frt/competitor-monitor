"""service 包：CLI 与 MCP 的唯一业务入口（Agent D）。

公共导出：
    WatchlistService — watchlist.yaml 增删查
    ScrapeService    — PDP/关键词/Offer 采集 + 异步任务
    MetricsService   — 指标查询封装
    HealthService    — 健康状态探测
"""

from src.service.health_service import HealthService
from src.service.metrics_service import MetricsService
from src.service.scrape_service import (
    ScrapeService,
    get_cooldowns,
    register_cooldown,
)
from src.service.watchlist_service import (
    ASIN_PATTERN,
    WatchlistService,
    WatchlistValidationError,
)

__all__ = [
    "WatchlistService",
    "WatchlistValidationError",
    "ASIN_PATTERN",
    "ScrapeService",
    "MetricsService",
    "HealthService",
    "register_cooldown",
    "get_cooldowns",
]
