"""Unit tests for HealthChecker."""

import pytest
import asyncio
from datetime import datetime, timedelta
from unittest.mock import Mock, AsyncMock, patch, MagicMock
import json

from src.pools.health_checker import HealthChecker
from src.pools.models import AccountSession, ProxyNode, AccountStatus, ProxyStatus
from src.pools.cookie_pool import CookiePoolManager
from src.pools.ip_pool import IPPoolManager


@pytest.fixture
def mock_redis():
    """Create a mock Redis client."""
    redis_mock = Mock()
    redis_mock.setex = Mock()
    redis_mock.zadd = Mock()
    redis_mock.zremrangebyscore = Mock()
    redis_mock.zrevrange = Mock(return_value=[])
    redis_mock.get = Mock(return_value=None)
    # CookiePoolManager/IPPoolManager 初始化时会调用 _load_all_accounts()，
    # 遍历 smembers 的返回值；必须返回可迭代对象
    redis_mock.smembers = Mock(return_value=set())
    redis_mock.hgetall = Mock(return_value={})
    redis_mock.hget = Mock(return_value=None)
    return redis_mock


@pytest.fixture
def mock_cookie_pool(mock_redis):
    """Create a mock CookiePoolManager."""
    pool = CookiePoolManager(redis_client=mock_redis)
    
    # 添加5个健康账号
    for i in range(5):
        account_id = f'acc_{i:08d}'
        pool.accounts[account_id] = AccountSession(
            account_id=account_id,
            username=f'user_{i}',
            cookies={'sessionid': f'session_{i}'},
            proxy_id=f'proxy_{i % 2:08d}',  # 2个代理轮换
            status=AccountStatus.ACTIVE.value,
            health_score=1.0,
            daily_request_count=50,
            daily_quota=200
        )
    
    # 添加2个失效账号
    for i in range(5, 7):
        account_id = f'acc_{i:08d}'
        pool.accounts[account_id] = AccountSession(
            account_id=account_id,
            username=f'user_{i}',
            cookies={'sessionid': f'session_{i}'},
            proxy_id=f'proxy_{i % 2:08d}',
            status=AccountStatus.ACTIVE.value,
            health_score=0.5,
            waf_block_count=3,  # 已达到失败阈值
            daily_request_count=180,
            daily_quota=200
        )
    
    pool.suspend_account = Mock()
    pool._save_account = Mock()
    
    return pool


@pytest.fixture
def mock_ip_pool(mock_redis):
    """Create a mock IPPoolManager."""
    pool = IPPoolManager(redis_client=mock_redis)
    
    # 添加2个健康代理
    for i in range(2):
        proxy_id = f'proxy_{i:08d}'
        pool.proxies[proxy_id] = ProxyNode(
            proxy_id=proxy_id,
            proxy_url=f'http://user:pass@proxy{i}.example.com:8080',
            region='US',
            status=ProxyStatus.ACTIVE.value,
            health_score=1.0,
            avg_latency=0.5,  # 500ms
            bound_accounts=[f'acc_{j:08d}' for j in range(i*3, (i+1)*3)]
        )
    
    # 添加1个高延迟代理
    pool.proxies['proxy_00000002'] = ProxyNode(
        proxy_id='proxy_00000002',
        proxy_url='http://user:pass@slow-proxy.example.com:8080',
        region='EU',
        status=ProxyStatus.ACTIVE.value,
        health_score=0.8,
        avg_latency=5.5,  # 5.5秒，超过阈值
        bound_accounts=[]
    )
    
    pool._save_proxy = Mock()
    
    return pool


@pytest.fixture
def health_checker(mock_cookie_pool, mock_ip_pool, mock_redis):
    """Create a HealthChecker instance."""
    return HealthChecker(
        cookie_pool=mock_cookie_pool,
        ip_pool=mock_ip_pool,
        redis_client=mock_redis
    )


