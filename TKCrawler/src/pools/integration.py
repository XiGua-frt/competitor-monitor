"""Cookie 池接入采集链路 —— Phase 3 + Phase 4 (docs/POOL_INTEGRATION_EXECUTION_PLAN.md).

职责边界：
- 本模块是池系统与 main.py run_*_scrape 之间的唯一接入层；scraper 不从全局池隐式取号。
- 一个任务 acquire 一次，任务内固定身份；不在请求中途换号；不引入并发。
- 429/WAF/SessionExpired 不在接入层自动换号重试（决策记录：默认停止/降速），
  异常继续上抛，finally 中 release(success=False)。
- POOL_ENABLED=false → yield (lease=None, shadow=False)，调用方走原路径，行为完全不变。
- POOL_SHADOW_MODE=true → 只选号记日志，不注入 cookie；lease 仍 acquire/release。
- POOL_FAIL_POLICY:
    fail_closed     池开启但 acquire 失败（空池/全占用/Redis 故障）→ 抛 PoolUnavailableError，
                    由 main() 转退出码 2（依赖错误，与数据库不可用同类，不静默降级）。
    fallback_single 记 WARNING 后回退现有单 session 原路径。
- 结构化日志字段白名单：task_id / scraper_type / account_id / lease_id / result /
  duration_s。严禁记录 cookie 内容、密码、完整代理凭据。
- CookiePoolManager/LeaseManager/IPPoolManager 惰性单例（get_lease_manager /
  get_ip_pool_manager）：--dry-run 或池关闭路径绝不连 Redis。
- IP 池注入（POOL_IP_ENABLED=true 时）：非影子 lease 按 account_id 从 IP 池取
  绑定代理注入 client.session.proxies（覆盖 config.PROXY_URL）；池中无可用代理
  → 记 WARNING 并保留原 PROXY_URL 配置（IP 池是增强项，不做 fail_closed）。
"""

import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional

from ..common.config import config
from ..common.logger import get_logger
from .lease import AccountLease, LeaseManager, _log_fields

logger = get_logger(__name__)

__all__ = [
    'PoolUnavailableError',
    'LeaseContext',
    'pool_scoped_client',
    'inject_cookie_to_client',
    'inject_proxy_to_client',
    'mask_proxy_url',
    'get_lease_manager',
    'reset_lease_manager',
    'get_ip_pool_manager',
    'reset_ip_pool_manager',
]


class PoolUnavailableError(Exception):
    """Cookie 池已开启但不可用（空池/全占用/Redis 故障）且策略为 fail_closed。

    与 NoAvailableAccountException（空池）、LeaseExhaustedException（全占用）可区分：
    本异常是接入层按 POOL_FAIL_POLICY=fail_closed 做出的策略性失败信号，
    调用方（main.py）统一转退出码 2。
    """


# 模块级单例缓存；None 表示尚未初始化（未触碰任何 Redis 连接）
_lease_manager: Optional[LeaseManager] = None
_ip_pool_manager = None


def get_lease_manager() -> LeaseManager:
    """惰性创建 LeaseManager 单例（含 CookiePoolManager），首次调用才连 Redis。"""
    global _lease_manager
    if _lease_manager is None:
        from .cookie_pool import CookiePoolManager

        cookie_pool = CookiePoolManager()
        _lease_manager = LeaseManager(
            cookie_pool,
            lease_ttl_seconds=int(getattr(config, 'POOL_LEASE_TTL_SECONDS', 600)),
        )
    return _lease_manager


def reset_lease_manager() -> None:
    """重置单例（测试用；生产进程无需调用）。"""
    global _lease_manager
    _lease_manager = None


def get_ip_pool_manager():
    """惰性创建 IPPoolManager 单例，首次调用才连 Redis。"""
    global _ip_pool_manager
    if _ip_pool_manager is None:
        from .ip_pool import IPPoolManager

        _ip_pool_manager = IPPoolManager()
    return _ip_pool_manager


def reset_ip_pool_manager() -> None:
    """重置 IP 池单例（测试用；生产进程无需调用）。"""
    global _ip_pool_manager
    _ip_pool_manager = None


