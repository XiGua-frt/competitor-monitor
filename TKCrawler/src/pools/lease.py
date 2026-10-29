"""任务级账号租约（lease）生命周期模块 —— Phase 2: Cookie 池接入抓取流程前置模块.

语义约定：
- 一个采集任务 acquire 一次，任务内固定身份；身份切换只发生在新任务边界。
- 429/WAF 不在 lease 层自动换号：由调用方决策（默认降速/停止），
  lease 层只提供 release + 再次 acquire 的显式入口。
- CookiePoolManager.get_account() 本身不做占用标记（负载统计在
  release_account 时才 +1，连续两次 get_account 可能返回同一账号），
  本模块通过 Redis key 补上占用语义：
      lease:{lease_id}            -> lease 元数据 JSON（SETEX，TTL=lease_ttl_seconds）
      lease:account:{account_id}  -> lease_id（SETEX，同 TTL）
- release 幂等：重复 release 为 no-op，不重复触发底层计数。
- reclaim_expired 兜底清理异常退出残留的 lease key（正常 SETEX 会自动过期）。
- 结构化日志字段白名单：task_id / scraper_type / account_id / lease_id /
  result(success|failure|expired|noop) / duration_s。
  严禁记录 cookies、密码、完整代理 URL。
"""

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from .models import AccountSession
from .exceptions import NoAvailableAccountException
from ..common.logger import get_logger

logger = get_logger(__name__)

LEASE_KEY_PREFIX = 'lease:'
LEASE_ACCOUNT_KEY_PREFIX = 'lease:account:'

# 日志字段白名单：任何日志只允许输出这些结构化字段
_ALLOWED_LOG_FIELDS = (
    'task_id', 'scraper_type', 'account_id', 'lease_id', 'result', 'duration_s',
)


class LeaseExhaustedException(Exception):
    """池中所有候选账号均被活跃 lease 占用（池非空但无可租账号）。

    与 NoAvailableAccountException（池空/无可用账号）可区分：
    前者需要调用方决定是否等待/告警，后者说明池本身枯竭。
    """


def _log_fields(**kwargs: Any) -> str:
    """按白名单顺序拼接结构化日志字段，杜绝 cookies 等敏感信息进入日志。"""
    parts = []
    for key in _ALLOWED_LOG_FIELDS:
        if key in kwargs and kwargs[key] is not None:
            parts.append(f"{key}={kwargs[key]}")
    return ' '.join(parts)


@dataclass
class AccountLease:
    """一次任务级账号租约。不携带 cookie 明文（account 引用由调用方按需取）。"""

    lease_id: str
    account_id: str
    scraper_type: str
    task_id: str
    acquired_at: datetime
    expires_at: datetime
    released: bool = False
    # AccountSession 含 cookies，repr/比较中排除，避免意外泄露
    account: Optional[AccountSession] = field(default=None, repr=False, compare=False)

    @property
    def expired(self) -> bool:
        return datetime.now() >= self.expires_at

    def to_storage_dict(self) -> Dict[str, Any]:
        """Redis 存储载荷（不含任何 cookie 信息）。"""
        return {
            'lease_id': self.lease_id,
            'account_id': self.account_id,
            'scraper_type': self.scraper_type,
            'task_id': self.task_id,
            'acquired_at': self.acquired_at.isoformat(),
            'expires_at': self.expires_at.isoformat(),
        }

    @classmethod
    def from_storage_dict(cls, data: Dict[str, Any]) -> 'AccountLease':
        return cls(
            lease_id=data['lease_id'],
            account_id=data['account_id'],
            scraper_type=data.get('scraper_type', ''),
            task_id=data.get('task_id', ''),
            acquired_at=datetime.fromisoformat(data['acquired_at']),
            expires_at=datetime.fromisoformat(data['expires_at']),
        )

    def __repr__(self) -> str:
        return (
            f"AccountLease(lease_id={self.lease_id!r}, account_id={self.account_id!r}, "
            f"scraper_type={self.scraper_type!r}, task_id={self.task_id!r}, "
            f"released={self.released}, expired={self.expired})"
        )


