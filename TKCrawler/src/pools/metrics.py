"""Prometheus metrics exporter for pool management system.

This module provides comprehensive monitoring for CookiePoolManager, IPPoolManager,
and the overall scraping infrastructure. It exports metrics compatible with the
Grafana dashboard at docs/monitoring/pool_dashboard.json.

Metrics Categories:
    1. Pool Status Gauges: Account/proxy counts by status
    2. Request Counters: Total requests by scraper and status
    3. Latency Histograms: Request duration distributions
    4. WAF/Health: Block counts and health scores

Usage:
    from src.pools.metrics import PoolMetricsExporter
    
    exporter = PoolMetricsExporter(cookie_pool, ip_pool)
    exporter.start_server(port=9090)
    
    # Update metrics periodically
    exporter.update_pool_metrics()
    
    # Record individual requests
    exporter.record_request(
        scraper='shop',
        duration=2.5,
        success=True
    )
"""

import os
import time
from typing import Optional, Dict
from contextlib import contextmanager
from threading import Thread, Event

from prometheus_client import (
    REGISTRY,
    Counter,
    Gauge,
    Histogram,
    start_http_server,
)

from ..common.logger import get_logger

logger = get_logger(__name__)


# ==============================================================================
# Metric Definitions
# ==============================================================================

# Pool Status Gauges
pool_accounts_total = Gauge(
    'pool_accounts_total',
    'Total number of accounts in pool'
)

pool_accounts_active = Gauge(
    'pool_accounts_active',
    'Number of active accounts'
)

pool_accounts_cooling = Gauge(
    'pool_accounts_cooling',
    'Number of accounts in cooling state'
)

pool_accounts_suspended = Gauge(
    'pool_accounts_suspended',
    'Number of suspended accounts'
)

pool_accounts_failed = Gauge(
    'pool_accounts_failed',
    'Number of failed accounts'
)

pool_proxies_total = Gauge(
    'pool_proxies_total',
    'Total number of proxies in pool'
)

pool_proxies_healthy = Gauge(
    'pool_proxies_healthy',
    'Number of healthy proxies'
)

pool_proxies_failed = Gauge(
    'pool_proxies_failed',
    'Number of failed proxies'
)

# Account Health Metrics
account_health_score = Gauge(
    'pool_account_health_score',
    'Average health score of accounts',
    ['status']
)

account_quota_usage = Gauge(
    'pool_account_quota_usage',
    'Average daily quota usage ratio (0.0-1.0)'
)

# Proxy Performance Metrics
proxy_avg_latency = Gauge(
    'pool_proxy_avg_latency_ms',
    'Average proxy latency in milliseconds',
    ['region']
)

proxy_success_rate = Gauge(
    'pool_proxy_success_rate',
    'Proxy success rate (0.0-1.0)',
    ['region']
)

# Scraping Request Counters
scraping_requests_total = Counter(
    'pool_scraping_requests_total',
    'Total scraping requests',
    ['scraper', 'status']
)

scraping_products_total = Counter(
    'pool_scraping_products_total',
    'Total products scraped successfully'
)

scraping_videos_total = Counter(
    'pool_scraping_videos_total',
    'Total videos scraped successfully'
)

scraping_creators_total = Counter(
    'pool_scraping_creators_total',
    'Total creators scraped successfully'
)

# WAF and Rate Limiting
scraping_waf_blocks_total = Counter(
    'pool_scraping_waf_blocks_total',
    'Total WAF blocks',
    ['scraper']
)

scraping_rate_limits_total = Counter(
    'pool_scraping_rate_limits_total',
    'Total rate limit hits',
    ['scraper']
)

# Request Duration Histogram
request_duration_seconds = Histogram(
    'pool_request_duration_seconds',
    'Request duration in seconds',
    ['scraper'],
    buckets=(0.1, 0.5, 1, 2, 5, 8, 15, 30, 60, float('inf'))
)

# Task Scheduling Metrics
task_assignments_total = Counter(
    'pool_task_assignments_total',
    'Total task assignments',
    ['task_type', 'strategy']
)

task_assignment_failures_total = Counter(
    'pool_task_assignment_failures_total',
    'Task assignment failures',
    ['reason']
)


# ==============================================================================
# Metrics Exporter
# ==============================================================================

