"""BaseScraper: shared infrastructure for all scrapers.

Provides:
- AntiDetectionClient ownership (default: CurlCffiClient)
- Rate limiting with randomized intervals (reuses anti_detection.rate_limiter)
- Daily quota check (DAILY_QUOTA_PRODUCTS=200 by default)
- 429/403 soft cooldown: on trigger, block requests for 1 hour (state + exception)
- Unified logging

Risk-control parameters are read from environment variables (see
RISK_DEFAULTS below) so nothing is hardcoded in business logic. Since
src/common/config.py is owned by another agent, scraper-specific knobs are
read via os.getenv here with conservative defaults.
"""

import os
import time
from typing import Optional

from ..anti_detection import CurlCffiClient, RateLimiter
from ..common.config import config
from ..common.logger import get_logger
from ..common.exceptions import RateLimitException, WAFBlockedException

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Prometheus 指标埋点（零开销降级：prometheus_client 不可用时为 no-op，
# 记录过程中抛出的任何异常也只会被吞掉并降级为 debug 日志，绝不影响主流程。
# metrics server 是否启动与本埋点无关——未启动时指标照常累加，仅不暴露端点。)
# ---------------------------------------------------------------------------
try:
    from ..monitoring import observe_request, inc_waf_block, set_missing_ratio
except Exception:  # pragma: no cover - prometheus_client 未安装等环境问题
    observe_request = None
    inc_waf_block = None
    set_missing_ratio = None


def _record_metric(fn, *args):
    """Call a metrics helper, swallowing any failure (zero-cost degradation)."""
    if fn is None:
        return
    try:
        fn(*args)
    except Exception:
        logger.debug("metric recording failed", exc_info=True)

# ---------------------------------------------------------------------------
# Risk-control parameters (single source of truth, read from env/config).
# Task card requires 7-10s per product request; config.REQUEST_*_INTERVAL
# defaults to 5-8s, so scraper-specific env vars take precedence.
# ---------------------------------------------------------------------------
RISK_DEFAULTS = {
    # Per-request randomized interval bounds in seconds (product pages: 7-10s)
    'REQUEST_MIN_INTERVAL': float(
        os.getenv('SCRAPER_REQUEST_MIN_INTERVAL', '7')
    ),
    'REQUEST_MAX_INTERVAL': float(
        os.getenv('SCRAPER_REQUEST_MAX_INTERVAL', '10')
    ),
    # Soft cooldown duration after a 429/403, in seconds (default: 1 hour)
    'SOFT_COOLDOWN_SECONDS': float(
        os.getenv('SCRAPER_SOFT_COOLDOWN_SECONDS', '3600')
    ),
}


