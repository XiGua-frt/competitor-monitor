"""Unit tests for pool scheduler."""

import pytest
import time
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from src.pools.scheduler import PoolScheduler, Task
from src.pools.models import AccountSession, ProxyNode, AccountStatus, ProxyStatus
from src.pools.exceptions import NoAvailableAccountException, NoAvailableProxyException


@pytest.fixture
def mock_redis():
    """Mock Redis client."""
    redis = Mock()
    redis.smembers.return_value = set()
    redis.sadd.return_value = 1
    redis.srem.return_value = 1
    redis.hset.return_value = 1
    redis.hgetall.return_value = {}
    return redis


@pytest.fixture
def mock_cookie_pool(mock_redis):
    """Mock cookie pool manager."""
    pool = Mock()
    pool.redis = mock_redis
    pool.accounts = {}
    
    # Mock _load_account to return from accounts dict
    def load_account(account_id):
        return pool.accounts.get(account_id)
    
    pool._load_account = Mock(side_effect=load_account)
    pool._save_account = Mock()
    pool.release_account = Mock()
    pool.resume_cooling_accounts = Mock(return_value=0)
    pool.get_pool_stats = Mock(return_value={
        'total': 0,
        'active': 0,
        'cooling': 0,
        'suspended': 0
    })
    
    return pool


@pytest.fixture
def mock_ip_pool(mock_redis):
    """Mock IP pool manager."""
    pool = Mock()
    pool.redis = mock_redis
    pool.proxies = {}
    
    # Mock get_proxy to return from proxies dict
    def get_proxy(proxy_id):
        return pool.proxies.get(proxy_id)
    
    pool.get_proxy = Mock(side_effect=get_proxy)
    pool._save_proxy = Mock()
    pool.get_pool_stats = Mock(return_value={
        'total': 0,
        'active': 0,
        'failed': 0
    })
    
    return pool


@pytest.fixture
def scheduler(mock_cookie_pool, mock_ip_pool):
    """Create scheduler instance."""
    return PoolScheduler(
        cookie_pool=mock_cookie_pool,
        ip_pool=mock_ip_pool,
        default_strategy='weighted'
    )


@pytest.fixture
def sample_accounts():
    """Create sample accounts."""
    return [
        AccountSession(
            account_id='acc_001',
            username='user1',
            cookies={'sessionid': 'token1'},
            proxy_id='proxy_001',
            status=AccountStatus.ACTIVE.value,
            health_score=1.0,
            daily_request_count=10,
            daily_quota=100
        ),
        AccountSession(
            account_id='acc_002',
            username='user2',
            cookies={'sessionid': 'token2'},
            proxy_id='proxy_002',
            status=AccountStatus.ACTIVE.value,
            health_score=0.9,
            daily_request_count=50,
            daily_quota=100
        ),
        AccountSession(
            account_id='acc_003',
            username='user3',
            cookies={'sessionid': 'token3'},
            proxy_id='proxy_003',
            status=AccountStatus.ACTIVE.value,
            health_score=0.8,
            daily_request_count=80,
            daily_quota=100
        )
    ]


@pytest.fixture
def sample_proxies():
    """Create sample proxies."""
    return [
        ProxyNode(
            proxy_id='proxy_001',
            proxy_url='http://proxy1.com:8080',
            status=ProxyStatus.ACTIVE.value,
            health_score=1.0,
            bound_accounts=['acc_001'],
            max_accounts=3
        ),
        ProxyNode(
            proxy_id='proxy_002',
            proxy_url='http://proxy2.com:8080',
            status=ProxyStatus.ACTIVE.value,
            health_score=0.9,
            bound_accounts=['acc_002'],
            max_accounts=3
        ),
        ProxyNode(
            proxy_id='proxy_003',
            proxy_url='http://proxy3.com:8080',
            status=ProxyStatus.ACTIVE.value,
            health_score=0.8,
            bound_accounts=['acc_003'],
            max_accounts=3
        )
    ]


class TestSchedulerInitialization:
    """Test scheduler initialization."""
    
    def test_init_with_valid_strategy(self, mock_cookie_pool, mock_ip_pool):
        """Test initialization with valid strategy."""
        scheduler = PoolScheduler(mock_cookie_pool, mock_ip_pool, 'weighted')
        assert scheduler.default_strategy == 'weighted'
        assert len(scheduler.sticky_mappings) == 0
    
    def test_init_with_invalid_strategy(self, mock_cookie_pool, mock_ip_pool):
        """Test initialization with invalid strategy."""
        with pytest.raises(ValueError, match="Invalid strategy"):
            PoolScheduler(mock_cookie_pool, mock_ip_pool, 'invalid')