class TestHealthChecker:
    """Test HealthChecker class."""
    
    @pytest.mark.asyncio
    async def test_check_account_healthy(self, health_checker):
        """Test checking a healthy account."""
        # Mock async HTTP response
        with patch('src.pools.health_checker.AsyncSession') as mock_session:
            mock_response = AsyncMock()
            mock_response.status_code = 200
            
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value.get = AsyncMock(return_value=mock_response)
            mock_session.return_value = mock_client
            
            is_healthy, response_time = await health_checker.check_account('acc_00000000')
            
            assert is_healthy is True
            assert response_time > 0
    
    @pytest.mark.asyncio
    async def test_check_account_failed(self, health_checker):
        """Test checking a failed account."""
        with patch('src.pools.health_checker.AsyncSession') as mock_session:
            mock_response = AsyncMock()
            mock_response.status_code = 403  # 被封禁
            
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value.get = AsyncMock(return_value=mock_response)
            mock_session.return_value = mock_client
            
            is_healthy, response_time = await health_checker.check_account('acc_00000000')
            
            assert is_healthy is False
    
    @pytest.mark.asyncio
    async def test_check_account_timeout(self, health_checker):
        """Test account check timeout."""
        with patch('src.pools.health_checker.AsyncSession') as mock_session:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value.get = AsyncMock(
                side_effect=asyncio.TimeoutError()
            )
            mock_session.return_value = mock_client
            
            is_healthy, response_time = await health_checker.check_account('acc_00000000')
            
            assert is_healthy is False
            assert response_time == health_checker.ACCOUNT_TIMEOUT
    
    @pytest.mark.asyncio
    async def test_check_proxy_healthy(self, health_checker):
        """Test checking a healthy proxy."""
        proxy = health_checker.ip_pool.proxies['proxy_00000000']
        
        with patch('src.pools.health_checker.AsyncSession') as mock_session:
            mock_response = AsyncMock()
            mock_response.status_code = 200
            
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value.get = AsyncMock(return_value=mock_response)
            mock_session.return_value = mock_client
            
            is_healthy, latency = await health_checker.check_proxy(proxy)
            
            assert is_healthy is True
            assert latency < health_checker.PROXY_LATENCY_THRESHOLD
    
    @pytest.mark.asyncio
    async def test_check_proxy_high_latency(self, health_checker):
        """Test checking a proxy with high latency."""
        proxy = health_checker.ip_pool.proxies['proxy_00000000']
        
        with patch('src.pools.health_checker.AsyncSession') as mock_session:
            mock_response = AsyncMock()
            mock_response.status_code = 200
            
            # 模拟5秒延迟
            async def slow_get(*args, **kwargs):
                await asyncio.sleep(0.1)  # 实际测试用较短时间
                return mock_response
            
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value.get = slow_get
            mock_session.return_value = mock_client
            
            # 临时降低阈值以加快测试
            original_threshold = health_checker.PROXY_LATENCY_THRESHOLD
            health_checker.PROXY_LATENCY_THRESHOLD = 0.05
            
            is_healthy, latency = await health_checker.check_proxy(proxy)
            
            assert is_healthy is False
            assert latency > 0.05
            
            # 恢复阈值
            health_checker.PROXY_LATENCY_THRESHOLD = original_threshold
    
    @pytest.mark.asyncio
    async def test_check_all_accounts(self, health_checker):
        """Test checking all accounts."""
        with patch('src.pools.health_checker.AsyncSession') as mock_session:
            mock_response = AsyncMock()
            mock_response.status_code = 200
            
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value.get = AsyncMock(return_value=mock_response)
            mock_session.return_value = mock_client
            
            results = await health_checker._check_all_accounts()
            
            assert len(results) == 7  # 5个健康 + 2个失效
            assert all('account_id' in r for r in results)
            assert all('healthy' in r for r in results)
            assert all('response_time' in r for r in results)
    
    @pytest.mark.asyncio
    async def test_check_all_proxies(self, health_checker):
        """Test checking all proxies."""
        with patch('src.pools.health_checker.AsyncSession') as mock_session:
            mock_response = AsyncMock()
            mock_response.status_code = 200
            
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value.get = AsyncMock(return_value=mock_response)
            mock_session.return_value = mock_client
            
            results = await health_checker._check_all_proxies()
            
            assert len(results) == 3  # 3个代理
            assert all('proxy_id' in r for r in results)
            assert all('healthy' in r for r in results)
            assert all('latency_ms' in r for r in results)
    
    @pytest.mark.asyncio
    async def test_check_all(self, health_checker):
        """Test comprehensive health check."""
        with patch('src.pools.health_checker.AsyncSession') as mock_session:
            mock_response = AsyncMock()
            mock_response.status_code = 200
            
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value.get = AsyncMock(return_value=mock_response)
            mock_session.return_value = mock_client
            
            report = await health_checker.check_all()
            
            assert 'accounts' in report
            assert 'proxies' in report
            assert 'timestamp' in report
            assert 'check_duration' in report
            
            assert report['accounts']['total'] == 7
            assert report['proxies']['total'] == 3
            
            # 验证保存到 Redis
            assert health_checker.redis.setex.called
            assert health_checker.redis.zadd.called
    
    @pytest.mark.asyncio
    async def test_auto_recover_suspend_accounts(self, health_checker):
        """Test auto-recovery suspends failed accounts."""
        recovery_report = await health_checker.auto_recover()
        
        assert 'accounts_suspended' in recovery_report['actions']
        assert len(recovery_report['actions']['accounts_suspended']) == 2  # 2个失效账号
        
        # 验证调用了暂停方法
        assert health_checker.cookie_pool.suspend_account.call_count == 2
    
    @pytest.mark.asyncio
    async def test_auto_recover_mark_unhealthy_proxies(self, health_checker):
        """Test auto-recovery marks unhealthy proxies."""
        recovery_report = await health_checker.auto_recover()
        
        assert 'proxies_marked_unhealthy' in recovery_report['actions']
        assert len(recovery_report['actions']['proxies_marked_unhealthy']) == 1  # 1个高延迟代理
        
        # 验证代理状态被更新
        slow_proxy = health_checker.ip_pool.proxies['proxy_00000002']
        assert slow_proxy.status == ProxyStatus.FAILED.value
        assert slow_proxy.health_score == 0.0
    
    @pytest.mark.asyncio
    async def test_auto_recover_trigger_alert(self, health_checker):
        """Test auto-recovery triggers alert when low healthy accounts."""
        # 暂停所有账号以触发告警
        for session in health_checker.cookie_pool.accounts.values():
            session.status = AccountStatus.SUSPENDED.value
        
        recovery_report = await health_checker.auto_recover()
        
        assert 'alerts_triggered' in recovery_report['actions']
        assert len(recovery_report['actions']['alerts_triggered']) > 0
        
        alert = recovery_report['actions']['alerts_triggered'][0]
        assert alert['type'] == 'low_healthy_accounts'
        assert alert['healthy_count'] < health_checker.MIN_HEALTHY_ACCOUNTS
    
    def test_generate_report(self, health_checker):
        """Test report generation."""
        account_results = [
            {'account_id': 'acc1', 'healthy': True, 'response_time': 0.5},
            {'account_id': 'acc2', 'healthy': True, 'response_time': 0.6},
            {'account_id': 'acc3', 'healthy': False, 'response_time': 0.0}
        ]
        
        proxy_results = [
            {'proxy_id': 'proxy1', 'healthy': True, 'latency': 0.5, 'latency_ms': 500},
            {'proxy_id': 'proxy2', 'healthy': False, 'latency': 5.0, 'latency_ms': 5000}
        ]
        
        report = health_checker._generate_report(account_results, proxy_results)
        
        assert report['accounts']['total'] == 3
        assert report['accounts']['healthy'] == 2
        assert report['accounts']['unhealthy'] == 1
        assert report['accounts']['health_rate'] == pytest.approx(2/3)
        
        assert report['proxies']['total'] == 2
        assert report['proxies']['healthy'] == 1
        assert report['proxies']['unhealthy'] == 1
        assert report['proxies']['avg_latency_ms'] == 500
    
    def test_get_health_summary(self, health_checker):
        """Test getting health summary."""
        summary = health_checker.get_health_summary()
        
        assert 'accounts' in summary
        assert 'proxies' in summary
        assert 'timestamp' in summary
        
        assert summary['accounts']['total'] == 7
        assert summary['accounts']['active'] == 7  # 所有账号都是 active 状态
        assert summary['accounts']['available'] > 0
        
        assert summary['proxies']['total'] == 3
        assert summary['proxies']['active'] == 3