class BaseScraper:
    """Base class holding the anti-detection client, rate limiter, quota
    tracking and 429/403 soft-cooldown state."""

    def __init__(self, client=None, min_interval: float = None,
                 max_interval: float = None, cooldown_seconds: float = None):
        """Initialize base scraper.

        Args:
            client: HTTP client exposing request(url, method, headers, **kwargs).
                    Defaults to CurlCffiClient. Tests inject a mock.
            min_interval: Override min request interval (seconds).
            max_interval: Override max request interval (seconds).
            cooldown_seconds: Override soft cooldown duration (seconds).
        """
        self.client = client if client is not None else CurlCffiClient()
        self.limiter = RateLimiter(
            min_interval=int(min_interval if min_interval is not None
                             else RISK_DEFAULTS['REQUEST_MIN_INTERVAL']),
            max_interval=int(max_interval if max_interval is not None
                             else RISK_DEFAULTS['REQUEST_MAX_INTERVAL']),
        )
        self.cooldown_seconds = float(
            cooldown_seconds if cooldown_seconds is not None
            else RISK_DEFAULTS['SOFT_COOLDOWN_SECONDS']
        )
        # Soft-cooldown state: requests are refused until this wall-clock time.
        self._cooldown_until: float = 0.0

    # ------------------------------------------------------------------
    # Soft cooldown (429/403)
    # ------------------------------------------------------------------
    @property
    def in_cooldown(self) -> bool:
        """True while the scraper is in soft-cooldown after a 429/403."""
        return time.time() < self._cooldown_until

    def get_cooldown_remaining(self) -> float:
        """Seconds remaining in soft cooldown (0 if not cooling down)."""
        return max(0.0, self._cooldown_until - time.time())

    def enter_cooldown(self, reason: str = "") -> None:
        """Enter soft cooldown for self.cooldown_seconds.

        After a 429/403 we stop issuing requests for 1 hour to let the
        risk-control window reset. Implemented as state (timestamp) plus a
        RateLimitException raised on any premature request.
        """
        self._cooldown_until = time.time() + self.cooldown_seconds
        logger.warning(
            f"Entering soft cooldown for {self.cooldown_seconds:.0f}s "
            f"(until {time.strftime('%H:%M:%S', time.localtime(self._cooldown_until))}). "
            f"Reason: {reason}"
        )

    def reset_cooldown(self) -> None:
        """Manually clear cooldown state (e.g. after operator intervention)."""
        self._cooldown_until = 0.0
        logger.info("Soft cooldown manually reset")

    def _ensure_not_cooling_down(self) -> None:
        """Raise RateLimitException if still in soft cooldown."""
        if self.in_cooldown:
            remaining = self.get_cooldown_remaining()
            raise RateLimitException(
                f"Soft cooldown active for another {remaining:.0f}s "
                f"(triggered by previous 429/403)"
            )

    # ------------------------------------------------------------------
    # Metrics (埋点全部收敛在 BaseScraper 一层，具体 scraper 无需感知)
    # ------------------------------------------------------------------
    def _scraper_name(self) -> str:
        """Metric label value: class name lowercased (e.g. ShopProductScraper
        -> 'shopproductscraper')."""
        return type(self).__name__.lower()

    def _observe_request(self, status: str, start: float) -> None:
        """Record one fetch() outcome (counter + duration histogram)."""
        _record_metric(observe_request, self._scraper_name(), status,
                       time.monotonic() - start)

    def record_data_quality(self, table: str, data) -> float:
        """Compute and report the None-field ratio of ``data``.

        上层（各 scraper 或调度层）在拿到解析结果后可调用本助手：
        data 为非空 dict 时，空字段率 = 值为 None 的字段占比，
        上报到 data_quality_missing_fields_ratio{table} 并返回该比率；
        data 不是非空 dict 时返回 0.0 且不上报（无字段可评估）。
        任何情况下不抛错（零开销降级）。
        """
        if not isinstance(data, dict) or not data:
            return 0.0
        ratio = sum(1 for v in data.values() if v is None) / len(data)
        _record_metric(set_missing_ratio, table, ratio)
        return ratio

    # ------------------------------------------------------------------
    # Quota
    # ------------------------------------------------------------------
    def check_quota(self, resource_type: str, limit: int = None) -> bool:
        """Check and consume one unit of the daily quota.

        Raises QuotaExceededException when the limit is reached
        (delegated to anti_detection.rate_limiter.RateLimiter).
        """
        if limit is None:
            limit = config.DAILY_QUOTA_PRODUCTS
        return self.limiter.check_quota(resource_type, limit)

    def get_quota_status(self, resource_type: str, limit: int = None) -> dict:
        """Return {'used', 'limit', 'remaining'} for a resource type."""
        if limit is None:
            limit = config.DAILY_QUOTA_PRODUCTS
        return self.limiter.get_quota_status(resource_type, limit)

    # ------------------------------------------------------------------
    # Request pipeline: cooldown check -> rate-limit wait -> request
    # ------------------------------------------------------------------
    def fetch(self, url: str, method: str = 'GET',
              headers: Optional[dict] = None, **kwargs):
        """Throttled request through the anti-detection client.

        Pipeline:
        1. refuse while in soft cooldown (RateLimitException)
        2. wait a randomized interval (rate limiter)
        3. issue request via self.client
        4. on 429 (RateLimitException) or 403/418/503 (WAFBlockedException)
           from the client, enter soft cooldown and re-raise

        Metrics (status ∈ success/waf/rate_limited/error):
        - success          -> observe_request(name, 'success', duration)
        - WAFBlockedException -> 'waf' + inc_waf_block(name)
        - RateLimitException  -> 'rate_limited'
        - other exceptions    -> 'error'
        冷却期拒绝发生在真实请求之前，不计入请求指标。
        埋点自身绝不抛错（见 _record_metric）。

        Returns the raw client response.
        """
        self._ensure_not_cooling_down()
        self.limiter.wait()

        start = time.monotonic()
        try:
            response = self.client.request(url, method=method, headers=headers,
                                           **kwargs)
        except WAFBlockedException as exc:
            self._observe_request('waf', start)
            _record_metric(inc_waf_block, self._scraper_name())
            self._enter_cooldown_for(exc, url)
            raise
        except RateLimitException as exc:
            self._observe_request('rate_limited', start)
            self._enter_cooldown_for(exc, url)
            raise
        except Exception:
            self._observe_request('error', start)
            raise
        self._observe_request('success', start)
        return response

    def _enter_cooldown_for(self, exc: Exception, url: str) -> None:
        """Enter soft cooldown after a 429/403-class client exception."""
        status = getattr(exc, 'status_code', None)
        reason = f"{type(exc).__name__} status={status} url={url}"
        self.enter_cooldown(reason)
