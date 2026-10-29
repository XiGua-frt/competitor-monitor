"""Pools integration layer — pool_scoped_client 上下文（对标 TK integration.py）。

职责边界：
- 本模块是池系统与采集链路的唯一接入层；scraper 不直接触碰池内部。
- get_ip_pool() 惰性单例：按 config 构造 ProxySource → IPPool（模块级缓存 +
  reset_for_tests()）。
- pool_scoped_client(task_type, scraper=None)：内部先 acquire_lease（冲突抛
  PoolUnavailableError），再组装 client（IPPool.acquire 代理 + 指纹池指纹）；
  池空且 config.PROXY_URL 配置 → 单代理；都没有 → 裸 client 并 WARNING。
- report_outcome(client, ok, blocked)：blocked → pool.report_failure(ip) +
  fingerprint_pool.report_ban(fp)（+ cookie profile 冷却，若绑定）；ok → 上报成功。
- 上下文退出自动归还（无状态归还，仅清理 session 级资源）。
"""

import os
from contextlib import contextmanager
from typing import Dict, Optional

try:  # pragma: no cover - 取决于 Agent A 是否已就绪
    from src.common.logger import get_logger
    from src.common.config import config as _app_config
except ImportError:  # pragma: no cover
    from ..anti_detection._common_fallback import get_logger
    _app_config = None

from .ip_pool import IPPool
from .fingerprint_pool import FingerprintPool
from .lease import LeaseConflictException, LeaseManager

logger = get_logger(__name__)

__all__ = [
    'PoolUnavailableError',
    'PoolScopedContext',
    'pool_scoped_client',
    'report_outcome',
    'inject_proxy_to_client',
    'get_ip_pool',
    'reset_for_tests',
    'get_fingerprint_pool',
    'get_cookie_pool',
]

LEASE_TTL_SECONDS = 600


class PoolUnavailableError(Exception):
    """池已开启但不可用（lease 冲突 / 空池且无回退代理）——接入层策略性失败信号。"""


# ---------------------------------------------------------------- 单例缓存

_ip_pool: Optional[IPPool] = None
_fingerprint_pool: Optional[FingerprintPool] = None
_cookie_pool = None


def get_ip_pool() -> IPPool:
    """惰性构造 IPPool 单例：ProxySource 列表 → 差集同步进池。"""
    global _ip_pool
    if _ip_pool is None:
        pool = IPPool()
        try:
            from .proxy_source import create_proxy_source, refresh_pool
            source = create_proxy_source(_app_config)
            if source is not None:
                refresh_pool(pool, source)
        except Exception as exc:  # 来源故障不阻塞池创建
            logger.warning(f"Proxy source unavailable, empty pool: {exc}")
        _ip_pool = pool
    return _ip_pool


def get_fingerprint_pool() -> FingerprintPool:
    """指纹池单例（rotate 策略；环境变量 AMZ_FINGERPRINT_STRATEGY 可覆盖）。"""
    global _fingerprint_pool
    if _fingerprint_pool is None:
        strategy = os.getenv('AMZ_FINGERPRINT_STRATEGY', 'rotate')
        _fingerprint_pool = FingerprintPool(strategy=strategy)
    return _fingerprint_pool


def get_cookie_pool():
    """CookiePool 惰性单例：默认单 US profile（AMZ_COOKIE_PROFILES 可扩展）。

    单 profile 语义退化为 SessionManager 等价物；构建失败返回 None
    （cookie 池是增强项，不引入 fail_closed）。
    """
    global _cookie_pool
    if _cookie_pool is None:
        try:
            from .cookie_pool import CookiePool, SessionProfile
            marketplace = getattr(_app_config, 'MARKETPLACE', 'www.amazon.com') \
                if _app_config is not None else os.getenv(
                    'AMZ_MARKETPLACE', 'www.amazon.com')
            profiles = [SessionProfile(marketplace=marketplace)]
            _cookie_pool = CookiePool(profiles)
        except Exception as exc:
            logger.warning(f"Cookie pool unavailable: {exc}")
            _cookie_pool = False  # False = 已尝试且不可用，避免反复重建
    return _cookie_pool or None


