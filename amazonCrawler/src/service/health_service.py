"""HealthService — 运行健康状态探测（DB / 配额 / 冷却 / 代理池）。"""

import time
from typing import Optional

try:
    from src.common.config import config
    from src.common.logger import get_logger
except ImportError:  # pragma: no cover
    config = None
    get_logger = None

if get_logger is not None:
    logger = get_logger(__name__)
else:  # pragma: no cover
    import logging
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)


class HealthService:
    """健康状态：db_ok / quota / cooldown_active / pool_healthy。"""

    def __init__(self, session_factory=None, rate_limiter=None, ip_pool=None,
                 quota_limits: Optional[dict] = None):
        """
        Args:
            session_factory: 会话工厂（测试注入；None 走全局 SessionLocal）。
            rate_limiter: RateLimiter 实例（读 get_quota_status）；
                          None 时惰性从 BaseScraper 实例取或降级 config limit。
            ip_pool: IPPool 实例（读 healthy_count）；None → pool_healthy=None。
            quota_limits: {resource_type: limit} 覆盖；缺省用 config 配额。
        """
        self._session_factory = session_factory
        self._rate_limiter = rate_limiter
        self._ip_pool = ip_pool
        self._quota_limits = quota_limits  # None → 从 config 读取

    # ------------------------------------------------------------------
    def status(self) -> dict:
        """汇总健康状态（任何子项探测失败不抛错，降级为 False/None）。"""
        return {
            "db_ok": self._check_db(),
            "quota": self._quota_status(),
            "cooldown_active": self._cooldown_active(),
            "pool_healthy": self._pool_healthy(),
            "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    # ------------------------------------------------------------------
    def _check_db(self) -> bool:
        """SELECT 1 探测；失败 False。"""
        try:
            if self._session_factory is not None:
                with self._session_factory() as session:
                    session.execute(
                        __import__("sqlalchemy").text("SELECT 1"))
                return True
            from src.database.connection import get_session
            from sqlalchemy import text
            with get_session() as session:
                session.execute(text("SELECT 1"))
            return True
        except Exception as exc:
            logger.warning("DB 健康探测失败: {}", exc)
            return False

    def _quota_limits_resolved(self) -> dict:
        if self._quota_limits is not None:
            return dict(self._quota_limits)
        if config is not None:
            return {"asins": config.DAILY_QUOTA_ASINS,
                    "search": config.DAILY_QUOTA_SEARCH}
        return {"asins": 200, "search": 50}

    def _quota_status(self) -> dict:
        """{resource_type: {used, limit}}；RateLimiter 缺失时 used=None。"""
        limits = self._quota_limits_resolved()
        result = {}
        rl = self._get_rate_limiter()
        for resource_type, limit in limits.items():
            if rl is not None:
                try:
                    st = rl.get_quota_status(resource_type, limit)
                    result[resource_type] = {
                        "used": st.get("used"), "limit": limit}
                    continue
                except Exception as exc:
                    logger.debug("quota status via RateLimiter 失败: {}", exc)
            result[resource_type] = {"used": None, "limit": limit}
        return result

    def _get_rate_limiter(self):
        """惰性取 RateLimiter：注入优先，其次从现有 BaseScraper 实例借 limiter。"""
        if self._rate_limiter is not None:
            return self._rate_limiter
        try:
            from src.scrapers.base_scraper import BaseScraper  # noqa: F401
            # 不主动构造真实 scraper（会连 Redis）；仅当调用方已注入时可用
            return None
        except Exception:  # pragma: no cover
            return None

    def _cooldown_active(self) -> bool:
        """是否有爬虫处于 BotBlocked 软冷却（ScrapeService 全局登记表）。"""
        try:
            from src.service.scrape_service import get_cooldowns
            return bool(get_cooldowns())
        except Exception:  # pragma: no cover
            return False

    def _pool_healthy(self):
        """代理池健康节点数；未配置池返回 None（未知 ≠ 异常）。"""
        if self._ip_pool is None:
            return None
        try:
            return self._ip_pool.healthy_count()
        except Exception:  # pragma: no cover
            return None
