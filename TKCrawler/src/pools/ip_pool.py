"""IP Pool Manager for managing proxy nodes."""

import uuid
import time
from typing import Dict, List, Optional
from datetime import datetime, timedelta
import redis

from .models import ProxyNode, ProxyStatus
from .exceptions import (
    NoAvailableProxyException,
    ProxyFailedException,
    InvalidProxyException
)
from ..common.logger import get_logger
from ..common.config import config

logger = get_logger(__name__)


class IPPoolManager:
    """Manages proxy IP pool with health checking."""
    
    def __init__(self, redis_client: redis.Redis = None):
        """Initialize IP pool manager.
        
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
        self.proxies: Dict[str, ProxyNode] = {}
        
        # Load existing proxies from Redis
        self._load_all_proxies()
        
        logger.info(f"IPPoolManager initialized with {len(self.proxies)} proxies")
    
    def add_proxy(
        self,
        proxy_url: str,
        proxy_type: str = 'http',
        region: str = 'US',
        max_accounts: int = 3
    ) -> str:
        """Add a new proxy to the pool.
        
        Args:
            proxy_url: Proxy URL (http://user:pass@host:port)
            proxy_type: Proxy type (http/socks5)
            region: Proxy region (US/EU/APAC)
            max_accounts: Maximum accounts per proxy
        
        Returns:
            proxy_id: Generated proxy ID
        
        Raises:
            InvalidProxyException: When proxy fails validation
        """
        # Validate proxy URL format
        if not self._validate_proxy_url(proxy_url):
            raise InvalidProxyException(proxy_url)
        
        proxy_id = f"proxy_{uuid.uuid4().hex[:8]}"
        
        node = ProxyNode(
            proxy_id=proxy_id,
            proxy_url=proxy_url,
            proxy_type=proxy_type,
            region=region,
            max_accounts=max_accounts,
            status=ProxyStatus.TESTING.value
        )
        
        # Health check before adding
        logger.info(f"Testing proxy {proxy_id}...")
        if self._health_check_proxy(node):
            node.status = ProxyStatus.ACTIVE.value
            
            # Save to Redis
            self._save_proxy(node)
            
            # Add to active set
            self.redis.sadd('proxies:active', proxy_id)
            
            # Cache in memory
            self.proxies[proxy_id] = node
            
            logger.info(
                f"Added proxy {proxy_id} to pool "
                f"(region: {region}, latency: {node.avg_latency:.0f}ms)"
            )
            return proxy_id
        else:
            # 脱敏后再记日志，避免 user:pass 凭据泄露
            import re as _re
            masked = _re.sub(r'//[^@/]+@', '//***@', proxy_url)
            logger.error(f"Proxy {masked} failed health check")
            raise ProxyFailedException(proxy_id, "Failed initial health check")
    
    def remove_proxy(self, proxy_id: str) -> bool:
        """Remove proxy from pool.
        
        Args:
            proxy_id: Proxy ID to remove
        
        Returns:
            True if removed successfully
        """
        if proxy_id not in self.proxies:
            logger.warning(f"Proxy {proxy_id} not found in pool")
            return False
        
        # Remove from Redis
        self.redis.delete(f'proxy:{proxy_id}')
        self.redis.srem('proxies:active', proxy_id)
        self.redis.srem('proxies:failed', proxy_id)
        
        # Remove from memory
        del self.proxies[proxy_id]
        
        logger.info(f"Removed proxy {proxy_id} from pool")
        return True
    
    def get_proxy(self, account_id: str = None) -> ProxyNode:
        """Get proxy for account (with binding).
        
        Args:
            account_id: Account ID (for long-term binding)
        
        Returns:
            ProxyNode object
        
        Raises:
            NoAvailableProxyException: When no proxy is available
        """
        if account_id:
            # Check if account already has a bound proxy
            bound_proxy_id = self.get_bound_proxy_id(account_id)
            
            if bound_proxy_id:
                proxy = self._load_proxy(bound_proxy_id)
                if proxy and proxy.is_available():
                    return proxy
                else:
                    logger.warning(
                        f"Bound proxy {bound_proxy_id} not available for account {account_id}, "
                        "assigning new proxy"
                    )
            
            # Assign new proxy
            return self._assign_proxy_to_account(account_id)
        else:
            # Get any available proxy
            active_ids = self.redis.smembers('proxies:active')
            
            for proxy_id in active_ids:
                proxy = self._load_proxy(proxy_id)
                if proxy and proxy.is_available():
                    return proxy
            
            raise NoAvailableProxyException()
    
    def get_bound_proxy_id(self, account_id: str) -> Optional[str]:
        """Get bound proxy ID for account.
        
        Args:
            account_id: Account ID
        
        Returns:
            Proxy ID or None
        """
        return self.redis.get(f'binding:account:{account_id}')
    
    def bind_proxy_to_account(self, proxy_id: str, account_id: str) -> bool:
        """Bind proxy to account (long-term binding).
        
        Args:
            proxy_id: Proxy ID
            account_id: Account ID
        
        Returns:
            True if bound successfully
        """
        proxy = self._load_proxy(proxy_id)
        if not proxy:
            logger.error(f"Proxy {proxy_id} not found")
            return False
        
        # Check if proxy can accept more accounts
        if len(proxy.bound_accounts) >= proxy.max_accounts:
            logger.warning(
                f"Proxy {proxy_id} already has {len(proxy.bound_accounts)} accounts "
                f"(max: {proxy.max_accounts})"
            )
            return False
        
        # Add to bound accounts
        if account_id not in proxy.bound_accounts:
            proxy.bound_accounts.append(account_id)
            proxy.updated_at = datetime.now()
            self._save_proxy(proxy)
        
        # Create binding in Redis
        self.redis.set(f'binding:account:{account_id}', proxy_id)
        
        logger.info(f"Bound account {account_id} to proxy {proxy_id}")
        return True
    
    def unbind_proxy_from_account(self, account_id: str) -> bool:
        """Unbind proxy from account.
        
        Args:
            account_id: Account ID
        
        Returns:
            True if unbound successfully
        """
        proxy_id = self.get_bound_proxy_id(account_id)
        if not proxy_id:
            return False
        
        proxy = self._load_proxy(proxy_id)
        if proxy and account_id in proxy.bound_accounts:
            proxy.bound_accounts.remove(account_id)
            proxy.updated_at = datetime.now()
            self._save_proxy(proxy)
        
        # Remove binding
        self.redis.delete(f'binding:account:{account_id}')
        
        logger.info(f"Unbound account {account_id} from proxy {proxy_id}")
        return True
    
    def health_check_all(self) -> List[dict]:
        """Health check all active proxies.
        
        Returns:
            List of check results
        """
        active_ids = self.redis.smembers('proxies:active')
        
        results = []
        failed_count = 0
        
        for proxy_id in active_ids:
            proxy = self._load_proxy(proxy_id)
            if not proxy:
                continue
            
            is_healthy = self._health_check_proxy(proxy)
            
            if is_healthy:
                proxy.status = ProxyStatus.ACTIVE.value
                self._save_proxy(proxy)
            else:
                proxy.status = ProxyStatus.FAILED.value
                proxy.updated_at = datetime.now()
                
                # Move to failed set
                self.redis.srem('proxies:active', proxy_id)
                self.redis.sadd('proxies:failed', proxy_id)
                
                self._save_proxy(proxy)
                
                logger.warning(f"Proxy {proxy_id} failed health check")
                failed_count += 1
            
            results.append({
                'proxy_id': proxy_id,
                'healthy': is_healthy,
                'latency': proxy.avg_latency,
                'success_rate': proxy.success_rate
            })
        
        logger.info(
            f"Health check completed: {len(results)} checked, {failed_count} failed"
        )
        
        return results
    
    def rotate_proxy(self, account_id: str) -> ProxyNode:
        """Rotate proxy for account (unbind old, assign new).
        
        Args:
            account_id: Account ID
        
        Returns:
            New ProxyNode
        """
        # Unbind current proxy
        self.unbind_proxy_from_account(account_id)
        
        # Assign new proxy
        return self._assign_proxy_to_account(account_id)
    
    def get_pool_stats(self) -> dict:
        """Get pool statistics.
        
        Returns:
            Dictionary with pool stats
        """
        active_count = len(self.redis.smembers('proxies:active'))
        failed_count = len(self.redis.smembers('proxies:failed'))
        
        # Calculate average latency
        total_latency = 0
        count = 0
        for proxy_id in self.redis.smembers('proxies:active'):
            proxy = self._load_proxy(proxy_id)
            if proxy:
                total_latency += proxy.avg_latency
                count += 1
        
        avg_latency = total_latency / count if count > 0 else 0
        
        return {
            'total_proxies': active_count + failed_count,
            'active_proxies': active_count,
            'failed_proxies': failed_count,
            'avg_latency_ms': round(avg_latency, 0),
            'timestamp': datetime.now().isoformat()
        }
    
    def _assign_proxy_to_account(self, account_id: str) -> ProxyNode:
        """Assign proxy to account (select least loaded).
        
        Args:
            account_id: Account ID
        
        Returns:
            Assigned ProxyNode
        
        Raises:
            NoAvailableProxyException: When no proxy is available
        """
        active_ids = self.redis.smembers('proxies:active')
        
        if not active_ids:
            raise NoAvailableProxyException()
        
        # Find proxy with least accounts bound
        min_load = float('inf')
        selected_proxy = None
        
        for proxy_id in active_ids:
            proxy = self._load_proxy(proxy_id)
            if not proxy or not proxy.is_available():
                continue
            
            load = len(proxy.bound_accounts)
            
            if load < min_load:
                min_load = load
                selected_proxy = proxy
        
        if not selected_proxy:
            raise NoAvailableProxyException()
        
        # Bind proxy to account
        self.bind_proxy_to_account(selected_proxy.proxy_id, account_id)
        
        logger.info(
            f"Assigned proxy {selected_proxy.proxy_id} to account {account_id} "
            f"(load: {len(selected_proxy.bound_accounts)}/{selected_proxy.max_accounts})"
        )
        
        return selected_proxy
    
    def _health_check_proxy(self, proxy: ProxyNode) -> bool:
        """Health check a single proxy.
        
        Args:
            proxy: ProxyNode to check
        
        Returns:
            True if healthy
        """
        try:
            # Import here to avoid circular dependency
            from ..anti_detection.curl_client import CurlCffiClient
            
            client = CurlCffiClient()
            client.session.proxies = {
                'http': proxy.proxy_url,
                'https': proxy.proxy_url
            }
            
            start_time = time.time()
            response = client.get('https://www.tiktok.com', timeout=10)
            latency = (time.time() - start_time) * 1000
            
            proxy.total_checks += 1
            
            if response.status_code == 200:
                proxy.success_checks += 1
                proxy.avg_latency = latency
                proxy.last_check_time = datetime.now()
                proxy.update_success_rate()
                
                logger.debug(f"Proxy {proxy.proxy_id} health check passed ({latency:.0f}ms)")
                return True
            else:
                logger.warning(
                    f"Proxy {proxy.proxy_id} returned status {response.status_code}"
                )
                proxy.update_success_rate()
                return False
        
        except Exception as e:
            logger.error(f"Proxy {proxy.proxy_id} health check failed: {e}")
            proxy.total_checks += 1
            proxy.update_success_rate()
            return False
    
    def _validate_proxy_url(self, proxy_url: str) -> bool:
        """Validate proxy URL format.
        
        Args:
            proxy_url: Proxy URL to validate
        
        Returns:
            True if valid
        """
        # Basic validation
        if not proxy_url:
            return False
        
        # Check format: http://user:pass@host:port or http://host:port
        import re
        pattern = r'^(http|https|socks5)://([^:]+:[^@]+@)?[\w\.\-]+(:\d+)?$'
        return bool(re.match(pattern, proxy_url))
    
    def _load_proxy(self, proxy_id: str) -> Optional[ProxyNode]:
        """Load proxy from Redis.
        
        Args:
            proxy_id: Proxy ID
        
        Returns:
            ProxyNode or None if not found
        """
        # Check memory cache first
        if proxy_id in self.proxies:
            return self.proxies[proxy_id]
        
        # Load from Redis
        data = self.redis.hgetall(f'proxy:{proxy_id}')
        if not data:
            return None
        
        # Convert string values back to proper types
        data['max_accounts'] = int(data.get('max_accounts', 3))
        data['total_checks'] = int(data.get('total_checks', 0))
        data['success_checks'] = int(data.get('success_checks', 0))
        data['bandwidth_limit'] = int(data.get('bandwidth_limit', 0))
        data['daily_usage'] = int(data.get('daily_usage', 0))
        data['health_score'] = float(data.get('health_score', 1.0))
        data['success_rate'] = float(data.get('success_rate', 1.0))
        data['avg_latency'] = float(data.get('avg_latency', 0.0))
        
        # Parse bound_accounts from JSON string
        import json
        data['bound_accounts'] = json.loads(data.get('bound_accounts', '[]'))
        
        proxy = ProxyNode.from_dict(data)
        
        # Cache in memory
        self.proxies[proxy_id] = proxy
        
        return proxy
    
    def _save_proxy(self, proxy: ProxyNode):
        """Save proxy to Redis.
        
        Args:
            proxy: ProxyNode to save
        """
        data = proxy.to_dict()
        
        # Convert bound_accounts to JSON string
        import json
        data['bound_accounts'] = json.dumps(data['bound_accounts'])
        
        self.redis.hset(f'proxy:{proxy.proxy_id}', mapping=data)
        
        # Update memory cache
        self.proxies[proxy.proxy_id] = proxy
    
    def _load_all_proxies(self):
        """Load all proxies from Redis on startup."""
        all_sets = ['proxies:active', 'proxies:failed']
        
        for set_name in all_sets:
            proxy_ids = self.redis.smembers(set_name)
            for proxy_id in proxy_ids:
                proxy = self._load_proxy(proxy_id)
                if proxy:
                    self.proxies[proxy_id] = proxy
        
        logger.info(f"Loaded {len(self.proxies)} proxies from Redis")