def reset_for_tests() -> None:
    """重置全部模块级单例（仅测试用）。"""
    global _ip_pool, _fingerprint_pool, _cookie_pool
    _ip_pool = None
    _fingerprint_pool = None
    _cookie_pool = None


# ---------------------------------------------------------------- 上下文


class PoolScopedContext:
    """pool_scoped_client 的 yield 对象。

    - client: 组装好的 HTTP 客户端（注入代理与指纹）
    - proxy_url: 本次绑定的代理 URL（无代理为 None）
    - fingerprint: 本次使用的 impersonate 指纹
    - cookie_profile_key: 绑定的 profile_key（cookie 池不可用为 None）
    """

    def __init__(self, client, proxy_url: Optional[str],
                 fingerprint: str, cookie_profile_key: Optional[str] = None):
        self.client = client
        self.proxy_url = proxy_url
        self.fingerprint = fingerprint
        self.cookie_profile_key = cookie_profile_key


def _resolve_proxy_url(pool: IPPool) -> Optional[str]:
    """按优先级取代理：IPPool.acquire → config.PROXY_URL 单代理 → None。"""
    proxy = pool.acquire()
    if proxy:
        return proxy
    if len(pool) > 0:
        logger.warning('All proxies in cooldown, fallback to single proxy')
    single = getattr(_app_config, 'PROXY_URL', None) if _app_config else None
    if single:
        return single
    logger.warning('No proxy available (pool empty, PROXY_URL unset), '
                   'proceeding with bare client')
    return None


@contextmanager
def pool_scoped_client(task_type: str, scraper=None, lease_manager: Optional[LeaseManager] = None,
                       ip_pool: Optional[IPPool] = None,
                       fingerprint_pool: Optional[FingerprintPool] = None):
    """任务级池边界：lease → 组装 client → yield PoolScopedContext → 清理。

    Args:
        task_type: 任务类型标识（如 'pdp'），同时用作 lease key
        scraper: 可选 BaseScraper——提供时把组装好的 client 注入 scraper
            （scraper._client = ctx.client），scraper 在 fetch 前后用
            report_outcome(ctx, ok=..., blocked=...) 上报
        lease_manager: 租约管理器注入（测试用）；None 惰性单例
        ip_pool: IP 池注入（测试用）；None 用 get_ip_pool() 单例
        fingerprint_pool: 指纹池注入（测试用）；None 用 get_fingerprint_pool()

    Yields:
        PoolScopedContext（client 已注入代理与指纹）

    Raises:
        PoolUnavailableError: lease 被占用或获取失败
    """
    manager = lease_manager or LeaseManager()
    # lease 冲突统一包装为 PoolUnavailableError（接入层策略性失败信号，
    # 调用方不必感知 LeaseConflictException 细节）
    try:
        lease_cm = manager.acquire_lease(task_type, ttl=LEASE_TTL_SECONDS)
        lease_cm.__enter__()
    except LeaseConflictException as exc:
        raise PoolUnavailableError(
            f"Task lease conflict on '{task_type}'") from exc
    except Exception as exc:
        raise PoolUnavailableError(
            f"Lease acquisition failed on '{task_type}': "
            f"{type(exc).__name__}") from exc
    try:
        pool = ip_pool if ip_pool is not None else get_ip_pool()
        fp_pool = fingerprint_pool if fingerprint_pool is not None \
            else get_fingerprint_pool()

        proxy_url = _resolve_proxy_url(pool)
        fingerprint = fp_pool.acquire()
        client = _build_client(fingerprint, proxy_url)

        cookie_pool = get_cookie_pool()
        cookie_profile_key = None
        if cookie_pool is not None:
            lease_entry = cookie_pool.acquire()
            if lease_entry is not None:
                cookie_profile_key = lease_entry.profile.profile_key
                _inject_cookies(client, lease_entry.cookies)

        ctx = PoolScopedContext(
            client=client, proxy_url=proxy_url, fingerprint=fingerprint,
            cookie_profile_key=cookie_profile_key)
        if scraper is not None and getattr(scraper, '_client', None) is None:
            # 只在 scraper 尚未注入 client 时注入池 client；
            # 显式注入的 client（测试 mock / 自定义客户端）优先级更高
            scraper._client = client
        try:
            yield ctx
        finally:
            # 无状态归还：仅清理 session 级资源（不把代理/指纹置回池——
            # 成败由 report_outcome 显式上报）
            _close_client(client)
            lease_cm.__exit__(None, None, None)
    except Exception:
        # 组装阶段异常（lease 已持有）：释放租约后原样上抛
        lease_cm.__exit__(None, None, None)
        raise


