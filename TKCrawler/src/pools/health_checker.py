"""Health Checker for accounts and proxies with auto-recovery."""

import asyncio
import time
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
import redis
import json

from curl_cffi import requests
from curl_cffi.requests import AsyncSession

from .models import AccountSession, ProxyNode, AccountStatus, ProxyStatus
from .exceptions import NoAvailableAccountException, NoAvailableProxyException
from ..common.logger import get_logger
from ..common.config import config

logger = get_logger(__name__)


class HealthChecker:
    """Health checker for accounts and proxies with auto-recovery."""
    
    # 健康检查配置
    CHECK_URL = 'https://www.douyin.com/'
    ACCOUNT_TIMEOUT = 10  # 账号检查超时(秒)
    PROXY_TIMEOUT = 5     # 代理检查超时(秒)
    PROXY_LATENCY_THRESHOLD = 3.0  # 代理延迟阈值(秒)
    PROXY_UNHEALTHY_THRESHOLD = 5.0  # 代理不健康阈值(秒)
    
    # 自动恢复配置
    ACCOUNT_FAIL_THRESHOLD = 3  # 账号失败阈值
    MIN_HEALTHY_ACCOUNTS = 5    # 最小健康账号数
    
    # 并发控制
    MAX_CONCURRENT_CHECKS = 10
    
    # 历史保留天数
    HISTORY_RETENTION_DAYS = 7
    
    def __init__(
        self,
        cookie_pool,
        ip_pool,
        redis_client: redis.Redis = None
    ):
        """Initialize health checker.
        
        Args:
            cookie_pool: CookiePoolManager instance
            ip_pool: IPPoolManager instance
            redis_client: Redis client for logging
        """
        self.cookie_pool = cookie_pool
        self.ip_pool = ip_pool
        
        if redis_client is None:
            redis_client = redis.Redis(
                host=config.REDIS_HOST,
                port=config.REDIS_PORT,
                password=config.REDIS_PASSWORD,
                db=config.REDIS_DB,
                decode_responses=True
            )
        
        self.redis = redis_client
        
        logger.info("HealthChecker initialized")
    
    async def check_account(self, account_id: str) -> Tuple[bool, float]:
        """Check if an account is healthy.
        
        Args:
            account_id: Account ID to check
        
        Returns:
            Tuple of (is_healthy, response_time)
        """
        session = self.cookie_pool.accounts.get(account_id)
        if not session:
            logger.warning(f"Account {account_id} not found")
            return False, 0.0
        
        try:
            # 获取绑定的代理
            proxy = self.ip_pool.proxies.get(session.proxy_id)
            if not proxy:
                logger.warning(f"Proxy {session.proxy_id} not found for account {account_id}")
                return False, 0.0
            
            # 构建请求头
            headers = {
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'zh-CN,zh;q=0.9',
                'Cookie': '; '.join([f'{k}={v}' for k, v in session.cookies.items()])
            }
            
            # 发送请求
            start_time = time.time()
            
            async with AsyncSession() as client:
                response = await client.get(
                    self.CHECK_URL,
                    headers=headers,
                    proxies={
                        'http': proxy.proxy_url,
                        'https': proxy.proxy_url
                    },
                    timeout=self.ACCOUNT_TIMEOUT,
                    allow_redirects=True
                )
                
                response_time = time.time() - start_time
                
                # 检查响应状态
                is_healthy = response.status_code == 200
                
                if is_healthy:
                    logger.debug(
                        f"Account {account_id} health check passed "
                        f"(response_time: {response_time:.2f}s)"
                    )
                else:
                    logger.warning(
                        f"Account {account_id} health check failed "
                        f"(status: {response.status_code})"
                    )
                
                return is_healthy, response_time
        
        except asyncio.TimeoutError:
            logger.warning(f"Account {account_id} health check timeout")
            return False, self.ACCOUNT_TIMEOUT
        
        except Exception as e:
            logger.error(f"Account {account_id} health check error: {e}")
            return False, 0.0
    
    async def check_proxy(self, proxy: ProxyNode) -> Tuple[bool, float]:
        """Check if a proxy is healthy.
        
        Args:
            proxy: ProxyNode to check
        
        Returns:
            Tuple of (is_healthy, latency)
        """
        try:
            start_time = time.time()
            
            async with AsyncSession() as client:
                response = await client.get(
                    self.CHECK_URL,
                    proxies={
                        'http': proxy.proxy_url,
                        'https': proxy.proxy_url
                    },
                    timeout=self.PROXY_TIMEOUT,
                    allow_redirects=True
                )
                
                latency = time.time() - start_time
                
                # 检查响应和延迟
                is_healthy = (
                    response.status_code == 200 and 
                    latency < self.PROXY_LATENCY_THRESHOLD
                )
                
                if is_healthy:
                    logger.debug(
                        f"Proxy {proxy.proxy_id} health check passed "
                        f"(latency: {latency*1000:.0f}ms)"
                    )
                else:
                    logger.warning(
                        f"Proxy {proxy.proxy_id} health check failed "
                        f"(status: {response.status_code}, latency: {latency:.2f}s)"
                    )
                
                return is_healthy, latency
        
        except asyncio.TimeoutError:
            logger.warning(f"Proxy {proxy.proxy_id} health check timeout")
            return False, self.PROXY_TIMEOUT
        
        except Exception as e:
            logger.error(f"Proxy {proxy.proxy_id} health check error: {e}")
            return False, 0.0
    
    async def check_all(self) -> Dict:
        """Check all accounts and proxies concurrently.
        
        Returns:
            Health report dictionary
        """
        logger.info("Starting comprehensive health check...")
        
        start_time = time.time()
        
        # 并发检查所有账号
        account_results = await self._check_all_accounts()
        
        # 并发检查所有代理
        proxy_results = await self._check_all_proxies()
        
        # 生成报告
        report = self._generate_report(account_results, proxy_results)
        report['check_duration'] = time.time() - start_time
        report['timestamp'] = datetime.now().isoformat()
        
        # 保存到 Redis
        self._save_health_report(report)
        
        logger.info(
            f"Health check completed in {report['check_duration']:.2f}s - "
            f"Accounts: {report['accounts']['healthy']}/{report['accounts']['total']}, "
            f"Proxies: {report['proxies']['healthy']}/{report['proxies']['total']}"
        )
        
        return report
    
    async def _check_all_accounts(self) -> List[Dict]:
        """Check all accounts with concurrency control.
        
        Returns:
            List of account check results
        """
        results = []
        
        # 获取所有账号ID
        account_ids = list(self.cookie_pool.accounts.keys())
        
        if not account_ids:
            logger.warning("No accounts to check")
            return results
        
        # 分批并发检查
        for i in range(0, len(account_ids), self.MAX_CONCURRENT_CHECKS):
            batch = account_ids[i:i + self.MAX_CONCURRENT_CHECKS]
            
            tasks = [self.check_account(acc_id) for acc_id in batch]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # 处理结果
            for acc_id, result in zip(batch, batch_results):
                session = self.cookie_pool.accounts.get(acc_id)
                
                if isinstance(result, Exception):
                    logger.error(f"Account {acc_id} check exception: {result}")
                    is_healthy, response_time = False, 0.0
                else:
                    is_healthy, response_time = result
                
                results.append({
                    'account_id': acc_id,
                    'username': session.username if session else 'unknown',
                    'healthy': is_healthy,
                    'response_time': response_time,
                    'status': session.status if session else 'unknown',
                    'health_score': session.health_score if session else 0.0,
                    'daily_usage': f"{session.daily_request_count}/{session.daily_quota}" if session else 'N/A',
                    'timestamp': datetime.now().isoformat()
                })
        
        return results
    
    async def _check_all_proxies(self) -> List[Dict]:
        """Check all proxies with concurrency control.
        
        Returns:
            List of proxy check results
        """
        results = []
        
        # 获取所有代理
        proxies = list(self.ip_pool.proxies.values())
        
        if not proxies:
            logger.warning("No proxies to check")
            return results
        
        # 分批并发检查
        for i in range(0, len(proxies), self.MAX_CONCURRENT_CHECKS):
            batch = proxies[i:i + self.MAX_CONCURRENT_CHECKS]
            
            tasks = [self.check_proxy(proxy) for proxy in batch]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # 处理结果
            for proxy, result in zip(batch, batch_results):
                if isinstance(result, Exception):
                    logger.error(f"Proxy {proxy.proxy_id} check exception: {result}")
                    is_healthy, latency = False, 0.0
                else:
                    is_healthy, latency = result
                
                results.append({
                    'proxy_id': proxy.proxy_id,
                    'region': proxy.region,
                    'healthy': is_healthy,
                    'latency': latency,
                    'latency_ms': int(latency * 1000),
                    'status': proxy.status,
                    'health_score': proxy.health_score,
                    'bound_accounts': len(proxy.bound_accounts),
                    'timestamp': datetime.now().isoformat()
                })
        
        return results
    
    def _generate_report(
        self,
        account_results: List[Dict],
        proxy_results: List[Dict]
    ) -> Dict:
        """Generate health check report.
        
        Args:
            account_results: Account check results
            proxy_results: Proxy check results
        
        Returns:
            Health report dictionary
        """
        # 账号统计
        healthy_accounts = sum(1 for r in account_results if r['healthy'])
        total_accounts = len(account_results)
        
        # 代理统计
        healthy_proxies = sum(1 for r in proxy_results if r['healthy'])
        total_proxies = len(proxy_results)
        
        return {
            'accounts': {
                'total': total_accounts,
                'healthy': healthy_accounts,
                'unhealthy': total_accounts - healthy_accounts,
                'health_rate': healthy_accounts / total_accounts if total_accounts > 0 else 0.0,
                'details': account_results
            },
            'proxies': {
                'total': total_proxies,
                'healthy': healthy_proxies,
                'unhealthy': total_proxies - healthy_proxies,
                'health_rate': healthy_proxies / total_proxies if total_proxies > 0 else 0.0,
                'avg_latency_ms': int(
                    sum(r['latency_ms'] for r in proxy_results if r['healthy']) / healthy_proxies
                ) if healthy_proxies > 0 else 0,
                'details': proxy_results
            }
        }
    
    async def auto_recover(self) -> Dict:
        """Automatic recovery for failed accounts and proxies.
        
        Returns:
            Recovery report
        """
        logger.info("Starting auto-recovery process...")
        
        recovery_actions = {
            'accounts_suspended': [],
            'proxies_marked_unhealthy': [],
            'alerts_triggered': []
        }
        
        # 检查账号失败次数
        for account_id, session in self.cookie_pool.accounts.items():
            if session.status == AccountStatus.ACTIVE.value:
                # 如果连续失败次数超过阈值，暂停账号
                if session.waf_block_count >= self.ACCOUNT_FAIL_THRESHOLD:
                    logger.warning(
                        f"Account {account_id} exceeded fail threshold "
                        f"({session.waf_block_count} failures), suspending..."
                    )
                    
                    self.cookie_pool.suspend_account(
                        account_id,
                        reason='exceeded_fail_threshold'
                    )
                    
                    recovery_actions['accounts_suspended'].append({
                        'account_id': account_id,
                        'username': session.username,
                        'fail_count': session.waf_block_count,
                        'reason': 'exceeded_fail_threshold'
                    })
        
        # 检查代理延迟
        for proxy_id, proxy in self.ip_pool.proxies.items():
            if proxy.status == ProxyStatus.ACTIVE.value:
                # 如果平均延迟超过阈值，标记为不健康
                if proxy.avg_latency > self.PROXY_UNHEALTHY_THRESHOLD:
                    logger.warning(
                        f"Proxy {proxy_id} latency too high "
                        f"({proxy.avg_latency:.2f}s), marking as unhealthy..."
                    )
                    
                    proxy.status = ProxyStatus.FAILED.value
                    proxy.health_score = 0.0
                    proxy.updated_at = datetime.now()
                    
                    # 更新到 Redis
                    self.ip_pool._save_proxy(proxy)
                    
                    recovery_actions['proxies_marked_unhealthy'].append({
                        'proxy_id': proxy_id,
                        'region': proxy.region,
                        'avg_latency': proxy.avg_latency,
                        'reason': 'high_latency'
                    })
        
        # 检查健康账号数量
        healthy_accounts = sum(
            1 for session in self.cookie_pool.accounts.values()
            if session.status == AccountStatus.ACTIVE.value and session.is_available()
        )
        
        if healthy_accounts < self.MIN_HEALTHY_ACCOUNTS:
            alert_msg = (
                f"WARNING: Only {healthy_accounts} healthy accounts available "
                f"(minimum: {self.MIN_HEALTHY_ACCOUNTS})"
            )
            logger.error(alert_msg)
            
            recovery_actions['alerts_triggered'].append({
                'type': 'low_healthy_accounts',
                'message': alert_msg,
                'healthy_count': healthy_accounts,
                'threshold': self.MIN_HEALTHY_ACCOUNTS
            })
        
        # 保存恢复报告
        recovery_report = {
            'timestamp': datetime.now().isoformat(),
            'actions': recovery_actions,
            'total_actions': (
                len(recovery_actions['accounts_suspended']) +
                len(recovery_actions['proxies_marked_unhealthy']) +
                len(recovery_actions['alerts_triggered'])
            )
        }
        
        self._save_recovery_report(recovery_report)
        
        logger.info(
            f"Auto-recovery completed - "
            f"{len(recovery_actions['accounts_suspended'])} accounts suspended, "
            f"{len(recovery_actions['proxies_marked_unhealthy'])} proxies marked unhealthy, "
            f"{len(recovery_actions['alerts_triggered'])} alerts triggered"
        )
        
        return recovery_report
    
    def _save_health_report(self, report: Dict):
        """Save health report to Redis.
        
        Args:
            report: Health report dictionary
        """
        try:
            timestamp = int(datetime.now().timestamp())
            key = f'health_check:{timestamp}'
            
            self.redis.setex(
                key,
                self.HISTORY_RETENTION_DAYS * 86400,  # 7天过期
                json.dumps(report)
            )
            
            # 添加到时间序列索引
            self.redis.zadd('health_check:history', {key: timestamp})
            
            # 清理过期记录
            cutoff = timestamp - (self.HISTORY_RETENTION_DAYS * 86400)
            self.redis.zremrangebyscore('health_check:history', 0, cutoff)
            
            logger.debug(f"Health report saved to Redis: {key}")
        
        except Exception as e:
            logger.error(f"Failed to save health report: {e}")
    
    def _save_recovery_report(self, report: Dict):
        """Save recovery report to Redis.
        
        Args:
            report: Recovery report dictionary
        """
        try:
            timestamp = int(datetime.now().timestamp())
            key = f'recovery:{timestamp}'
            
            self.redis.setex(
                key,
                self.HISTORY_RETENTION_DAYS * 86400,
                json.dumps(report)
            )
            
            self.redis.zadd('recovery:history', {key: timestamp})
            
            # 清理过期记录
            cutoff = timestamp - (self.HISTORY_RETENTION_DAYS * 86400)
            self.redis.zremrangebyscore('recovery:history', 0, cutoff)
            
            logger.debug(f"Recovery report saved to Redis: {key}")
        
        except Exception as e:
            logger.error(f"Failed to save recovery report: {e}")
    
    def get_recent_reports(self, limit: int = 10) -> List[Dict]:
        """Get recent health check reports.
        
        Args:
            limit: Maximum number of reports to return
        
        Returns:
            List of health reports
        """
        try:
            # 获取最近的报告键
            keys = self.redis.zrevrange('health_check:history', 0, limit - 1)
            
            reports = []
            for key in keys:
                data = self.redis.get(key)
                if data:
                    reports.append(json.loads(data))
            
            return reports
        
        except Exception as e:
            logger.error(f"Failed to get recent reports: {e}")
            return []
    
    def get_health_summary(self) -> Dict:
        """Get overall health summary.
        
        Returns:
            Health summary dictionary
        """
        total_accounts = len(self.cookie_pool.accounts)
        active_accounts = sum(
            1 for s in self.cookie_pool.accounts.values()
            if s.status == AccountStatus.ACTIVE.value
        )
        available_accounts = sum(
            1 for s in self.cookie_pool.accounts.values()
            if s.is_available()
        )
        
        total_proxies = len(self.ip_pool.proxies)
        active_proxies = sum(
            1 for p in self.ip_pool.proxies.values()
            if p.status == ProxyStatus.ACTIVE.value
        )
        available_proxies = sum(
            1 for p in self.ip_pool.proxies.values()
            if p.is_available()
        )
        
        return {
            'accounts': {
                'total': total_accounts,
                'active': active_accounts,
                'available': available_accounts,
                'suspended': total_accounts - active_accounts
            },
            'proxies': {
                'total': total_proxies,
                'active': active_proxies,
                'available': available_proxies,
                'failed': total_proxies - active_proxies
            },
            'timestamp': datetime.now().isoformat()
        }