class TestHealthCheckerIntegration:
    """Integration tests for HealthChecker."""
    
    @pytest.mark.asyncio
    async def test_full_workflow(self, health_checker):
        """Test full health check and recovery workflow."""
        # 模拟健康检查
        with patch('src.pools.health_checker.AsyncSession') as mock_session:
            mock_response = AsyncMock()
            mock_response.status_code = 200
            
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value.get = AsyncMock(return_value=mock_response)
            mock_session.return_value = mock_client
            
            # 1. 执行健康检查
            health_report = await health_checker.check_all()
            
            assert health_report['accounts']['total'] > 0
            assert health_report['proxies']['total'] > 0
            
            # 2. 执行自动恢复
            recovery_report = await health_checker.auto_recover()
            
            assert 'actions' in recovery_report
            assert recovery_report['total_actions'] > 0
            
            # 3. 获取健康摘要
            summary = health_checker.get_health_summary()
            
            assert summary['accounts']['available'] >= 0
            assert summary['proxies']['available'] >= 0


# 健康检查报告样例
SAMPLE_HEALTH_REPORT = {
    "accounts": {
        "total": 7,
        "healthy": 5,
        "unhealthy": 2,
        "health_rate": 0.714,
        "details": [
            {
                "account_id": "acc_00000000",
                "username": "user_0",
                "healthy": True,
                "response_time": 0.523,
                "status": "active",
                "health_score": 1.0,
                "daily_usage": "50/200",
                "timestamp": "2026-10-08T10:30:00"
            },
            {
                "account_id": "acc_00000005",
                "username": "user_5",
                "healthy": False,
                "response_time": 0.0,
                "status": "active",
                "health_score": 0.5,
                "daily_usage": "180/200",
                "timestamp": "2026-10-08T10:30:00"
            }
        ]
    },
    "proxies": {
        "total": 3,
        "healthy": 2,
        "unhealthy": 1,
        "health_rate": 0.667,
        "avg_latency_ms": 450,
        "details": [
            {
                "proxy_id": "proxy_00000000",
                "region": "US",
                "healthy": True,
                "latency": 0.45,
                "latency_ms": 450,
                "status": "active",
                "health_score": 1.0,
                "bound_accounts": 3,
                "timestamp": "2026-10-08T10:30:00"
            },
            {
                "proxy_id": "proxy_00000002",
                "region": "EU",
                "healthy": False,
                "latency": 5.5,
                "latency_ms": 5500,
                "status": "active",
                "health_score": 0.8,
                "bound_accounts": 0,
                "timestamp": "2026-10-08T10:30:00"
            }
        ]
    },
    "check_duration": 2.34,
    "timestamp": "2026-10-08T10:30:00"
}


if __name__ == '__main__':
    # 保存健康检查报告样例
    with open('sample_health_report.json', 'w', encoding='utf-8') as f:
        json.dump(SAMPLE_HEALTH_REPORT, f, indent=2, ensure_ascii=False)
    
    print("Sample health report saved to: sample_health_report.json")
    
    # 运行测试
    pytest.main([__file__, '-v', '--tb=short'])