class TestRoundRobinStrategy:
    """Test round-robin scheduling strategy."""
    
    def test_round_robin_distribution(self, scheduler, sample_accounts, sample_proxies):
        """Test round-robin distributes tasks evenly."""
        # Setup
        scheduler.cookie_pool.redis.smembers.return_value = {
            'acc_001', 'acc_002', 'acc_003'
        }
        for acc in sample_accounts:
            scheduler.cookie_pool.accounts[acc.account_id] = acc
        for proxy in sample_proxies:
            scheduler.ip_pool.proxies[proxy.proxy_id] = proxy
        
        # Assign 9 tasks
        assignments = []
        for i in range(9):
            task = Task(task_id=f't{i}', task_type='scrape', target=f'target{i}')
            account, proxy = scheduler.assign_task(task, strategy='round_robin')
            assignments.append(account.account_id)
        
        # Verify distribution (each account gets 3 tasks)
        from collections import Counter
        counts = Counter(assignments)
        assert len(counts) == 3
        assert all(count == 3 for count in counts.values())
    
    def test_round_robin_no_accounts(self, scheduler):
        """Test round-robin with no available accounts."""
        scheduler.cookie_pool.redis.smembers.return_value = set()
        
        task = Task(task_id='t1', task_type='scrape', target='target1')
        with pytest.raises(NoAvailableAccountException):
            scheduler.assign_task(task, strategy='round_robin')


class TestLeastLoadedStrategy:
    """Test least-loaded scheduling strategy."""
    
    def test_least_loaded_selection(self, scheduler, sample_accounts, sample_proxies):
        """Test least-loaded selects account with minimum load."""
        # Setup
        scheduler.cookie_pool.redis.smembers.return_value = {
            'acc_001', 'acc_002', 'acc_003'
        }
        for acc in sample_accounts:
            scheduler.cookie_pool.accounts[acc.account_id] = acc
        for proxy in sample_proxies:
            scheduler.ip_pool.proxies[proxy.proxy_id] = proxy
        
        # Assign task - should select acc_001 (load=10)
        task = Task(task_id='t1', task_type='scrape', target='target1')
        account, proxy = scheduler.assign_task(task, strategy='least_loaded')
        
        assert account.account_id == 'acc_001'
        assert account.daily_request_count == 10
    
    def test_least_loaded_filters_unhealthy_proxies(self, scheduler, sample_accounts, sample_proxies):
        """Test least-loaded filters accounts with unhealthy proxies."""
        # Setup - make proxy_001 unhealthy
        sample_proxies[0].health_score = 0.3
        
        scheduler.cookie_pool.redis.smembers.return_value = {
            'acc_001', 'acc_002', 'acc_003'
        }
        for acc in sample_accounts:
            scheduler.cookie_pool.accounts[acc.account_id] = acc
        for proxy in sample_proxies:
            scheduler.ip_pool.proxies[proxy.proxy_id] = proxy
        
        # Assign task - should skip acc_001 and select acc_002 (load=50)
        task = Task(task_id='t1', task_type='scrape', target='target1')
        account, proxy = scheduler.assign_task(task, strategy='least_loaded')
        
        assert account.account_id == 'acc_002'