def _build_client(fingerprint: str, proxy_url: Optional[str]):
    """构造 CurlCffiClient（指纹 + 代理注入）；构造失败回退裸 session 客户端。"""
    proxies: Optional[Dict[str, str]] = None
    if proxy_url:
        proxies = {'http': proxy_url, 'https': proxy_url}
    try:
        from ..anti_detection.curl_client import CurlCffiClient
        return CurlCffiClient(impersonate=fingerprint, proxies=proxies)
    except Exception as exc:
        logger.warning(f"CurlCffiClient build failed, bare client: {exc}")

    class _BareClient:
        """最小 request 兼容客户端（CurlCffiClient 不可用时的降级）。"""

        def __init__(self, fingerprint_, proxies_):
            self.impersonate = fingerprint_
            self.proxies = proxies_
            self.session = None

        def request(self, url, method='GET', headers=None, **kw):
            raise RuntimeError('no HTTP backend available')

        def close(self):
            pass

    return _BareClient(fingerprint, proxies)


def _inject_cookies(client, cookies: Dict[str, str]) -> None:
    """零侵入注入 cookies 到底层 session（无 session 则跳过）。"""
    if not cookies:
        return
    session = getattr(client, 'session', None)
    if session is None:
        logger.warning('client has no injectable session, cookies skipped')
        return
    try:
        session.cookies.update(cookies)
    except Exception as exc:
        logger.warning(f"cookie injection failed: {exc}")


def _close_client(client) -> None:
    """退出上下文时清理 session；任何异常不向上传播。"""
    try:
        client.close()
    except Exception:
        logger.debug('client close failed', exc_info=True)


def inject_proxy_to_client(client, proxy_url: Optional[str]) -> bool:
    """把代理注入 client.session.proxies（兼容 TK 命名的零侵入辅助）。

    Returns:
        True 表示注入成功；client 无 session 或注入失败返回 False。
    """
    if not proxy_url:
        return False
    session = getattr(client, 'session', None)
    if session is None:
        logger.warning('client has no injectable session, proxy skipped')
        return False
    try:
        session.proxies = {'http': proxy_url, 'https': proxy_url}
        return True
    except Exception as exc:
        logger.warning(f"proxy injection failed: {exc}")
        return False


def report_outcome(ctx: PoolScopedContext, ok: bool = False,
                   blocked: bool = False, response_time: Optional[float] = None,
                   ip_pool: Optional[IPPool] = None,
                   fingerprint_pool: Optional[FingerprintPool] = None) -> None:
    """请求结果上报：blocked → 三池联合冷却；ok → 三池联合成功。

    Args:
        ctx: pool_scoped_client yield 的上下文
        ok: 请求成功（200 正常正文）
        blocked: 被 503/challenge 拦截（优先于 ok 判定）
        response_time: 可选耗时（进 IP 池平均响应时间统计）
    """
    pool = ip_pool if ip_pool is not None else get_ip_pool()
    fp_pool = fingerprint_pool if fingerprint_pool is not None \
        else get_fingerprint_pool()

    if blocked:
        if ctx.proxy_url:
            pool.report_failure(ctx.proxy_url)
        if ctx.fingerprint:
            fp_pool.report_ban(ctx.fingerprint)
        cookie_pool = get_cookie_pool()
        if cookie_pool is not None and ctx.cookie_profile_key:
            cookie_pool.report_failure(ctx.cookie_profile_key)
        logger.warning('Pool outcome: blocked '
                       f"(proxy={'yes' if ctx.proxy_url else 'no'}, "
                       f"fp={ctx.fingerprint})")
        return

    if ok:
        if ctx.proxy_url:
            pool.report_success(ctx.proxy_url, response_time=response_time)
        if ctx.fingerprint:
            fp_pool.report_success(ctx.fingerprint)
        cookie_pool = get_cookie_pool()
        if cookie_pool is not None and ctx.cookie_profile_key:
            cookie_pool.report_success(ctx.cookie_profile_key)
