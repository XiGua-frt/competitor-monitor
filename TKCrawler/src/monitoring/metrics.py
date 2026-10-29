"""Prometheus metrics for TKCrawler.

Defines the scraper metrics required by Agent J task card and provides
convenience functions for the scraper layer to record request outcomes,
WAF blocks and data-quality ratios.

Metrics:
    scraper_requests_total{scraper, status}          Counter  - 请求总数(按爬虫与结果)
    scraper_request_duration_seconds{scraper}        Histogram- 请求耗时分布
    scraper_waf_blocks_total{scraper}                Counter  - WAF 拦截次数
    data_quality_missing_fields_ratio{table}         Gauge    - 表级空字段率

Usage (scraper layer):
    from src.monitoring import observe_request, inc_waf_block, set_missing_ratio

    start = time.monotonic()
    try:
        ...do request...
        observe_request("shop", "success", time.monotonic() - start)
    except WAFBlockedException:
        inc_waf_block("shop")
        observe_request("shop", "waf_blocked", time.monotonic() - start)

Registration is idempotent: calling :func:`register_metrics` repeatedly or
re-importing this module never raises duplicate-timeseries errors.
"""

import os
import time
from contextlib import contextmanager

from prometheus_client import (
    REGISTRY,
    Counter,
    Gauge,
    Histogram,
    start_http_server,
)

# ---------------------------------------------------------------------------
# 指标名（与 AGENT_TASK_CARDS.md / prometheus.yml / 告警规则保持一致）
# ---------------------------------------------------------------------------

REQUESTS_TOTAL = "scraper_requests_total"
DURATION_SECONDS = "scraper_request_duration_seconds"
WAF_BLOCKS_TOTAL = "scraper_waf_blocks_total"
MISSING_FIELDS_RATIO = "data_quality_missing_fields_ratio"

REQUESTS_HELP = "Total number of scraper requests by scraper and result status."
DURATION_HELP = "Duration of scraper requests in seconds."
WAF_HELP = "Total number of requests blocked by TikTok WAF."
MISSING_HELP = "Ratio of rows with missing critical fields per table (0.0-1.0)."

# 爬虫请求耗时桶：覆盖 0.1s ~ 60s（受 5-8s 速率限制影响，正常请求在秒级）
DURATION_BUCKETS = (0.1, 0.5, 1, 2, 5, 8, 15, 30, 60, float("inf"))


def _get_or_create(cls, name, doc, labelnames, registry=REGISTRY, **kwargs):
    """Create a collector, or return the existing one with the same name.

    Makes registration idempotent across module reloads and repeated
    register_metrics() calls.
    """
    existing = getattr(registry, "_names_to_collectors", {}).get(name)
    if existing is not None:
        return existing
    try:
        return cls(name, doc, labelnames, registry=registry, **kwargs)
    except ValueError:
        # Race / already registered without the lookup above working
        return registry._names_to_collectors[name]


def register_metrics(registry=REGISTRY):
    """Idempotently register all crawler metrics into ``registry``.

    Returns a dict of the metric collectors.
    """
    requests_total = _get_or_create(
        Counter, REQUESTS_TOTAL, REQUESTS_HELP, ["scraper", "status"], registry
    )
    duration = _get_or_create(
        Histogram, DURATION_SECONDS, DURATION_HELP,
        ["scraper"], registry, buckets=DURATION_BUCKETS,
    )
    waf_blocks = _get_or_create(
        Counter, WAF_BLOCKS_TOTAL, WAF_HELP, ["scraper"], registry
    )
    missing_ratio = _get_or_create(
        Gauge, MISSING_FIELDS_RATIO, MISSING_HELP, ["table"], registry
    )
    return {
        "requests_total": requests_total,
        "duration_seconds": duration,
        "waf_blocks_total": waf_blocks,
        "missing_fields_ratio": missing_ratio,
    }


# Module-level singletons (safe on re-import thanks to _get_or_create).
_metrics = register_metrics()
scraper_requests_total = _metrics["requests_total"]
scrape_duration_seconds = _metrics["duration_seconds"]
scraper_waf_blocks_total = _metrics["waf_blocks_total"]
data_quality_missing_fields_ratio = _metrics["missing_fields_ratio"]


# ---------------------------------------------------------------------------
# 便捷函数：供采集层调用
# ---------------------------------------------------------------------------

def observe_request(scraper: str, status: str, duration: float) -> None:
    """Record one scraper request: total counter + duration histogram.

    :param scraper:  爬虫名，如 "shop" / "video" / "creator"
    :param status:   "success" / "failed" / "waf_blocked" 等
    :param duration: 请求耗时（秒）
    """
    scraper_requests_total.labels(scraper=scraper, status=status).inc()
    scrape_duration_seconds.labels(scraper=scraper).observe(duration)


@contextmanager
def track_request(scraper: str, status: str = "success"):
    """Context manager that times a request and records it on exit.

    Usage:
        with track_request("shop"):
            ...do request...

    Exceptions propagate; the request is recorded as "failed" before re-raise.
    """
    start = time.monotonic()
    try:
        yield
    except Exception:
        observe_request(scraper, "failed", time.monotonic() - start)
        raise
    else:
        observe_request(scraper, status, time.monotonic() - start)


def inc_waf_block(scraper: str) -> None:
    """Increment the WAF-blocked counter for ``scraper``."""
    scraper_waf_blocks_total.labels(scraper=scraper).inc()


def set_missing_ratio(table: str, ratio: float) -> None:
    """Set the missing-fields ratio (0.0-1.0) for ``table``.

    :param table: 表名，如 "product_snapshots" / "video_snapshots"
    """
    data_quality_missing_fields_ratio.labels(table=table).set(float(ratio))


# ---------------------------------------------------------------------------
# 查询辅助（主要供测试与本地调试）
# ---------------------------------------------------------------------------

def get_request_count(scraper: str, status: str) -> float:
    return scraper_requests_total.labels(
        scraper=scraper, status=status
    )._value.get()


def get_waf_block_count(scraper: str) -> float:
    return scraper_waf_blocks_total.labels(scraper=scraper)._value.get()


def get_missing_ratio(table: str) -> float:
    return data_quality_missing_fields_ratio.labels(table=table)._value.get()


def start_metrics_server(port: int = None, addr: str = "0.0.0.0"):
    """Start the HTTP metrics endpoint for Prometheus scraping.

    Port resolution order: explicit arg > env CRAWLER_METRICS_PORT > 9091.
    Returns the (server, thread) from prometheus_client.start_http_server.
    """
    if port is None:
        port = int(os.environ.get("CRAWLER_METRICS_PORT", "9091"))
    return start_http_server(port, addr=addr)
