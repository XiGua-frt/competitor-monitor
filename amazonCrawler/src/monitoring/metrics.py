"""Prometheus 埋点层 — scraper 请求 / 耗时 / bot 拦截 / 数据质量指标。

指标清单（与 CONTRACT_V2 对齐）:
    scraper_requests_total{scraper, status}       Counter  - 请求总数（按爬虫与结果）
    scraper_request_duration_seconds{scraper}     Histogram- 请求耗时分布
    amazon_bot_blocks_total{scraper}              Counter  - Amazon 503/captcha 拦截次数
    data_quality_missing_ratio{table}             Gauge    - 表级关键字段缺失率 (0.0-1.0)

采集层用法:
    from src.monitoring import observe_request, inc_bot_block, set_missing_ratio

    start = time.monotonic()
    try:
        ...请求...
        observe_request("pdp", "success", time.monotonic() - start)
    except BotBlockedException:
        inc_bot_block("pdp")
        observe_request("pdp", "bot_block", time.monotonic() - start)

降级策略:
    prometheus_client 未安装时全部退化为 no-op（接口不变、不抛错），
    注册幂等：重复 import / 重复 register_metrics() 不产生重复时间序列。
"""

import os

try:
    from src.common.logger import get_logger
    logger = get_logger(__name__)
except ImportError:  # pragma: no cover - 最早期的降级
    import logging
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

try:
    from prometheus_client import (
        REGISTRY,
        Counter,
        Gauge,
        Histogram,
    )
    _HAS_PROMETHEUS = True
except ImportError:  # pragma: no cover - 无 prometheus_client 环境
    _HAS_PROMETHEUS = False

# ---------------------------------------------------------------------------
# 指标名（监控/告警按这些名字抓取，勿改）
# ---------------------------------------------------------------------------

REQUESTS_TOTAL = "scraper_requests_total"
DURATION_SECONDS = "scraper_request_duration_seconds"
BOT_BLOCKS_TOTAL = "amazon_bot_blocks_total"
MISSING_RATIO = "data_quality_missing_ratio"

REQUESTS_HELP = "Total number of scraper requests by scraper and result status."
DURATION_HELP = "Duration of scraper requests in seconds."
BOT_BLOCKS_HELP = "Total number of requests blocked by Amazon anti-bot."
MISSING_HELP = "Ratio of rows with missing critical fields per table (0.0-1.0)."

# 请求耗时桶：Amazon 限速下正常请求在秒级（8-15s 间隔），上界放宽到 120s
DURATION_BUCKETS = (0.1, 0.5, 1, 2, 5, 8, 15, 30, 60, 120, float("inf"))


# ---------------------------------------------------------------------------
# no-op 降级对象：接口与 prometheus_client 的 labels() 子对象一致
# ---------------------------------------------------------------------------

class _NoopChild:
    """单标签组合下的 no-op 计数/观测对象。"""

    def inc(self, *args, **kwargs):
        pass

    def observe(self, *args, **kwargs):
        pass

    def set(self, *args, **kwargs):
        pass


class _NoopMetric:
    """无 prometheus_client 时的整指标 no-op。"""

    def labels(self, **labels):  # noqa: D401 - 与 prom 接口同名
        return _NoopChild()


def _get_or_create(cls, name, doc, labelnames, registry=None, **kwargs):
    """幂等创建 collector：同名已注册则复用（跨模块 reload 安全）。"""
    if not _HAS_PROMETHEUS:
        return _NoopMetric()
    registry = registry or REGISTRY
    existing = getattr(registry, "_names_to_collectors", {}).get(name)
    if existing is not None:
        return existing
    try:
        return cls(name, doc, labelnames, registry=registry, **kwargs)
    except ValueError:  # pragma: no cover - 并发/重复注册竞态
        return registry._names_to_collectors[name]


