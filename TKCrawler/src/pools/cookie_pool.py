"""Cookie Pool Manager for managing multiple TikTok accounts."""

import uuid
import random
from typing import Dict, List, Optional
from datetime import datetime, timedelta
import redis

from .models import AccountSession, AccountStatus
from .exceptions import (
    NoAvailableAccountException,
    AccountSuspendedException,
    AccountQuotaExceededException
)
from ..common.logger import get_logger
from ..common.config import config

logger = get_logger(__name__)


class CookiePoolManager:
    """Manages multiple TikTok account sessions with cookies."""
    
    def __init__(self, redis_client: redis.Redis = None):
        """Initialize cookie pool manager.
        
        Args:
            redis_client: Redis client for persistence
        """
        if redis_client is None:
            redis_client = redis.Redis(
                host=config.REDIS_HOST,
                port=config.REDIS_PORT,
                password=config.REDIS_PASSWORD,
                db=config.REDIS_DB,
                decode_responses=True
            )
        
        self.redis = redis_client
        self.accounts: Dict[str, AccountSession] = {}
        
        # Load existing accounts from Redis
        self._load_all_accounts()
        
        logger.info(f"CookiePoolManager initialized with {len(self.accounts)} accounts")
    
    def add_account(
        self,
        username: str,
        cookies: Dict[str, str],
        proxy_id: str,
        fingerprint_id: str = '',
        daily_quota: int = 200
    ) -> str:
        """Add a new account to the pool.
        
        Args:
            username: TikTok username
            cookies: Cookie dictionary
            proxy_id: Bound proxy ID
            fingerprint_id: Bound fingerprint ID
            daily_quota: Daily request quota
        
        Returns:
            account_id: Generated account ID
        """
        account_id = f"acc_{uuid.uuid4().hex[:8]}"
        
        session = AccountSession(
            account_id=account_id,
            username=username,
            cookies=cookies,
            proxy_id=proxy_id,
            fingerprint_id=fingerprint_id,
            daily_quota=daily_quota,
            status=AccountStatus.ACTIVE.value
        )
        
        # Save to Redis
        self._save_account(session)
        
        # Add to active set
        self.redis.sadd('accounts:active', account_id)
        
        # Cache in memory
        self.accounts[account_id] = session
        
        logger.info(f"Added account {username} ({account_id}) to pool")
        return account_id
    
    def remove_account(self, account_id: str) -> bool:
        """Remove account from pool.
        
        Args:
            account_id: Account ID to remove
        
        Returns:
            True if removed successfully
        """
        if account_id not in self.accounts:
            logger.warning(f"Account {account_id} not found in pool")
            return False
        
        # Remove from Redis
        self.redis.delete(f'account:{account_id}')
        self.redis.srem('accounts:active', account_id)
        self.redis.srem('accounts:cooling', account_id)
        self.redis.srem('accounts:suspended', account_id)
        
        # Remove from memory
        del self.accounts[account_id]
        
        logger.info(f"Removed account {account_id} from pool")
        return True
    
    def get_account(self, strategy: str = 'weighted') -> AccountSession:
        """Get an available account from pool.
        
        Args:
            strategy: Selection strategy ('round_robin', 'least_loaded', 'weighted')
        
        Returns:
            AccountSession object
        
        Raises:
            NoAvailableAccountException: When no account is available
        """
        # Get active account IDs
        active_ids = self.redis.smembers('accounts:active')
        
        if not active_ids:
            raise NoAvailableAccountException("No active accounts in pool")
        
        # Load and filter available accounts
        candidates = []
        for acc_id in active_ids:
            session = self._load_account(acc_id)
            
            if session and session.is_available():
                candidates.append(session)
        
        if not candidates:
            raise NoAvailableAccountException(
                f"No available accounts (total: {len(active_ids)}, available: 0)"
            )
        
        # Select account by strategy
        if strategy == 'round_robin':
            selected = self._select_round_robin(candidates)
        elif strategy == 'least_loaded':
            selected = self._select_least_loaded(candidates)
        elif strategy == 'weighted':
            selected = self._select_weighted(candidates)
        else:
            selected = random.choice(candidates)
        
        logger.debug(
            f"Selected account {selected.account_id} "
            f"(health: {selected.health_score:.2f}, "
            f"quota: {selected.daily_request_count}/{selected.daily_quota})"
        )
        
        return selected
    
    def release_account(self, account_id: str, success: bool, response_time: float = 0):
        """Release account after use and update metrics.
        
        Args:
            account_id: Account ID to release
            success: Whether the request was successful
            response_time: Response time in seconds
        """
        session = self._load_account(account_id)
        if not session:
            logger.warning(f"Account {account_id} not found for release")
            return
        
        # Update metrics
        session.last_request_time = datetime.now()
        session.daily_request_count += 1
        session.total_requests += 1
        
        if success:
            session.success_requests += 1
            # Gradually increase health score
            session.health_score = min(1.0, session.health_score + 0.01)
        else:
            # Decrease health score on failure
            session.health_score = max(0.0, session.health_score - 0.05)
        
        # Update success rate
        session.update_success_rate()
        
        # Update average response time
        if response_time > 0:
            if session.avg_response_time == 0:
                session.avg_response_time = response_time
            else:
                # Exponential moving average
                alpha = 0.3
                session.avg_response_time = (
                    alpha * response_time + (1 - alpha) * session.avg_response_time
                )
        
        # Check if health score is too low
        if session.health_score < 0.3:
            logger.warning(
                f"Account {account_id} health score too low ({session.health_score:.2f}), "
                "triggering cooling"
            )
            self.trigger_cooling(account_id, duration_hours=2)
        
        # Update timestamp
        session.updated_at = datetime.now()
        
        # Save to Redis
        self._save_account(session)
        
        logger.debug(
            f"Released account {account_id} "
            f"(success: {success}, health: {session.health_score:.2f})"
        )
    
    def trigger_cooling(self, account_id: str, duration_hours: int = 2):
        """Trigger account cooling period.
        
        Args:
            account_id: Account ID to cool
            duration_hours: Cooling duration in hours
        """
        session = self._load_account(account_id)
        if not session:
            return
        
        session.status = AccountStatus.COOLING.value
        session.cooling_until = datetime.now() + timedelta(hours=duration_hours)
        session.updated_at = datetime.now()
        
        # Move to cooling set
        self.redis.srem('accounts:active', account_id)
        self.redis.sadd('accounts:cooling', account_id)
        
        self._save_account(session)
        
        logger.warning(
            f"Account {account_id} cooling until {session.cooling_until} "
            f"({duration_hours}h)"
        )
    
    def resume_cooling_accounts(self):
        """Resume accounts that have finished cooling."""
        cooling_ids = self.redis.smembers('accounts:cooling')
        
        resumed_count = 0
        for acc_id in cooling_ids:
            session = self._load_account(acc_id)
            if not session:
                continue
            
            # Check if cooling period is over
            if session.cooling_until and datetime.now() >= session.cooling_until:
                session.status = AccountStatus.ACTIVE.value
                session.cooling_until = None
                session.updated_at = datetime.now()
                
                # Move back to active set
                self.redis.srem('accounts:cooling', acc_id)
                self.redis.sadd('accounts:active', acc_id)
                
                self._save_account(session)
                
                logger.info(f"Resumed account {acc_id} from cooling")
                resumed_count += 1
        
        if resumed_count > 0:
            logger.info(f"Resumed {resumed_count} accounts from cooling")
    
    def suspend_account(self, account_id: str, reason: str = ''):
        """Suspend an account.
        
        Args:
            account_id: Account ID to suspend
            reason: Suspension reason
        """
        session = self._load_account(account_id)
        if not session:
            return
        
        session.status = AccountStatus.SUSPENDED.value
        session.updated_at = datetime.now()
        
        # Move to suspended set
        self.redis.srem('accounts:active', account_id)
        self.redis.srem('accounts:cooling', account_id)
        self.redis.sadd('accounts:suspended', account_id)
        
        self._save_account(session)
        
        logger.warning(f"Suspended account {account_id}: {reason}")
    
    def reset_daily_counts(self):
        """Reset daily request counts for all accounts (called at midnight)."""
        all_ids = self.redis.smembers('accounts:active')
        all_ids.update(self.redis.smembers('accounts:cooling'))
        # suspended 账号也要清零，否则 resume 回 active 后可能因陈旧计数直接不可用
        all_ids.update(self.redis.smembers('accounts:suspended'))
        
        reset_count = 0
        for acc_id in all_ids:
            session = self._load_account(acc_id)
            if not session:
                continue
            
            session.daily_request_count = 0
            session.updated_at = datetime.now()
            self._save_account(session)
            reset_count += 1
        
        logger.info(f"Reset daily counts for {reset_count} accounts")
    
    def get_pool_stats(self) -> dict:
        """Get pool statistics.
        
        Returns:
            Dictionary with pool stats
        """
        active_count = len(self.redis.smembers('accounts:active'))
        cooling_count = len(self.redis.smembers('accounts:cooling'))
        suspended_count = len(self.redis.smembers('accounts:suspended'))
        
        # Calculate average health score
        total_health = 0
        count = 0
        for acc_id in self.redis.smembers('accounts:active'):
            session = self._load_account(acc_id)
            if session:
                total_health += session.health_score
                count += 1
        
        avg_health = total_health / count if count > 0 else 0
        
        return {
            'total_accounts': active_count + cooling_count + suspended_count,
            'active_accounts': active_count,
            'cooling_accounts': cooling_count,
            'suspended_accounts': suspended_count,
            'avg_health_score': round(avg_health, 2),
            'timestamp': datetime.now().isoformat()
        }
    
    def _select_round_robin(self, candidates: List[AccountSession]) -> AccountSession:
        """Select account using round-robin strategy."""
        # Simple round-robin based on last request time
        return min(candidates, key=lambda x: x.last_request_time or datetime.min)
    
    def _select_least_loaded(self, candidates: List[AccountSession]) -> AccountSession:
        """Select account with least load."""
        return min(candidates, key=lambda x: x.daily_request_count)
    
    def _select_weighted(self, candidates: List[AccountSession]) -> AccountSession:
        """Select account using weighted random based on health and quota."""
        weights = [acc.get_weight() for acc in candidates]
        total_weight = sum(weights)
        
        if total_weight == 0:
            return random.choice(candidates)
        
        # Weighted random selection
        return random.choices(candidates, weights=weights)[0]
    
    def _load_account(self, account_id: str) -> Optional[AccountSession]:
        """Load account from Redis.
        
        Args:
            account_id: Account ID
        
        Returns:
            AccountSession or None if not found
        """
        # Check memory cache first
        if account_id in self.accounts:
            return self.accounts[account_id]
        
        # Load from Redis
        data = self.redis.hgetall(f'account:{account_id}')
        if not data:
            return None
        
        # Convert string values back to proper types
        data['daily_request_count'] = int(data.get('daily_request_count', 0))
        data['daily_quota'] = int(data.get('daily_quota', 200))
        data['waf_block_count'] = int(data.get('waf_block_count', 0))
        data['rate_limit_count'] = int(data.get('rate_limit_count', 0))
        data['total_requests'] = int(data.get('total_requests', 0))
        data['success_requests'] = int(data.get('success_requests', 0))
        data['health_score'] = float(data.get('health_score', 1.0))
        data['success_rate'] = float(data.get('success_rate', 1.0))
        data['avg_response_time'] = float(data.get('avg_response_time', 0.0))
        
        # Parse cookies from JSON string
        import json
        data['cookies'] = json.loads(data.get('cookies', '{}'))
        
        session = AccountSession.from_dict(data)
        
        # Cache in memory
        self.accounts[account_id] = session
        
        return session
    
    def _save_account(self, session: AccountSession):
        """Save account to Redis.
        
        Args:
            session: AccountSession to save
        """
        data = session.to_dict()
        
        # Convert cookies to JSON string
        import json
        data['cookies'] = json.dumps(data['cookies'])
        
        self.redis.hset(f'account:{session.account_id}', mapping=data)
        
        # Update memory cache
        self.accounts[session.account_id] = session
    
    def _load_all_accounts(self):
        """Load all accounts from Redis on startup."""
        all_sets = ['accounts:active', 'accounts:cooling', 'accounts:suspended']
        
        for set_name in all_sets:
            account_ids = self.redis.smembers(set_name)
            for acc_id in account_ids:
                session = self._load_account(acc_id)
                if session:
                    self.accounts[acc_id] = session
        
        logger.info(f"Loaded {len(self.accounts)} accounts from Redis")