class TestWeightedStrategy:
    """Test weighted scheduling strategy."""
    
    def test_weighted_selection(self, scheduler, sample_accounts, sample_proxies):
        """Test weighted strategy considers health and load."""
        # Setup
        scheduler.cookie_pool.redis.smembers.return_value = {
            'acc_001', 'acc_002', 'acc_003'
        }
        for acc in sample_accounts:
            scheduler.cookie_pool.accounts[acc.account_id] = acc
        for proxy in sample_proxies:
            scheduler.ip_pool.proxies[proxy.proxy_id] = proxy
        
        # Assign 100 tasks and check distribution
        assignments = []
        for i in range(100):
            task = Task(task_id=f't{i}', task_type='scrape', target=f'target{i}')
            account, proxy = scheduler.assign_task(task, strategy='weighted')
            assignments.append(account.account_id)
        
        from collections import Counter
        counts = Counter(assignments)
        
        # acc_001 should get the most (health=1.0, load=10/100)
        # acc_002 should get medium (health=0.9, load=50/100)
        # acc_003 should get the least (health=0.8, load=80/100)
        assert counts['acc_001'] > counts['acc_002']
        assert counts['acc_002'] > counts['acc_003']
    
    def test_weighted_zero_weight_fallback(self, scheduler, sample_accounts, sample_proxies):
        """Test weighted strategy with zero total weight."""
        # Setup - make all accounts fully loaded
        for acc in sample_accounts:
            acc.daily_request_count = acc.daily_quota
            acc.health_score = 0
        
        scheduler.cookie_pool.redis.smembers.return_value = {'acc_001'}
        scheduler.cookie_pool.accounts['acc_001'] = sample_accounts[0]
        scheduler.ip_pool.proxies['proxy_001'] = sample_proxies[0]
        
        # Should still work (random selection)
        task = Task(task_id='t1', task_type='scrape', target='target1')
        account, proxy = scheduler.assign_task(task, strategy='weighted')
        assert account.account_id == 'acc_001'


class TestStickyStrategy:
    """Test sticky scheduling strategy."""
    
    def test_sticky_creates_mapping(self, scheduler, sample_accounts, sample_proxies):
        """Test sticky strategy creates task_type mapping."""
        # Setup
        scheduler.cookie_pool.redis.smembers.return_value = {'acc_001'}
        scheduler.cookie_pool.accounts['acc_001'] = sample_accounts[0]
        scheduler.ip_pool.proxies['proxy_001'] = sample_proxies[0]
        
        # First task creates mapping
        task1 = Task(task_id='t1', task_type='scrape_product', target='p1')
        account1, _ = scheduler.assign_task(task1, strategy='sticky')
        
        assert 'scrape_product' in scheduler.sticky_mappings
        assert scheduler.sticky_mappings['scrape_product'] == account1.account_id
        
        # Second task with same type uses same account
        task2 = Task(task_id='t2', task_type='scrape_product', target='p2')
        account2, _ = scheduler.assign_task(task2, strategy='sticky')
        
        assert account2.account_id == account1.account_id
    
    def test_sticky_with_explicit_account_id(self, scheduler, sample_accounts, sample_proxies):
        """Test sticky strategy with explicit account_id."""
        # Setup
        scheduler.cookie_pool.redis.smembers.return_value = {
            'acc_001', 'acc_002'
        }
        for acc in sample_accounts[:2]:
            scheduler.cookie_pool.accounts[acc.account_id] = acc
        for proxy in sample_proxies[:2]:
            scheduler.ip_pool.proxies[proxy.proxy_id] = proxy
        
        # Task with explicit account_id
        task = Task(
            task_id='t1',
            task_type='scrape_product',
            target='p1',
            account_id='acc_002'
        )
        account, _ = scheduler.assign_task(task, strategy='sticky')
        
        # Should use specified account
        assert account.account_id == 'acc_002'
    
    def test_sticky_fallback_when_account_unavailable(self, scheduler, sample_accounts, sample_proxies):
        """Test sticky strategy fallback when mapped account unavailable."""
        # Setup
        scheduler.cookie_pool.redis.smembers.return_value = {
            'acc_001', 'acc_002'
        }
        for acc in sample_accounts[:2]:
            scheduler.cookie_pool.accounts[acc.account_id] = acc
        for proxy in sample_proxies[:2]:
            scheduler.ip_pool.proxies[proxy.proxy_id] = proxy
        
        # Create mapping
        scheduler.sticky_mappings['scrape_product'] = 'acc_001'
        
        # Make acc_001 unavailable
        sample_accounts[0].status = AccountStatus.COOLING.value
        
        # Task should fallback to weighted and clear mapping
        task = Task(task_id='t1', task_type='scrape_product', target='p1')
        account, _ = scheduler.assign_task(task, strategy='sticky')
        
        # Should select acc_002 and create new mapping
        assert account.account_id == 'acc_002'
        assert scheduler.sticky_mappings['scrape_product'] == 'acc_002'