class LeaseManager:
    """任务级账号租约管理器，为 CookiePoolManager 补上占用语义。"""

    def __init__(self, cookie_pool, lease_ttl_seconds: int = 600,
                 max_acquire_attempts: int = 5):
        """
        Args:
            cookie_pool: CookiePoolManager 实例（复用其 redis 客户端与选号逻辑）
            lease_ttl_seconds: lease 的 Redis TTL，同时是占用自动失效时长
            max_acquire_attempts: acquire 时遇到被占用账号的最大尝试次数
        """
        self.cookie_pool = cookie_pool
        self.redis = cookie_pool.redis
        self.lease_ttl_seconds = int(lease_ttl_seconds)
        self.max_acquire_attempts = int(max_acquire_attempts)

    # ------------------------------------------------------------------ keys

    def _lease_key(self, lease_id: str) -> str:
        return f"{LEASE_KEY_PREFIX}{lease_id}"

    def _account_key(self, account_id: str) -> str:
        return f"{LEASE_ACCOUNT_KEY_PREFIX}{account_id}"

    # ------------------------------------------------------------ internal

    def _load_lease(self, lease_id: str) -> Optional[AccountLease]:
        raw = self.redis.get(self._lease_key(lease_id))
        if not raw:
            return None
        try:
            return AccountLease.from_storage_dict(json.loads(raw))
        except (ValueError, KeyError, TypeError):
            return None

    def _account_lease_id(self, account_id: str) -> Optional[str]:
        """返回占用该账号且未过期的 lease_id；顺带清理过期/残留标记。"""
        lease_id = self.redis.get(self._account_key(account_id))
        if not lease_id:
            return None
        lease = self._load_lease(lease_id)
        if lease is None:
            # 详情 key 已消失（TTL 到期或残留不一致）→ 清理账号占用标记
            self.redis.delete(self._account_key(account_id))
            return None
        if lease.expired:
            self._cleanup_keys(lease)
            return None
        return lease_id

    def _cleanup_keys(self, lease: AccountLease) -> None:
        """删除 lease 详情 key；账号占用标记仅在其仍指向本 lease 时删除，
        避免误删后续新 lease 的标记。"""
        self.redis.delete(self._lease_key(lease.lease_id))
        account_key = self._account_key(lease.account_id)
        holder = self.redis.get(account_key)
        if holder is None or holder == lease.lease_id:
            self.redis.delete(account_key)

    # ------------------------------------------------------------------ API

    def acquire(self, scraper_type: str, task_id: str,
                strategy: str = 'weighted') -> AccountLease:
        """为一次采集任务租用一个账号。

        循环调用 cookie_pool.get_account(strategy)；若候选账号已有活跃 lease
        则跳过换下一个，最多 max_acquire_attempts 次。

        Raises:
            NoAvailableAccountException: 池空/无可用账号（自然传播）
            LeaseExhaustedException: 候选账号全部被活跃 lease 占用
        """
        occupied_count = 0
        for _attempt in range(1, self.max_acquire_attempts + 1):
            try:
                session = self.cookie_pool.get_account(strategy=strategy)
            except NoAvailableAccountException:
                logger.warning(_log_fields(
                    task_id=task_id, scraper_type=scraper_type, result='noop',
                ) + ' | pool has no available account, acquire aborted')
                raise

            account_id = session.account_id
            if self._account_lease_id(account_id) is not None:
                occupied_count += 1
                logger.debug(_log_fields(
                    task_id=task_id, scraper_type=scraper_type,
                    account_id=account_id, result='noop',
                ) + ' | candidate occupied, skipping')
                continue

            now = datetime.now()
            lease = AccountLease(
                lease_id=f"ls_{uuid.uuid4().hex[:12]}",
                account_id=account_id,
                scraper_type=scraper_type,
                task_id=task_id,
                acquired_at=now,
                expires_at=now + timedelta(seconds=self.lease_ttl_seconds),
                account=session,
            )
            payload = json.dumps(lease.to_storage_dict())
            self.redis.set(self._lease_key(lease.lease_id),
                           payload, ex=self.lease_ttl_seconds)
            self.redis.set(self._account_key(account_id),
                           lease.lease_id, ex=self.lease_ttl_seconds)
            logger.info(_log_fields(
                task_id=task_id, scraper_type=scraper_type,
                account_id=account_id, lease_id=lease.lease_id, result='success',
            ) + ' | lease acquired')
            return lease

        raise LeaseExhaustedException(
            f"All {occupied_count} candidate account(s) occupied "
            f"after {self.max_acquire_attempts} attempts"
        )

    def release(self, lease: AccountLease, success: bool = True,
                response_time: float = 0) -> str:
        """释放租约（幂等）。返回 'success'|'failure'|'expired'|'noop'。

        第一次调用：删除 Redis 标记并回调 cookie_pool.release_account 更新
        健康分/计数。重复调用：no-op，不重复计数。已过期 lease 的 release
        只做清理，不回写底层计数（账号此时可能已被复用，避免重复计数）。
        """
        if lease.released:
            logger.info(_log_fields(
                task_id=lease.task_id, scraper_type=lease.scraper_type,
                account_id=lease.account_id, lease_id=lease.lease_id,
                result='noop',
            ) + ' | lease already released, release is a no-op')
            return 'noop'

        # 先置位再清理，尽力而为保证并发下不重复计数
        lease.released = True
        self._cleanup_keys(lease)

        if lease.expired:
            logger.info(_log_fields(
                task_id=lease.task_id, scraper_type=lease.scraper_type,
                account_id=lease.account_id, lease_id=lease.lease_id,
                result='expired',
            ) + ' | lease already expired, cleanup only')
            return 'expired'

        duration_s = max(0.0, (datetime.now() - lease.acquired_at).total_seconds())
        self.cookie_pool.release_account(
            lease.account_id, success=success, response_time=response_time
        )
        logger.info(_log_fields(
            task_id=lease.task_id, scraper_type=lease.scraper_type,
            account_id=lease.account_id, lease_id=lease.lease_id,
            result='success' if success else 'failure',
            duration_s=round(duration_s, 3),
        ) + ' | lease released')
        return 'success' if success else 'failure'

    def reclaim_expired(self) -> int:
        """清扫已过期但未删除的 lease key，返回清理数量。

        真实 Redis 的 SETEX 会自动过期，本方法主要兜底异常退出残留
        （如无 TTL 或 expires_at 已过但 key 仍存在的 key）。
        """
        reclaimed = 0
        now = datetime.now()
        for key in self.redis.scan_iter(match=f"{LEASE_KEY_PREFIX}*"):
            # 跳过账号占用标记 key（lease:account:*），只清 lease 详情 key
            if key.startswith(LEASE_ACCOUNT_KEY_PREFIX):
                continue
            raw = self.redis.get(key)
            if raw is None:
                continue
            try:
                lease = AccountLease.from_storage_dict(json.loads(raw))
            except (ValueError, KeyError, TypeError):
                # 无法解析的残留垃圾，直接清理
                self.redis.delete(key)
                reclaimed += 1
                continue
            if lease.expires_at <= now:
                self._cleanup_keys(lease)
                reclaimed += 1
                logger.info(_log_fields(
                    account_id=lease.account_id, lease_id=lease.lease_id,
                    result='expired',
                ) + ' | expired lease reclaimed')
        return reclaimed

    def active_leases(self) -> List[AccountLease]:
        """当前活跃（未过期、未释放）的 lease 列表，用于观测。"""
        leases: List[AccountLease] = []
        now = datetime.now()
        for key in self.redis.scan_iter(match=f"{LEASE_KEY_PREFIX}*"):
            if key.startswith(LEASE_ACCOUNT_KEY_PREFIX):
                continue
            raw = self.redis.get(key)
            if raw is None:
                continue
            try:
                lease = AccountLease.from_storage_dict(json.loads(raw))
            except (ValueError, KeyError, TypeError):
                continue
            if lease.expires_at > now:
                lease.account = self.cookie_pool.accounts.get(lease.account_id)
                leases.append(lease)
        return leases
