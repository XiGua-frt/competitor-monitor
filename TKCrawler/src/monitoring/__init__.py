"""TKCrawler monitoring package.

Exposes Prometheus metrics and convenience helpers for the scraper layer.
See docs/monitoring/README.md for usage.
"""

from .metrics import (
    data_quality_missing_fields_ratio,
    get_missing_ratio,
    get_request_count,
    get_waf_block_count,
    inc_waf_block,
    observe_request,
    register_metrics,
    scrape_duration_seconds,
    scraper_requests_total,
    set_missing_ratio,
    start_metrics_server,
    track_request,
)

__all__ = [
    "scraper_requests_total",
    "scrape_duration_seconds",
    "data_quality_missing_fields_ratio",
    "observe_request",
    "track_request",
    "inc_waf_block",
    "set_missing_ratio",
    "get_request_count",
    "get_waf_block_count",
    "get_missing_ratio",
    "register_metrics",
    "start_metrics_server",
]
