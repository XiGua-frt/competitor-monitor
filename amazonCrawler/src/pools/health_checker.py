"""Proxy health checker: 主动探活 + 结果上报（TK health_checker 裁剪版）。

- ProxyHealthChecker(pool, checker_fn=None, interval=600, clock=time.monotonic)
- checker_fn(proxy_url) -> bool 可注入（默认实现经代理请求 Amazon 移动轻页，
  超时 5s；真实网络路径不进单测）
- probe_all()：并发探测（ThreadPoolExecutor max_workers=5），失败 report_failure、
  成功 report_success（结果同时写入 pool.stats()）
- start()/stop()：后台 daemon 线程周期探测；单测不 start，直接调 probe_all
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, List, Optional

try:  # pragma: no cover - 取决于 Agent A 是否已就绪
    from src.common.logger import get_logger
except ImportError:
    from ..anti_detection._common_fallback import get_logger

logger = get_logger(__name__)

# 探活 URL：Amazon 移动轻页（响应小、无重 JS，适合高频探测）
DEFAULT_PROBE_URL = 'https://www.amazon.com/gp/aw/y'
DEFAULT_PROBE_TIMEOUT = 5.0
DEFAULT_INTERVAL = 600
MAX_WORKERS = 5


def default_checker(proxy_url: str, probe_url: str = DEFAULT_PROBE_URL,
                    timeout: float = DEFAULT_PROBE_TIMEOUT) -> bool:
    """默认探活实现：经代理请求轻页，200 视为健康（真实网络路径）。"""
    from curl_cffi import requests
    try:
        resp = requests.get(
            probe_url,
            proxies={'http': proxy_url, 'https': proxy_url},
            timeout=timeout,
            impersonate='chrome131',
        )
        return resp.status_code == 200
    except Exception as exc:
        logger.debug(f"Probe failed via {proxy_url.split('@')[-1]}: {exc}")
        return False


class ProxyHealthChecker:
    """代理池主动探活器：周期并发探测 + 成败上报进池统计。"""

    def __init__(self, pool, checker_fn: Optional[Callable[[str], bool]] = None,
                 interval: float = DEFAULT_INTERVAL,
                 clock: Callable[[], float] = time.monotonic,
                 max_workers: int = MAX_WORKERS):
        """Initialize health checker.

        Args:
            pool: IPPool 实例（report_failure/report_success/stats 的宿主）
            checker_fn: 探活函数 proxy_url -> bool；None 用 default_checker
            interval: 后台探测周期秒数
            clock: 时间函数（测试注入假时钟）
            max_workers: 并发探测线程数
        """
        self.pool = pool
        self.checker_fn = checker_fn or default_checker
        self.interval = float(interval)
        self._clock = clock
        self.max_workers = max(1, int(max_workers))
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------ 探测

    def probe_all(self) -> dict:
        """并发探测池内全部代理并上报成败。

        Returns:
            {'total': 探测数, 'ok': 成功数, 'failed': 失败数}
        """
        proxies: List[str] = list(getattr(self.pool, '_proxies', []))
        if not proxies:
            return {'total': 0, 'ok': 0, 'failed': 0}

        ok = failed = 0
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            results = executor.map(self._probe_one, proxies)
            for healthy in results:
                if healthy:
                    ok += 1
                else:
                    failed += 1

        summary = {'total': len(proxies), 'ok': ok, 'failed': failed}
        logger.info(
            f"Health probe done: {ok}/{len(proxies)} healthy, {failed} failed")
        return summary

    def _probe_one(self, proxy_url: str) -> bool:
        """单代理探测 + 上报；任何异常都按失败处理（不向上传播）。"""
        try:
            healthy = bool(self.checker_fn(proxy_url))
        except Exception as exc:
            logger.warning(
                f"Checker raised for {proxy_url.split('@')[-1]}: {exc}")
            healthy = False

        if healthy:
            self.pool.report_success(proxy_url)
        else:
            self.pool.report_failure(proxy_url)
        return healthy

    # ------------------------------------------------------------------ 后台线程

    def start(self) -> None:
        """启动后台 daemon 探测线程（已启动则幂等）。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name='proxy-health-checker', daemon=True)
        self._thread.start()
        logger.info(f"Health checker started (interval={self.interval:.0f}s)")

    def stop(self, timeout: float = 2.0) -> None:
        """停止后台线程（等待至多 timeout 秒）。"""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        logger.info("Health checker stopped")

    def _run_loop(self) -> None:
        """周期探测循环：probe_all 一次 → 等待 interval（可被 stop 打断）。"""
        while not self._stop_event.is_set():
            try:
                self.probe_all()
            except Exception as exc:  # pragma: no cover - 防御性兜底
                logger.warning(f"probe_all error: {exc}")
            self._stop_event.wait(self.interval)
