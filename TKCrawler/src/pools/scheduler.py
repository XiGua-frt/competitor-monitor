"""Task scheduler with intelligent load balancing."""

import random
from typing import Dict, List, Optional, Tuple
from datetime import datetime
from dataclasses import dataclass

from .cookie_pool import CookiePoolManager
from .ip_pool import IPPoolManager
from .models import AccountSession, ProxyNode, AccountStatus
from .exceptions import NoAvailableAccountException, NoAvailableProxyException
from ..common.logger import get_logger

logger = get_logger(__name__)


@dataclass
class Task:
    """Task model for scheduling."""
    task_id: str
    task_type: str              # 任务类型（用于sticky策略）
    target: str                 # 目标URL或对象
    priority: int = 0           # 优先级
    account_id: Optional[str] = None  # 指定账号（sticky策略使用）
    assigned_account: Optional[str] = None  # 实际分配的账号
    assigned_proxy: Optional[str] = None    # 实际分配的代理
    created_at: datetime = None
    
    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now()


class PoolScheduler:
    """Task scheduler with multiple load balancing strategies."""
    
    STRATEGIES = ['round_robin', 'least_loaded', 'weighted', 'sticky']
    
    def __init__(
        self,
        cookie_pool: CookiePoolManager,
        ip_pool: IPPoolManager,
        default_strategy: str = 'weighted'
    ):
        """Initialize pool scheduler.
        
        Args:
            cookie_pool: Cookie pool manager
            ip_pool: IP pool manager
            default_strategy: Default scheduling strategy
        """
        if default_strategy not in self.STRATEGIES:
            raise ValueError(
                f"Invalid strategy: {default_strategy}. "
                f"Must be one of {self.STRATEGIES}"
            )
        
        self.cookie_pool = cookie_pool
        self.ip_pool = ip_pool
        self.default_strategy = default_strategy
        
        # Sticky mappings: {task_type: account_id}
        self.sticky_mappings: Dict[str, str] = {}
        
        # Round-robin state
        self._rr_index = 0
        
        logger.info(
            f"PoolScheduler initialized with strategy: {default_strategy}"
        )
    
    def assign_task(
        self,
        task: Task,
        strategy: Optional[str] = None
    ) -> Tuple[AccountSession, ProxyNode]:
        """Assign task to optimal account and proxy.
        
        Args:
            task: Task to assign
            strategy: Scheduling strategy (defaults to self.default_strategy)
        
        Returns:
            Tuple of (AccountSession, ProxyNode)
        
        Raises:
            NoAvailableAccountException: When no account is available
            NoAvailableProxyException: When no proxy is available
        """
        strategy = strategy or self.default_strategy
        
        if strategy not in self.STRATEGIES:
            raise ValueError(
                f"Invalid strategy: {strategy}. Must be one of {self.STRATEGIES}"
            )
        
        # Select account based on strategy
        if strategy == 'round_robin':
            account = self._assign_round_robin(task)
        elif strategy == 'least_loaded':
            account = self._assign_least_loaded(task)
        elif strategy == 'weighted':
            account = self._assign_weighted(task)
        elif strategy == 'sticky':
            account = self._assign_sticky(task)
        else:
            # Fallback to weighted
            account = self._assign_weighted(task)
        
        # Get associated proxy
        proxy = self.ip_pool.get_proxy(account.proxy_id)
        
        if not proxy:
            raise NoAvailableProxyException(
                f"Proxy {account.proxy_id} not found for account {account.account_id}"
            )
        
        # Verify proxy health
        if proxy.health_score <= 0.5:
            logger.warning(
                f"Proxy {proxy.proxy_id} has low health score: {proxy.health_score}"
            )
            # Try to find a healthier proxy if available
            try:
                healthy_proxy = self._find_healthy_proxy(account)
                if healthy_proxy:
                    proxy = healthy_proxy
                    # Update account's proxy binding
                    account.proxy_id = proxy.proxy_id
                    self.cookie_pool._save_account(account)
            except Exception as e:
                logger.warning(f"Failed to reassign proxy: {e}")
        
        # Record assignment
        task.assigned_account = account.account_id
        task.assigned_proxy = proxy.proxy_id
        
        logger.info(
            f"Assigned task {task.task_id} ({task.task_type}) "
            f"to account {account.account_id} with proxy {proxy.proxy_id} "
            f"using {strategy} strategy"
        )
        
        return account, proxy
    
    def release_task(
        self,
        task: Task,
        success: bool,
        response_time: float = 0
    ):
        """Release task and update account/proxy metrics.
        
        Args:
            task: Completed task
            success: Whether task succeeded
            response_time: Response time in seconds
        """
        if not task.assigned_account:
            logger.warning(f"Task {task.task_id} has no assigned account")
            return
        
        # Release account
        self.cookie_pool.release_account(
            task.assigned_account,
            success=success,
            response_time=response_time
        )
        
        # Update proxy metrics if available
        if task.assigned_proxy:
            try:
                proxy = self.ip_pool.get_proxy(task.assigned_proxy)
                if proxy:
                    proxy.total_checks += 1
                    if success:
                        proxy.success_checks += 1
                    
                    # Update success rate
                    proxy.success_rate = proxy.success_checks / proxy.total_checks
                    
                    # Update latency
                    if response_time > 0:
                        if proxy.avg_latency == 0:
                            proxy.avg_latency = response_time * 1000  # Convert to ms
                        else:
                            # Moving average
                            proxy.avg_latency = (
                                proxy.avg_latency * 0.8 + response_time * 1000 * 0.2
                            )
                    
                    proxy.updated_at = datetime.now()
                    self.ip_pool._save_proxy(proxy)
            except Exception as e:
                logger.warning(f"Failed to update proxy metrics: {e}")
        
        logger.debug(
            f"Released task {task.task_id} from account {task.assigned_account} "
            f"(success: {success}, response_time: {response_time:.2f}s)"
        )
    
    def rebalance_cooling_accounts(self) -> int:
        """Rebalance tasks from cooling accounts.
        
        Returns:
            Number of accounts resumed
        """
        resumed_count = self.cookie_pool.resume_cooling_accounts()
        
        # Clear sticky mappings for suspended accounts
        suspended = self.cookie_pool.redis.smembers('accounts:suspended')
        cleared = 0
        
        for task_type, account_id in list(self.sticky_mappings.items()):
            if account_id in suspended:
                del self.sticky_mappings[task_type]
                cleared += 1
                logger.info(f"Cleared sticky mapping for {task_type}")
        
        logger.info(
            f"Rebalanced pool: resumed {resumed_count} accounts, "
            f"cleared {cleared} sticky mappings"
        )
        
        return resumed_count
    
    def get_scheduler_stats(self) -> dict:
        """Get scheduler statistics.
        
        Returns:
            Statistics dictionary
        """
        cookie_stats = self.cookie_pool.get_pool_stats()
        ip_stats = self.ip_pool.get_pool_stats()
        
        return {
            'strategy': self.default_strategy,
            'sticky_mappings': len(self.sticky_mappings),
            'sticky_details': dict(self.sticky_mappings),
            'cookie_pool': cookie_stats,
            'ip_pool': ip_stats,
            'timestamp': datetime.now().isoformat()
        }
    
    # ============ Private Strategy Methods ============
    
    def _assign_round_robin(self, task: Task) -> AccountSession:
        """Assign using round-robin strategy.
        
        Args:
            task: Task to assign
        
        Returns:
            AccountSession
        """
        # Get all active accounts
        active_ids = list(self.cookie_pool.redis.smembers('accounts:active'))
        
        if not active_ids:
            raise NoAvailableAccountException("No active accounts in pool")
        
        # Load and filter available accounts
        candidates = []
        for acc_id in active_ids:
            session = self.cookie_pool._load_account(acc_id)
            if session and session.is_available():
                candidates.append(session)
        
        if not candidates:
            raise NoAvailableAccountException("No available accounts")
        
        # Sort by account_id for deterministic ordering
        candidates.sort(key=lambda x: x.account_id)
        
        # Select using round-robin index
        selected = candidates[self._rr_index % len(candidates)]
        self._rr_index += 1
        
        logger.debug(
            f"Round-robin selected account {selected.account_id} "
            f"(index: {self._rr_index - 1})"
        )
        
        return selected
    
    def _assign_least_loaded(self, task: Task) -> AccountSession:
        """Assign using least-loaded strategy.
        
        Args:
            task: Task to assign
        
        Returns:
            AccountSession
        """
        # Get all active accounts
        active_ids = self.cookie_pool.redis.smembers('accounts:active')
        
        if not active_ids:
            raise NoAvailableAccountException("No active accounts in pool")
        
        # Load and filter available accounts with healthy proxies
        candidates = []
        for acc_id in active_ids:
            session = self.cookie_pool._load_account(acc_id)
            
            if not session or not session.is_available():
                continue
            
            # Check proxy health
            proxy = self.ip_pool.get_proxy(session.proxy_id)
            if not proxy or proxy.health_score <= 0.5:
                continue
            
            candidates.append(session)
        
        if not candidates:
            raise NoAvailableAccountException(
                "No available accounts with healthy proxies"
            )
        
        # Select account with minimum current load
        selected = min(candidates, key=lambda x: x.daily_request_count)
        
        logger.debug(
            f"Least-loaded selected account {selected.account_id} "
            f"(load: {selected.daily_request_count}/{selected.daily_quota})"
        )
        
        return selected
    
    def _assign_weighted(self, task: Task) -> AccountSession:
        """Assign using weighted strategy.
        
        Args:
            task: Task to assign
        
        Returns:
            AccountSession
        """
        # Get all active accounts
        active_ids = self.cookie_pool.redis.smembers('accounts:active')
        
        if not active_ids:
            raise NoAvailableAccountException("No active accounts in pool")
        
        # Load accounts: available candidates and zero-weight fallbacks
        available = []
        zero_weight_fallback = []
        for acc_id in active_ids:
            session = self.cookie_pool._load_account(acc_id)
            if not session:
                continue
            
            if session.is_available():
                available.append(session)
            elif (
                session.status == AccountStatus.ACTIVE.value
                and not (
                    session.cooling_until
                    and datetime.now() < session.cooling_until
                )
            ):
                # Active but zero-weight (e.g. quota exhausted or health 0):
                # keep as fallback candidate instead of discarding
                zero_weight_fallback.append(session)
        
        candidates = available if available else zero_weight_fallback
        
        if not candidates:
            raise NoAvailableAccountException("No available accounts")
        
        # Calculate weights: health_score * (1 - current_load / daily_quota)
        weights = []
        for acc in candidates:
            load_factor = 1 - (acc.daily_request_count / acc.daily_quota)
            weight = acc.health_score * load_factor
            weights.append(weight)
        
        total_weight = sum(weights)
        
        if total_weight == 0:
            # All accounts have zero weight, fallback to random
            selected = random.choice(candidates)
        else:
            # Weighted random selection
            selected = random.choices(candidates, weights=weights)[0]
        
        logger.debug(
            f"Weighted selected account {selected.account_id} "
            f"(health: {selected.health_score:.2f}, "
            f"load: {selected.daily_request_count}/{selected.daily_quota})"
        )
        
        return selected
    
    def _assign_sticky(self, task: Task) -> AccountSession:
        """Assign using sticky strategy.
        
        Priority:
        1. If task.account_id is specified and healthy, use it
        2. If task_type has sticky mapping and account is healthy, use it
        3. Otherwise, create new mapping using weighted strategy
        
        Args:
            task: Task to assign
        
        Returns:
            AccountSession
        """
        # Priority 1: Explicit account_id in task
        if task.account_id:
            account = self.cookie_pool._load_account(task.account_id)
            
            if account and account.is_available():
                logger.debug(
                    f"Sticky assignment (explicit): {task.task_type} -> {task.account_id}"
                )
                return account
            else:
                logger.warning(
                    f"Task specified account {task.account_id} is unavailable, "
                    f"falling back to weighted strategy"
                )
        
        # Priority 2: Existing sticky mapping for task_type
        if task.task_type in self.sticky_mappings:
            account_id = self.sticky_mappings[task.task_type]
            account = self.cookie_pool._load_account(account_id)
            
            if account and account.is_available():
                logger.debug(
                    f"Sticky assignment (mapped): {task.task_type} -> {account_id}"
                )
                return account
            else:
                # Mapping expired, remove it
                del self.sticky_mappings[task.task_type]
                logger.info(
                    f"Sticky mapping expired for {task.task_type}, creating new mapping"
                )
        
        # Priority 3: Create new sticky mapping using weighted strategy
        account = self._assign_weighted(task)
        self.sticky_mappings[task.task_type] = account.account_id
        
        logger.info(
            f"Created sticky mapping: {task.task_type} -> {account.account_id}"
        )
        
        return account
    
    def _find_healthy_proxy(self, account: AccountSession) -> Optional[ProxyNode]:
        """Find a healthy proxy for account.
        
        Args:
            account: Account session
        
        Returns:
            ProxyNode or None if no healthy proxy found
        """
        # Get all active proxies
        active_proxies = self.ip_pool.redis.smembers('proxies:active')
        
        for proxy_id in active_proxies:
            proxy = self.ip_pool.get_proxy(proxy_id)
            
            if not proxy:
                continue
            
            # Check health score
            if proxy.health_score <= 0.5:
                continue
            
            # Check if proxy has capacity
            if len(proxy.bound_accounts) >= proxy.max_accounts:
                continue
            
            return proxy
        
        return None