class TestTaskRelease:
    """Test task release and metrics update."""
    
    def test_release_task_success(self, scheduler, sample_accounts, sample_proxies):
        """Test releasing successful task."""
        # Setup
        task = Task(task_id='t1', task_type='scrape', target='target1')
        task.assigned_account = 'acc_001'
        task.assigned_proxy = 'proxy_001'
        
        scheduler.ip_pool.proxies['proxy_001'] = sample_proxies[0]
        
        # Release task
        scheduler.release_task(task, success=True, response_time=1.5)
        
        # Verify cookie pool release called
        scheduler.cookie_pool.release_account.assert_called_once_with(
            'acc_001',
            success=True,
            response_time=1.5
        )
        
        # Verify proxy metrics updated
        proxy = scheduler.ip_pool.proxies['proxy_001']
        assert proxy.total_checks == 1
        assert proxy.success_checks == 1
        assert proxy.success_rate == 1.0
        assert proxy.avg_latency == 1500  # ms
    
    def test_release_task_failure(self, scheduler, sample_proxies):
        """Test releasing failed task."""
        task = Task(task_id='t1', task_type='scrape', target='target1')
        task.assigned_account = 'acc_001'
        task.assigned_proxy = 'proxy_001'
        
        scheduler.ip_pool.proxies['proxy_001'] = sample_proxies[0]
        
        # Release task
        scheduler.release_task(task, success=False, response_time=0.5)
        
        # Verify metrics
        proxy = scheduler.ip_pool.proxies['proxy_001']
        assert proxy.total_checks == 1
        assert proxy.success_checks == 0
        assert proxy.success_rate == 0.0
    
    def test_release_task_no_assignment(self, scheduler):
        """Test releasing task with no assignment."""
        task = Task(task_id='t1', task_type='scrape', target='target1')
        
        # Should not raise error
        scheduler.release_task(task, success=True)
        
        # Cookie pool should not be called
        scheduler.cookie_pool.release_account.assert_not_called()


class TestRebalancing:
    """Test pool rebalancing."""
    
    def test_rebalance_cooling_accounts(self, scheduler):
        """Test rebalancing cooling accounts."""
        # Setup
        scheduler.cookie_pool.resume_cooling_accounts.return_value = 2
        scheduler.cookie_pool.redis.smembers.return_value = {'acc_002', 'acc_003'}
        scheduler.sticky_mappings = {
            'scrape_product': 'acc_001',
            'scrape_user': 'acc_002',
            'scrape_video': 'acc_003'
        }
        
        # Rebalance
        resumed = scheduler.rebalance_cooling_accounts()
        
        assert resumed == 2
        # acc_002 and acc_003 mappings should be cleared
        assert 'scrape_product' in scheduler.sticky_mappings
        assert 'scrape_user' not in scheduler.sticky_mappings
        assert 'scrape_video' not in scheduler.sticky_mappings


class TestSchedulerStats:
    """Test scheduler statistics."""
    
    def test_get_scheduler_stats(self, scheduler):
        """Test getting scheduler statistics."""
        scheduler.sticky_mappings = {
            'scrape_product': 'acc_001',
            'scrape_user': 'acc_002'
        }
        
        stats = scheduler.get_scheduler_stats()
        
        assert stats['strategy'] == 'weighted'
        assert stats['sticky_mappings'] == 2
        assert 'sticky_details' in stats
        assert 'cookie_pool' in stats
        assert 'ip_pool' in stats
        assert 'timestamp' in stats


class TestIntegration:
    """Integration tests for scheduler."""
    
    def test_full_workflow(self, scheduler, sample_accounts, sample_proxies):
        """Test complete task assignment and release workflow."""
        # Setup
        scheduler.cookie_pool.redis.smembers.return_value = {
            'acc_001', 'acc_002', 'acc_003'
        }
        for acc in sample_accounts:
            scheduler.cookie_pool.accounts[acc.account_id] = acc
        for proxy in sample_proxies:
            scheduler.ip_pool.proxies[proxy.proxy_id] = proxy
        
        # Test all strategies
        strategies = ['round_robin', 'least_loaded', 'weighted', 'sticky']
        
        for strategy in strategies:
            task = Task(
                task_id=f't_{strategy}',
                task_type=f'test_{strategy}',
                target=f'target_{strategy}'
            )
            
            # Assign
            account, proxy = scheduler.assign_task(task, strategy=strategy)
            assert task.assigned_account is not None
            assert task.assigned_proxy is not None
            
            # Release
            scheduler.release_task(task, success=True, response_time=1.0)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