class PoolMetricsExporter:
    """Prometheus metrics exporter for pool management.
    
    This class aggregates metrics from CookiePoolManager and IPPoolManager,
    provides convenience methods for recording scraping events, and runs
    an HTTP server to expose /metrics endpoint for Prometheus scraping.
    """
    
    def __init__(
        self,
        cookie_pool=None,
        ip_pool=None,
        update_interval: int = 15
    ):
        """Initialize metrics exporter.
        
        Args:
            cookie_pool: CookiePoolManager instance (optional)
            ip_pool: IPPoolManager instance (optional)
            update_interval: Seconds between automatic metric updates
        """
        self.cookie_pool = cookie_pool
        self.ip_pool = ip_pool
        self.update_interval = update_interval
        
        self._update_thread: Optional[Thread] = None
        self._stop_event = Event()
        
        logger.info("PoolMetricsExporter initialized")
    
    def set_cookie_pool(self, cookie_pool):
        """Set cookie pool manager instance."""
        self.cookie_pool = cookie_pool
        logger.info("Cookie pool attached to metrics exporter")
    
    def set_ip_pool(self, ip_pool):
        """Set IP pool manager instance."""
        self.ip_pool = ip_pool
        logger.info("IP pool attached to metrics exporter")
    
    def update_pool_metrics(self):
        """Update all pool status metrics from current pool state.
        
        This method should be called periodically (e.g., every 15 seconds)
        to keep Prometheus metrics in sync with actual pool state.
        """
        if self.cookie_pool:
            self._update_account_metrics()
        
        if self.ip_pool:
            self._update_proxy_metrics()
    
    def _update_account_metrics(self):
        """Update account-related metrics."""
        accounts = self.cookie_pool.accounts.values()
        
        # Count by status
        status_counts = {
            'active': 0,
            'cooling': 0,
            'suspended': 0,
            'failed': 0
        }
        
        health_scores_by_status = {status: [] for status in status_counts}
        total_quota_usage = 0
        
        for account in accounts:
            status = account.status
            if status in status_counts:
                status_counts[status] += 1
            
            # Collect health scores
            health_scores_by_status[status].append(account.health_score)
            
            # Calculate quota usage
            if account.daily_quota > 0:
                usage = account.daily_request_count / account.daily_quota
                total_quota_usage += usage
        
        # Update gauges
        pool_accounts_total.set(len(accounts))
        pool_accounts_active.set(status_counts['active'])
        pool_accounts_cooling.set(status_counts['cooling'])
        pool_accounts_suspended.set(status_counts['suspended'])
        pool_accounts_failed.set(status_counts['failed'])
        
        # Average health scores by status
        for status, scores in health_scores_by_status.items():
            if scores:
                avg_score = sum(scores) / len(scores)
                account_health_score.labels(status=status).set(avg_score)
        
        # Average quota usage
        if accounts:
            account_quota_usage.set(total_quota_usage / len(accounts))
    
    def _update_proxy_metrics(self):
        """Update proxy-related metrics."""
        proxies = self.ip_pool.proxies.values()
        
        # Count by status
        healthy_count = sum(1 for p in proxies if p.status == 'active')
        failed_count = sum(1 for p in proxies if p.status == 'failed')
        
        pool_proxies_total.set(len(proxies))
        pool_proxies_healthy.set(healthy_count)
        pool_proxies_failed.set(failed_count)
        
        # Group by region
        latency_by_region: Dict[str, list] = {}
        success_rate_by_region: Dict[str, list] = {}
        
        for proxy in proxies:
            region = proxy.region
            
            if region not in latency_by_region:
                latency_by_region[region] = []
                success_rate_by_region[region] = []
            
            latency_by_region[region].append(proxy.avg_latency)
            success_rate_by_region[region].append(proxy.success_rate)
        
        # Update region metrics
        for region, latencies in latency_by_region.items():
            if latencies:
                avg_latency = sum(latencies) / len(latencies)
                proxy_avg_latency.labels(region=region).set(avg_latency)
        
        for region, rates in success_rate_by_region.items():
            if rates:
                avg_rate = sum(rates) / len(rates)
                proxy_success_rate.labels(region=region).set(avg_rate)
    
    def record_request(
        self,
        scraper: str,
        duration: float,
        success: bool,
        waf_blocked: bool = False,
        rate_limited: bool = False
    ):
        """Record a scraping request with its outcome.
        
        Args:
            scraper: Scraper name ('shop', 'video', 'creator')
            duration: Request duration in seconds
            success: Whether request succeeded
            waf_blocked: Whether request was blocked by WAF
            rate_limited: Whether request hit rate limit
        """
        # Determine status
        if waf_blocked:
            status = 'waf_blocked'
            scraping_waf_blocks_total.labels(scraper=scraper).inc()
        elif rate_limited:
            status = 'rate_limited'
            scraping_rate_limits_total.labels(scraper=scraper).inc()
        elif success:
            status = 'success'
        else:
            status = 'failed'
        
        # Update counters
        scraping_requests_total.labels(
            scraper=scraper,
            status=status
        ).inc()
        
        # Update histogram
        request_duration_seconds.labels(scraper=scraper).observe(duration)
        
        # Update type-specific counters on success
        if success:
            if scraper == 'shop':
                scraping_products_total.inc()
            elif scraper == 'video':
                scraping_videos_total.inc()
            elif scraper == 'creator':
                scraping_creators_total.inc()
    
    @contextmanager
    def track_request(
        self,
        scraper: str,
        auto_detect_waf: bool = True
    ):
        """Context manager to automatically track request duration.
        
        Args:
            scraper: Scraper name
            auto_detect_waf: Automatically detect WAF exceptions
        
        Usage:
            with exporter.track_request('shop'):
                response = scrape_product(...)
        """
        start = time.monotonic()
        waf_blocked = False
        rate_limited = False
        success = False
        
        try:
            yield
            success = True
        except Exception as e:
            # Auto-detect WAF blocks
            if auto_detect_waf:
                error_msg = str(e).lower()
                if 'captcha' in error_msg or 'verify' in error_msg:
                    waf_blocked = True
                elif 'rate limit' in error_msg or '429' in error_msg:
                    rate_limited = True
            raise
        finally:
            duration = time.monotonic() - start
            self.record_request(
                scraper=scraper,
                duration=duration,
                success=success,
                waf_blocked=waf_blocked,
                rate_limited=rate_limited
            )
    
    def record_task_assignment(
        self,
        task_type: str,
        strategy: str,
        success: bool,
        failure_reason: Optional[str] = None
    ):
        """Record a task assignment event.
        
        Args:
            task_type: Type of task ('scrape_product', 'scrape_video', etc.)
            strategy: Scheduling strategy used ('round_robin', 'weighted', etc.)
            success: Whether assignment succeeded
            failure_reason: Reason for failure (if applicable)
        """
        if success:
            task_assignments_total.labels(
                task_type=task_type,
                strategy=strategy
            ).inc()
        else:
            task_assignment_failures_total.labels(
                reason=failure_reason or 'unknown'
            ).inc()
    
    def start_auto_update(self):
        """Start background thread for automatic metric updates."""
        if self._update_thread and self._update_thread.is_alive():
            logger.warning("Auto-update thread already running")
            return
        
        self._stop_event.clear()
        self._update_thread = Thread(
            target=self._auto_update_loop,
            daemon=True
        )
        self._update_thread.start()
        logger.info(
            f"Started auto-update thread (interval={self.update_interval}s)"
        )
    
    def stop_auto_update(self):
        """Stop background update thread."""
        if not self._update_thread:
            return
        
        self._stop_event.set()
        self._update_thread.join(timeout=5)
        logger.info("Stopped auto-update thread")
    
    def _auto_update_loop(self):
        """Background loop for updating metrics."""
        while not self._stop_event.is_set():
            try:
                self.update_pool_metrics()
            except Exception as e:
                logger.error(f"Error updating metrics: {e}", exc_info=True)
            
            # Sleep with interrupt check
            self._stop_event.wait(self.update_interval)
    
    def start_server(
        self,
        port: int = None,
        addr: str = '0.0.0.0',
        auto_update: bool = True
    ):
        """Start HTTP server to expose /metrics endpoint.
        
        Args:
            port: HTTP port (default: 9090 or POOL_METRICS_PORT env var)
            addr: Bind address
            auto_update: Start automatic metric update thread
        
        Returns:
            Tuple of (server, thread) from prometheus_client
        """
        if port is None:
            port = int(os.environ.get('POOL_METRICS_PORT', '9090'))
        
        # Start auto-update if enabled
        if auto_update:
            self.start_auto_update()
        
        # Initial metrics update
        self.update_pool_metrics()
        
        logger.info(f"Starting metrics server on {addr}:{port}")
        return start_http_server(port, addr=addr, registry=REGISTRY)


