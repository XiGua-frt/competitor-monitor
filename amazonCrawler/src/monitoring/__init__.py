"""monitoring 包：Prometheus 埋点（无 prometheus_client 时全降级 no-op）。

公共导出：
    register_metrics, observe_request, inc_bot_block, set_missing_ratio,
    start_metrics_server, 及各指标 collector 单例。
"""

from src.monitoring.metrics import (
    amazon_bot_blocks_total,
    data_quality_missing_ratio,
    get_bot_block_count,
    get_missing_ratio,
    get_request_count,
    inc_bot_block,
    observe_request,
    register_metrics,
    scrape_duration_seconds,
    scraper_requests_total,
    set_missing_ratio,
    start_metrics_server,
)

__all__ = [
    "register_metrics",
    "observe_request",
    "inc_bot_block",
    "set_missing_ratio",
    "start_metrics_server",
    "get_request_count",
    "get_bot_block_count",
    "get_missing_ratio",
    "scraper_requests_total",
    "scrape_duration_seconds",
    "amazon_bot_blocks_total",
    "data_quality_missing_ratio",
]
