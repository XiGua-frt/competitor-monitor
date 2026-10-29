"""BaseScraper — Amazon 采集基类（参考 TKCrawler 骨架）。

提供：
- client 注入（默认 CurlCffiClient，测试注入 mock：request(url, method, headers, **kw)）
- RateLimiter 随机区间限速（anti_detection 未交付时降级为 no-op sleep）
- 日配额检查（Redis 配额不可用时降级本地计数）
- 503 / BotBlocked 软冷却：冷却期内直接抛 BotBlockedException，不发请求
- Prometheus 埋点 try/except 降级 no-op
- fetch(url, headers) 统一入口：状态码 503 或正文启发式命中 → BotBlockedException

src/common 与 src/anti_detection 由其他 Agent 实现，本模块全部 try/except
ImportError fallback，保证 scrapers 层可独立单测（无网络、无 Redis）。
"""

import os
import re
import time
from typing import Optional

try:
    from src.common.logger import get_logger
    logger = get_logger(__name__)
except ImportError:  # pragma: no cover
    logger = logging.getLogger(__name__) if False else None
if logger is None:
    import logging
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

try:
    from src.common.exceptions import (
        BotBlockedException,
        QuotaExceededException,
        RateLimitException,
    )
except ImportError:  # pragma: no cover - Agent A 未交付时的 fallback
    class BotBlockedException(Exception):
        """Amazon 503 / captcha 页。"""

    class RateLimitException(Exception):
        """限流 / 冷却期拒绝。"""

    class QuotaExceededException(Exception):
        def __init__(self, resource_type: str = '', limit: int = 0):
            self.resource_type = resource_type
            self.limit = limit
            super().__init__(
                f"Daily quota exceeded for {resource_type} (limit: {limit})")

try:
    from src.common.config import config as _app_config
except ImportError:  # pragma: no cover
    _app_config = None

# anti_detection 由 Agent B 实现；未交付时限速降级为固定 sleep
try:
    from src.anti_detection import RateLimiter as _RateLimiter
except ImportError:  # pragma: no cover
    _RateLimiter = None

try:  # Prometheus 埋点：metrics/monitoring 由 Agent D 实现，缺失即 no-op
    from src.monitoring import inc_bot_block, observe_request
except Exception:  # pragma: no cover
    observe_request = None
    inc_bot_block = None


def _record_metric(fn, *args):
    """零开销埋点降级：任何失败只记 debug，不影响主流程。"""
    if fn is None:
        return
    try:
        fn(*args)
    except Exception:
        logger.debug("metric recording failed", exc_info=True)


# 风控参数：环境变量优先，其次 common.config，最后保守默认
def _env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is not None:
        try:
            return float(v)
        except ValueError:
            pass
    if _app_config is not None:
        v = getattr(_app_config, name, None)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return default


# 正文启发式（与契约 anti_detection.looks_like_bot_challenge 四强特征对齐）：
# 仅当命中强特征才判 bot，正常页正文含 "captcha" 资源名不误报。
_BOT_TITLE_RE = re.compile(
    r'<title[^>]*>\s*(?:Sorry! Something went wrong|'
    r'Robot Check|Access Denied|.*(captcha).*)</title>', re.IGNORECASE)
_BOT_MARKERS = (
    'api-services-support@amazon.com',
    'captcha-image',
    'aws-waf-token',
)


class _FallbackRateLimiter:
    """anti_detection.RateLimiter 未交付时的最小限速实现（随机区间 sleep）。"""

    def __init__(self, min_interval: int, max_interval: int):
        import random
        self._random = random
        self.min_interval = min_interval
        self.max_interval = max_interval
        self._last_request = 0.0

    def wait(self):
        interval = self._random.uniform(self.min_interval, self.max_interval)
        elapsed = time.time() - self._last_request
        if elapsed < interval:
            time.sleep(interval - elapsed)
        self._last_request = time.time()

    def check_quota(self, resource_type: str, limit: int) -> bool:
        # 本地进程内计数降级（无 Redis）
        used = getattr(self, '_quota_used', None)
        if used is None:
            self._quota_used = {}
        self._quota_used[resource_type] = \
            self._quota_used.get(resource_type, 0) + 1
        if self._quota_used[resource_type] > limit:
            return False
        return True