# ==============================================================================
# Module-level Convenience Functions
# ==============================================================================

# Global exporter instance (lazily initialized)
_global_exporter: Optional[PoolMetricsExporter] = None


def get_exporter(
    cookie_pool=None,
    ip_pool=None,
    update_interval: int = 15
) -> PoolMetricsExporter:
    """Get or create global metrics exporter instance.
    
    Args:
        cookie_pool: CookiePoolManager instance
        ip_pool: IPPoolManager instance
        update_interval: Seconds between updates
    
    Returns:
        Global PoolMetricsExporter instance
    """
    global _global_exporter
    
    if _global_exporter is None:
        _global_exporter = PoolMetricsExporter(
            cookie_pool=cookie_pool,
            ip_pool=ip_pool,
            update_interval=update_interval
        )
    else:
        # Update pools if provided
        if cookie_pool:
            _global_exporter.set_cookie_pool(cookie_pool)
        if ip_pool:
            _global_exporter.set_ip_pool(ip_pool)
    
    return _global_exporter


def start_metrics_server(
    port: int = None,
    addr: str = '0.0.0.0',
    cookie_pool=None,
    ip_pool=None
):
    """Convenience function to start metrics server with global exporter.
    
    Args:
        port: HTTP port (default: 9090)
        addr: Bind address
        cookie_pool: CookiePoolManager instance
        ip_pool: IPPoolManager instance
    """
    exporter = get_exporter(cookie_pool=cookie_pool, ip_pool=ip_pool)
    return exporter.start_server(port=port, addr=addr)