def register_metrics(registry=None):
    """幂等注册全部爬虫指标，返回 collector dict（无 prom 时返回 no-op 集合）。"""
    requests_total = _get_or_create(
        Counter, REQUESTS_TOTAL, REQUESTS_HELP, ["scraper", "status"], registry
    )
    duration = _get_or_create(
        Histogram, DURATION_SECONDS, DURATION_HELP, ["scraper"], registry,
        buckets=DURATION_BUCKETS,
    )
    bot_blocks = _get_or_create(
        Counter, BOT_BLOCKS_TOTAL, BOT_BLOCKS_HELP, ["scraper"], registry
    )
    missing_ratio = _get_or_create(
        Gauge, MISSING_RATIO, MISSING_HELP, ["table"], registry
    )
    return {
        "requests_total": requests_total,
        "duration_seconds": duration,
        "bot_blocks_total": bot_blocks,
        "missing_ratio": missing_ratio,
    }


# 模块级单例（_get_or_create 保证 re-import 幂等）
_metrics = register_metrics()
scraper_requests_total = _metrics["requests_total"]
scrape_duration_seconds = _metrics["duration_seconds"]
amazon_bot_blocks_total = _metrics["bot_blocks_total"]
data_quality_missing_ratio = _metrics["missing_ratio"]


# ---------------------------------------------------------------------------
# 便捷函数：采集层（BaseScraper）与 service 层调用
# ---------------------------------------------------------------------------

def observe_request(scraper: str, status: str, duration: float) -> None:
    """记录一次请求：总数 +1 并观测耗时。任何失败只降级不抛错。"""
    try:
        scraper_requests_total.labels(scraper=scraper, status=status).inc()
        scrape_duration_seconds.labels(scraper=scraper).observe(float(duration))
    except Exception:  # pragma: no cover - 埋点永不影响主流程
        logger.debug("observe_request failed", exc_info=True)


def inc_bot_block(scraper: str) -> None:
    """bot 拦截计数 +1（503/captcha/Access Denied）。"""
    try:
        amazon_bot_blocks_total.labels(scraper=scraper).inc()
    except Exception:  # pragma: no cover
        logger.debug("inc_bot_block failed", exc_info=True)


def set_missing_ratio(table: str, ratio: float) -> None:
    """设置某表关键字段缺失率（0.0-1.0）。"""
    try:
        data_quality_missing_ratio.labels(table=table).set(float(ratio))
    except Exception:  # pragma: no cover
        logger.debug("set_missing_ratio failed", exc_info=True)


# ---------------------------------------------------------------------------
# 查询辅助（测试与本地调试用；no-op 模式返回 0.0）
# ---------------------------------------------------------------------------

def get_request_count(scraper: str, status: str) -> float:
    if not _HAS_PROMETHEUS:
        return 0.0
    try:
        return scraper_requests_total.labels(
            scraper=scraper, status=status)._value.get()
    except Exception:  # pragma: no cover
        return 0.0


def get_bot_block_count(scraper: str) -> float:
    if not _HAS_PROMETHEUS:
        return 0.0
    try:
        return amazon_bot_blocks_total.labels(scraper=scraper)._value.get()
    except Exception:  # pragma: no cover
        return 0.0


def get_missing_ratio(table: str) -> float:
    if not _HAS_PROMETHEUS:
        return 0.0
    try:
        return data_quality_missing_ratio.labels(table=table)._value.get()
    except Exception:  # pragma: no cover
        return 0.0


def start_metrics_server(port=None, addr: str = "0.0.0.0"):
    """启动 Prometheus 抓取端点。

    端口解析顺序：显式参数 > 环境变量 CRAWLER_METRICS_PORT > 9091。
    无 prometheus_client 时记 warning 并返回 None（不抛错）。
    返回 (server, thread) 或 None。
    """
    if not _HAS_PROMETHEUS:
        logger.warning("prometheus_client 未安装，metrics server 不启动（no-op）")
        return None
    from prometheus_client import start_http_server
    if port is None:
        port = int(os.environ.get("CRAWLER_METRICS_PORT", "9091"))
    return start_http_server(port, addr=addr)