@dataclass
class LeaseContext:
    """pool_scoped_client 的 yield 对象。

    - lease: 本次任务的 AccountLease；池关闭或回退原路径时为 None。
    - shadow: 影子模式标记；True 时调用方不得注入 cookie。
    - success: 调用方在采集成功时置 True；finally 中据此 release(success=...)。
      异常路径保持 False。
    - active_lease: 非影子模式下可注入的 lease（影子/池关闭为 None）。
    """

    lease: Optional[AccountLease] = None
    shadow: bool = False
    success: bool = False
    acquired_at_monotonic: float = field(default=0.0)

    @property
    def active_lease(self) -> Optional[AccountLease]:
        if self.lease is not None and not self.shadow:
            return self.lease
        return None


@contextmanager
def pool_scoped_client(scraper_type: str, task_id: Optional[str] = None,
                       dry_run: bool = False):
    """任务级池边界：acquire → yield LeaseContext → finally release。

    Args:
        scraper_type: 'shop' | 'video' | 'creator' | 'hashtag' | 'itemlist'
        task_id: 任务标识；缺省自动生成（不进日志的敏感字段除外，均按白名单输出）
        dry_run: True 时不触碰池（--dry-run 冒烟路径）

    Raises:
        PoolUnavailableError: 池开启但不可用且 POOL_FAIL_POLICY=fail_closed
    """
    if task_id is None:
        task_id = f"task_{uuid.uuid4().hex[:12]}"

    ctx = LeaseContext()

    # 池关闭 / dry-run：完全原路径，不连 Redis
    if dry_run or not config.POOL_ENABLED:
        yield ctx
        return

    strategy = getattr(config, 'POOL_STRATEGY', 'weighted')
    fail_policy = getattr(config, 'POOL_FAIL_POLICY', 'fail_closed')

    try:
        manager = get_lease_manager()
        ctx.acquired_at_monotonic = time.monotonic()
        lease = manager.acquire(scraper_type, task_id, strategy=strategy)
    except Exception as exc:
        # 空池 / 全占用 / Redis 故障统一走失败策略；不静默、不无限重试
        if fail_policy == 'fallback_single':
            logger.warning(_log_fields(
                task_id=task_id, scraper_type=scraper_type, result='fallback',
            ) + f' | pool unavailable ({type(exc).__name__}), '
                'falling back to single session path')
            yield ctx
            return
        raise PoolUnavailableError(
            f"Cookie 池开启但不可用 (POOL_FAIL_POLICY=fail_closed): "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    ctx.lease = lease
    ctx.shadow = bool(getattr(config, 'POOL_SHADOW_MODE', False))
    try:
        if ctx.shadow:
            logger.info(_log_fields(
                task_id=task_id, scraper_type=scraper_type,
                account_id=lease.account_id, lease_id=lease.lease_id,
                result='shadow',
            ) + ' | shadow mode: account selected, cookie NOT injected')
        yield ctx
    finally:
        response_time = time.monotonic() - ctx.acquired_at_monotonic
        try:
            manager.release(lease, success=ctx.success, response_time=response_time)
        except Exception as exc:
            # release 失败不吞掉业务异常，但必须留痕；SETEX TTL 兜底回收
            logger.warning(_log_fields(
                task_id=task_id, scraper_type=scraper_type,
                account_id=lease.account_id, lease_id=lease.lease_id,
                result='failure',
            ) + f' | lease release error: {type(exc).__name__}: {exc}')


def get_client_cookies(client) -> dict:
    """读取 client（含 AntiDetectionClient 包装类）session cookie jar 快照。

    供 scraper 侧判断"池租约身份是否已注入"并优先使用，空 dict 表示
    client 上没有可用 cookie（调用方回退 session 文件等其他来源）。
    """
    session = _resolve_session(client)
    if session is None:
        return {}
    try:
        return dict(session.cookies)
    except Exception:
        return {}


def _resolve_session(client):
    """零侵入地定位可注入 cookie 的 session 对象。

    - 优先 client.session（测试替身/自定义客户端）。
    - 否则回退 client._curl.session（AntiDetectionClient 实际结构）。
    """
    session = getattr(client, 'session', None)
    if session is None:
        curl = getattr(client, '_curl', None)
        session = getattr(curl, 'session', None)
    return session


def inject_cookie_to_client(client, lease: AccountLease) -> None:
    """把 lease.account.cookies 注入 client 的底层 session（零侵入）。

    只更新 session.cookies；不触碰代理配置（config.PROXY_URL 逻辑原样保留），
    本阶段不接 IP 池。日志仅输出白名单字段，严禁记录 cookie 内容。
    """
    if lease is None or getattr(lease, 'account', None) is None:
        return
    session = _resolve_session(client)
    cookies = dict(lease.account.cookies or {})
    if session is None:
        logger.warning(_log_fields(
            task_id=lease.task_id, scraper_type=lease.scraper_type,
            account_id=lease.account_id, lease_id=lease.lease_id,
            result='noop',
        ) + ' | client has no injectable session, cookie injection skipped')
        return
    session.cookies.update(cookies)
    logger.info(_log_fields(
        task_id=lease.task_id, scraper_type=lease.scraper_type,
        account_id=lease.account_id, lease_id=lease.lease_id,
        result='success',
    ) + f' | cookies injected ({len(cookies)} entries)')


def mask_proxy_url(proxy_url: str) -> str:
    """脱敏代理 URL：隐藏 userinfo 凭据，仅保留 scheme/host:port。

    http://user:pass@1.2.3.4:8080 → http://***@1.2.3.4:8080
    无凭据的 URL 原样返回。日志/异常消息中展示代理一律走本函数。
    """
    try:
        from urllib.parse import urlparse, urlunparse

        parts = urlparse(proxy_url)
        if parts.username or parts.password:
            return urlunparse(parts._replace(
                netloc=f'***@{parts.hostname}'
                       + (f':{parts.port}' if parts.port else ''),
            ))
    except Exception:
        return '<unparseable-proxy-url>'
    return proxy_url


def inject_proxy_to_client(client, lease: Optional[AccountLease]) -> Optional[str]:
    """把 lease 账号绑定的 IP 池代理注入 client.session.proxies（零侵入）。

    行为：
    - POOL_IP_ENABLED=false → 不做任何事（原 PROXY_URL 路径不变）。
    - lease 为 None（影子/池关闭）→ 不做任何事。
    - 按账号取绑定代理（get_proxy 会自动复用已绑定代理；无绑定时自动分配）。
    - 池中无可用代理 / Redis 故障 → 记 WARNING 并保留 client 现有代理配置
      （IP 池为增强项，不引入 fail_closed；调用方仍走 config.PROXY_URL 出口）。

    Returns:
        注入成功的 proxy_id；未注入（关闭/无 lease/失败回退）返回 None。
    日志仅输出 proxy_id 与脱敏 URL，严禁记录代理凭据。
    """
    if lease is None or getattr(lease, 'account', None) is None:
        return None
    if not getattr(config, 'POOL_IP_ENABLED', False):
        return None

    base = dict(
        task_id=lease.task_id, scraper_type=lease.scraper_type,
        account_id=lease.account_id, lease_id=lease.lease_id,
    )
    session = _resolve_session(client)
    if session is None:
        logger.warning(_log_fields(result='noop', **base)
                       + ' | client has no injectable session, proxy injection skipped')
        return None

    try:
        ip_pool = get_ip_pool_manager()
        proxy = ip_pool.get_proxy(lease.account_id)
    except Exception as exc:
        logger.warning(_log_fields(result='fallback_ip', **base)
                       + f' | ip pool unavailable ({type(exc).__name__}), '
                         'keeping existing proxy config')
        return None

    try:
        session.proxies = {
            'http': proxy.proxy_url,
            'https': proxy.proxy_url,
        }
    except Exception as exc:
        logger.warning(_log_fields(result='failure', **base)
                       + f' | proxy injection failed: {type(exc).__name__}')
        return None

    logger.info(_log_fields(result='success', **base)
                + f' | proxy injected: proxy_id={proxy.proxy_id} '
                  f'region={proxy.region} url={mask_proxy_url(proxy.proxy_url)}')
    return proxy.proxy_id