class BaseScraper:
    """所有 Amazon scraper 的基类：限速 + 配额 + 软冷却 + 统一 fetch。

    池接线（use_pool）：
    - use_pool=None 时读环境变量 AMZ_POOL_ENABLED（默认 False）——默认关闭，
      现有路径行为完全不变。
    - use_pool=True 时 client property 从 pools.integration.pool_scoped_client
      获取池组装的 client（代理 + 指纹注入），并把 PoolScopedContext 存到
      self._pool_ctx；fetch 前后通过 _report_pool_outcome 钩子上报成败
      （503/BotBlocked → report_failure，成功 → report_success）。
    - 取舍说明：BaseScraper 是长生命周期对象，而 pool_scoped_client 是任务级
      上下文管理器。这里不做 with 嵌入（会把任务租约挂到 scraper 生命周期上），
      而是采用"client 工厂 + 显式 report_outcome 钩子"方案：每次 fetch 前
      ensure_pool_client() 幂等获取池 client，fetch 结束立即上报。租约获取
      （acquire_lease）留在 integration.pool_scoped_client，由调用方
      （main/scheduler 层）包任务级 with——scraper 层只负责 client 装配与
      成败上报，测试可注入 fake 池完全离线验证。
    """

    def __init__(self, client=None, min_interval: float = None,
                 max_interval: float = None, cooldown_seconds: float = None,
                 use_pool: bool = None):
        """
        Args:
            client: 暴露 request(url, method='GET', headers=None, **kw) 的
                    HTTP 客户端；默认延迟构造 CurlCffiClient（import 失败时报错）。
                    测试注入 mock。
            min_interval/max_interval: 请求随机间隔秒数覆盖。
            cooldown_seconds: 503 后软冷却时长覆盖（默认 3600s）。
            use_pool: 是否走池接线（代理+指纹注入 + 成败上报）。None 读
                AMZ_POOL_ENABLED；False（默认）时行为与无池路径完全一致。
        """
        self._client = client
        self.use_pool = (self._resolve_use_pool_default()
                         if use_pool is None else bool(use_pool))
        # 池上下文（use_pool=True 时由 ensure_pool_client 填充）
        self._pool_ctx = None
        self.min_interval = int(min_interval if min_interval is not None
                                else _env_float('REQUEST_MIN_INTERVAL', 8))
        self.max_interval = int(max_interval if max_interval is not None
                                else _env_float('REQUEST_MAX_INTERVAL', 15))
        if _RateLimiter is not None:
            self.limiter = _RateLimiter(min_interval=self.min_interval,
                                        max_interval=self.max_interval)
        else:  # pragma: no cover - Agent B 未交付时的降级
            self.limiter = _FallbackRateLimiter(self.min_interval,
                                                self.max_interval)
        self.cooldown_seconds = float(
            cooldown_seconds if cooldown_seconds is not None
            else _env_float('SOFT_COOLDOWN_SECONDS', 3600))
        # 软冷却状态：冷却截止的 wall-clock 时间戳
        self._cooldown_until: float = 0.0

    # ------------------------------------------------------------------
    # client 延迟构造（默认 CurlCffiClient；use_pool=True 时走池装配）
    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_use_pool_default() -> bool:
        """AMZ_POOL_ENABLED 读取（仅 use_pool=None 时调用）。"""
        raw = os.getenv('AMZ_POOL_ENABLED', '').strip().lower()
        return raw in ('1', 'true', 'yes', 'on')

    @property
    def client(self):
        if self._client is None:
            try:
                from src.anti_detection import CurlCffiClient
                self._client = CurlCffiClient()
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError(
                    'No HTTP client injected and src.anti_detection.'
                    'CurlCffiClient unavailable') from exc
        return self._client

    # ------------------------------------------------------------------
    # 池接线（use_pool=True 路径）
    # ------------------------------------------------------------------
    def ensure_pool_client(self, task_type: str = 'fetch'):
        """幂等获取池组装的 client 并保存 PoolScopedContext。

        use_pool=False 时直接返回 None（完全不触碰池模块）。
        显式注入的 self._client 优先于池 client（integration 侧不覆盖），
        池上下文仅用于租约与成败上报。池模块不可用时记 WARNING 并回退
        裸 client（池是增强项，不阻塞采集）。
        """
        if not self.use_pool:
            return None
        if self._pool_ctx is not None:
            return self._pool_ctx[1]
        try:
            from src.pools.integration import pool_scoped_client
            # 手动驱动上下文：把 with 生命周期挂到 scraper 上（enter 一次，
            # 退出由 close_pool_client 或进程结束完成）
            cm = pool_scoped_client(task_type, scraper=self)
            ctx = cm.__enter__()
            self._pool_ctx = (cm, ctx)
            return ctx
        except Exception as exc:
            logger.warning(
                'Pool client unavailable (%s: %s), fallback to bare client',
                type(exc).__name__, exc)
            self._pool_ctx = None
            return None

    def close_pool_client(self) -> None:
        """退出池上下文（release lease + 清理 session 级资源）。"""
        if self._pool_ctx is None:
            return
        cm, _ctx = self._pool_ctx
        self._pool_ctx = None
        try:
            cm.__exit__(None, None, None)
        except Exception as exc:
            logger.warning('pool context exit failed: %s', exc)

    def _report_pool_outcome(self, ok: bool = False,
                             blocked: bool = False,
                             response_time: float = None) -> None:
        """fetch 前后钩子：把请求成败上报进池（use_pool 且有池上下文时）。"""
        if not self.use_pool or self._pool_ctx is None:
            return
        try:
            from src.pools.integration import report_outcome
            report_outcome(self._pool_ctx[1], ok=ok, blocked=blocked,
                           response_time=response_time)
        except Exception as exc:
            logger.debug('pool outcome report failed: %s', exc)

    # ------------------------------------------------------------------
    # 软冷却（503 / BotBlocked）
    # ------------------------------------------------------------------
    @property
    def in_cooldown(self) -> bool:
        return time.time() < self._cooldown_until

    def get_cooldown_remaining(self) -> float:
        return max(0.0, self._cooldown_until - time.time())

    def enter_cooldown(self, reason: str = '') -> None:
        self._cooldown_until = time.time() + self.cooldown_seconds
        logger.warning(
            'Entering soft cooldown for %.0fs (until %s). Reason: %s',
            self.cooldown_seconds,
            time.strftime('%H:%M:%S', time.localtime(self._cooldown_until)),
            reason)

    def reset_cooldown(self) -> None:
        self._cooldown_until = 0.0
        logger.info('Soft cooldown manually reset')

    def _ensure_not_cooling_down(self) -> None:
        """冷却期内直接拒绝，不发请求。"""
        if self.in_cooldown:
            raise BotBlockedException(
                f'Soft cooldown active for another '
                f'{self.get_cooldown_remaining():.0f}s '
                f'(triggered by previous 503/bot block)')

    # ------------------------------------------------------------------
    # 配额
    # ------------------------------------------------------------------
    def check_quota(self, resource_type: str, limit: int = None) -> bool:
        """消耗一个日配额单位；超限抛 QuotaExceededException。"""
        if limit is None:
            env_key = {'asin': 'DAILY_QUOTA_ASINS',
                       'search': 'DAILY_QUOTA_SEARCH'}.get(resource_type)
            limit = int(_env_float(env_key, 200)) if env_key else 200
        ok = self.limiter.check_quota(resource_type, limit)
        if not ok:
            raise QuotaExceededException(resource_type, limit)
        return True

    # ------------------------------------------------------------------
    # 统一请求入口
    # ------------------------------------------------------------------
    def fetch(self, url: str, method: str = 'GET',
              headers: Optional[dict] = None, **kwargs):
        """限速请求 + 503/bot 判定 + 软冷却。

        Pipeline:
        1. 冷却期内 → 直接抛 BotBlockedException（不发请求、不计指标）
        2. limiter.wait() 随机区间限速
        3. client.request(...)
        4. 状态码 503 或正文启发式命中 → enter_cooldown + 抛 BotBlockedException
        5. 429 → enter_cooldown + 抛 RateLimitException

        Returns: 原始 client 响应。
        """
        self._ensure_not_cooling_down()
        self.limiter.wait()
        # 池接线：use_pool=True 时确保池 client 就位（幂等；失败回退裸 client）
        if self.use_pool:
            self.ensure_pool_client()

        start = time.monotonic()
        try:
            response = self.client.request(url, method=method,
                                           headers=headers, **kwargs)
        except RateLimitException as exc:
            _record_metric(observe_request, self._scraper_name(),
                           'rate_limited', time.monotonic() - start)
            self._report_pool_outcome(blocked=True)
            self.enter_cooldown(f'{type(exc).__name__} url={url}')
            raise
        except BotBlockedException as exc:
            _record_metric(observe_request, self._scraper_name(), 'bot_block',
                           time.monotonic() - start)
            _record_metric(inc_bot_block, self._scraper_name())
            self._report_pool_outcome(blocked=True)
            self.enter_cooldown(f'{type(exc).__name__} url={url}')
            raise
        except Exception:
            _record_metric(observe_request, self._scraper_name(), 'error',
                           time.monotonic() - start)
            self._report_pool_outcome(blocked=True)
            raise

        status = getattr(response, 'status_code', None)
        if status == 429:
            _record_metric(observe_request, self._scraper_name(),
                           'rate_limited', time.monotonic() - start)
            self._report_pool_outcome(blocked=True)
            self.enter_cooldown(f'HTTP 429 url={url}')
            raise RateLimitException(f'HTTP 429 at {url}')

        if status == 503 or self._looks_like_bot_challenge(response):
            _record_metric(observe_request, self._scraper_name(), 'bot_block',
                           time.monotonic() - start)
            _record_metric(inc_bot_block, self._scraper_name())
            self._report_pool_outcome(blocked=True)
            self.enter_cooldown(f'HTTP {status} bot-challenge url={url}')
            raise BotBlockedException(
                f'Bot challenge detected at {url} (status={status})')

        _record_metric(observe_request, self._scraper_name(), 'success',
                       time.monotonic() - start)
        self._report_pool_outcome(
            ok=True, response_time=time.monotonic() - start)
        return response

    # ------------------------------------------------------------------
    def _looks_like_bot_challenge(self, response) -> bool:
        """正文启发式：强特征命中才判 bot（避免 "captcha" 资源名误报）。"""
        text = getattr(response, 'text', None)
        if not text:
            return False
        # 只检查前 64KB，bot 页特征都在头部
        head = text[:65536]
        if _BOT_TITLE_RE.search(head):
            return True
        return any(marker in head for marker in _BOT_MARKERS)

    def _scraper_name(self) -> str:
        return type(self).__name__.lower()
