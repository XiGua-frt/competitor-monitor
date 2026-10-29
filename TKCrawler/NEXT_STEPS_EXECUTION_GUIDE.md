# Cookie池 + IP池 - 下一步执行指南

> 创建时间: 2026-10-08  
> 目标: 完成池化系统的P0模块,实现10账号并发采集能力  
> 预估总工时: 5-7天

---

## 📋 执行计划总览

| 任务 | 优先级 | 预估工时 | 依赖关系 | 交付物 |
|------|--------|---------|---------|--------|
| Task 1: 实现P0核心模块 | **P0** | 3天 | 无 | 指纹池/调度器/健康检查器 |
| Task 2: 编写测试脚本 | **P0** | 1天 | Task 1 | 端到端测试 + 压力测试 |
| Task 3: 配置监控面板 | P1 | 1天 | Task 1 | Grafana Dashboard |
| Task 4: 自动登录脚本 | P1 | 2天 | 无(可并行) | 批量登录工具 |

**建议执行顺序**: Task 1 → Task 2 → Task 3, Task 4 可并行进行

---

## 🎯 Task 1: 实现剩余的P0核心模块

**目标**: 完成指纹池、调度器、健康检查器三个核心模块

### 1.1 指纹池管理器 (Day 1, 6-8小时)

#### 文件位置
```
src/pools/fingerprint_pool.py
```

#### 实现清单

##### 1.1.1 设备模板库
```python
"""Browser fingerprint pool manager."""

import random
import base64
import os
from typing import Dict, List
from .models import BrowserFingerprint
from ..common.logger import get_logger

logger = get_logger(__name__)


# 设备模板库(真实设备参数)
DEVICE_TEMPLATES = [
    {
        'name': 'MacBook Pro M1 Pro 16"',
        'viewport': {'width': 1920, 'height': 1080},
        'screen_resolution': '3456x2234',
        'user_agent': (
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/131.0.0.0 Safari/537.36'
        ),
        'webgl_vendor': 'Google Inc. (Apple)',
        'webgl_renderer': 'ANGLE (Apple, Apple M1 Pro, OpenGL 4.1)',
        'platform': 'MacIntel',
        'cpu_cores': 10,
        'device_memory': 16,
        'hardware_concurrency': 10
    },
    {
        'name': 'MacBook Air M2',
        'viewport': {'width': 1440, 'height': 900},
        'screen_resolution': '2560x1664',
        'user_agent': (
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/131.0.0.0 Safari/537.36'
        ),
        'webgl_vendor': 'Google Inc. (Apple)',
        'webgl_renderer': 'ANGLE (Apple, Apple M2, OpenGL 4.1)',
        'platform': 'MacIntel',
        'cpu_cores': 8,
        'device_memory': 8,
        'hardware_concurrency': 8
    },
    {
        'name': 'Windows Desktop RTX 3080',
        'viewport': {'width': 2560, 'height': 1440},
        'screen_resolution': '2560x1440',
        'user_agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/131.0.0.0 Safari/537.36'
        ),
        'webgl_vendor': 'Google Inc. (NVIDIA)',
        'webgl_renderer': 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3080, OpenGL 4.5)',
        'platform': 'Win32',
        'cpu_cores': 16,
        'device_memory': 32,
        'hardware_concurrency': 16
    },
    {
        'name': 'Windows Laptop RTX 3060',
        'viewport': {'width': 1920, 'height': 1080},
        'screen_resolution': '1920x1080',
        'user_agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/131.0.0.0 Safari/537.36'
        ),
        'webgl_vendor': 'Google Inc. (NVIDIA)',
        'webgl_renderer': 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Laptop, OpenGL 4.5)',
        'platform': 'Win32',
        'cpu_cores': 12,
        'device_memory': 16,
        'hardware_concurrency': 12
    },
    {
        'name': 'iMac 27" 5K',
        'viewport': {'width': 2560, 'height': 1440},
        'screen_resolution': '5120x2880',
        'user_agent': (
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/131.0.0.0 Safari/537.36'
        ),
        'webgl_vendor': 'Google Inc. (Apple)',
        'webgl_renderer': 'ANGLE (Apple, AMD Radeon Pro 5700 XT, OpenGL 4.1)',
        'platform': 'MacIntel',
        'cpu_cores': 10,
        'device_memory': 32,
        'hardware_concurrency': 10
    }
]

TIMEZONE_OPTIONS = [
    'America/Los_Angeles',  # PST
    'America/New_York',     # EST
    'America/Chicago',      # CST
    'America/Denver',       # MST
]

LOCALE_OPTIONS = ['en-US', 'en-GB']


class FingerprintPoolManager:
    """Manages browser fingerprints for accounts."""
    
    def __init__(self):
        """Initialize fingerprint pool."""
        self.fingerprints: Dict[str, BrowserFingerprint] = {}
        logger.info("FingerprintPoolManager initialized")
    
    def generate_fingerprint(self, account_id: str) -> BrowserFingerprint:
        """Generate unique fingerprint for account.
        
        Args:
            account_id: Account ID
        
        Returns:
            BrowserFingerprint object
        """
        fingerprint_id = f"fp_{account_id}"
        
        # Select random device template
        template = random.choice(DEVICE_TEMPLATES)
        
        # Generate unique canvas noise
        canvas_noise = self._generate_canvas_noise()
        
        # Random timezone and locale
        timezone = random.choice(TIMEZONE_OPTIONS)
        locale = random.choice(LOCALE_OPTIONS)
        
        fingerprint = BrowserFingerprint(
            fingerprint_id=fingerprint_id,
            user_agent=template['user_agent'],
            viewport=template['viewport'],
            screen_resolution=template['screen_resolution'],
            webgl_vendor=template['webgl_vendor'],
            webgl_renderer=template['webgl_renderer'],
            platform=template['platform'],
            cpu_cores=template['cpu_cores'],
            device_memory=template['device_memory'],
            hardware_concurrency=template['hardware_concurrency'],
            timezone=timezone,
            locale=locale,
            canvas_noise=canvas_noise
        )
        
        self.fingerprints[fingerprint_id] = fingerprint
        
        logger.info(
            f"Generated fingerprint {fingerprint_id} "
            f"(device: {template['name']}, timezone: {timezone})"
        )
        
        return fingerprint
    
    def get_fingerprint(self, account_id: str) -> BrowserFingerprint:
        """Get fingerprint for account.
        
        Args:
            account_id: Account ID
        
        Returns:
            BrowserFingerprint object
        """
        fingerprint_id = f"fp_{account_id}"
        
        if fingerprint_id not in self.fingerprints:
            return self.generate_fingerprint(account_id)
        
        return self.fingerprints[fingerprint_id]
    
    def apply_to_playwright_context(
        self,
        fingerprint: BrowserFingerprint,
        context_options: dict
    ) -> dict:
        """Apply fingerprint to Playwright browser context options.
        
        Args:
            fingerprint: BrowserFingerprint object
            context_options: Existing context options dict
        
        Returns:
            Updated context options
        """
        context_options.update({
            'viewport': fingerprint.viewport,
            'screen': {
                'width': int(fingerprint.screen_resolution.split('x')[0]),
                'height': int(fingerprint.screen_resolution.split('x')[1])
            },
            'user_agent': fingerprint.user_agent,
            'locale': fingerprint.locale,
            'timezone_id': fingerprint.timezone,
            'device_scale_factor': 2 if 'Retina' in fingerprint.screen_resolution else 1,
        })
        
        logger.debug(f"Applied fingerprint {fingerprint.fingerprint_id} to context")
        
        return context_options
    
    def _generate_canvas_noise(self) -> str:
        """Generate unique canvas noise for fingerprinting.
        
        Returns:
            Base64 encoded noise
        """
        # Generate 32 random bytes
        noise = os.urandom(32)
        return base64.b64encode(noise).decode('utf-8')
```

##### 1.1.2 验收标准
- [ ] 能生成5种不同设备的指纹
- [ ] Canvas噪声每次不同
- [ ] 指纹应用到Playwright context成功

---

### 1.2 池调度器 (Day 2, 4-6小时)

#### 文件位置
```
src/pools/scheduler.py
```

#### 实现清单

```python
"""Pool scheduler for task assignment and load balancing."""

from enum import Enum
from typing import Optional
from datetime import datetime

from .models import Task, AccountSession
from .cookie_pool import CookiePoolManager
from .ip_pool import IPPoolManager
from .exceptions import NoAvailableAccountException
from ..common.logger import get_logger

logger = get_logger(__name__)


class SchedulingStrategy(Enum):
    """Scheduling strategy enum."""
    ROUND_ROBIN = 'round_robin'
    LEAST_LOADED = 'least_loaded'
    WEIGHTED = 'weighted'
    STICKY = 'sticky'  # Same task type uses same account


class PoolScheduler:
    """Task scheduler with load balancing."""
    
    def __init__(
        self,
        cookie_pool: CookiePoolManager,
        ip_pool: IPPoolManager,
        default_strategy: str = 'weighted'
    ):
        """Initialize scheduler.
        
        Args:
            cookie_pool: Cookie pool manager
            ip_pool: IP pool manager
            default_strategy: Default scheduling strategy
        """
        self.cookie_pool = cookie_pool
        self.ip_pool = ip_pool
        self.default_strategy = default_strategy
        
        # Sticky mapping: task_type -> account_id
        self.sticky_mappings = {}
        
        logger.info(f"PoolScheduler initialized with strategy: {default_strategy}")
    
    def assign_task(
        self,
        task: Task,
        strategy: str = None
    ) -> AccountSession:
        """Assign account to task.
        
        Args:
            task: Task to assign
            strategy: Scheduling strategy (override default)
        
        Returns:
            Assigned AccountSession
        
        Raises:
            NoAvailableAccountException: When no account available
        """
        strategy = strategy or self.default_strategy
        
        # Sticky strategy: reuse same account for same task type
        if strategy == 'sticky':
            return self._assign_sticky(task)
        
        # Get account from pool
        account = self.cookie_pool.get_account(strategy=strategy)
        
        # Record assignment
        task.assigned_account = account.account_id
        
        logger.info(
            f"Assigned task {task.task_id} ({task.task_type}) "
            f"to account {account.account_id}"
        )
        
        return account
    
    def release_task(
        self,
        task: Task,
        success: bool,
        response_time: float = 0
    ):
        """Release task and update account metrics.
        
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
        
        logger.debug(
            f"Released task {task.task_id} from account {task.assigned_account} "
            f"(success: {success})"
        )
    
    def rebalance(self):
        """Rebalance load across accounts."""
        # Resume cooling accounts
        self.cookie_pool.resume_cooling_accounts()
        
        # Clear sticky mappings for suspended accounts
        suspended = self.cookie_pool.redis.smembers('accounts:suspended')
        for task_type, account_id in list(self.sticky_mappings.items()):
            if account_id in suspended:
                del self.sticky_mappings[task_type]
                logger.info(f"Cleared sticky mapping for {task_type}")
        
        logger.info("Rebalanced pool")
    
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
            'cookie_pool': cookie_stats,
            'ip_pool': ip_stats,
            'timestamp': datetime.now().isoformat()
        }
    
    def _assign_sticky(self, task: Task) -> AccountSession:
        """Assign using sticky strategy.
        
        Args:
            task: Task to assign
        
        Returns:
            AccountSession
        """
        # Check if task type has sticky mapping
        if task.task_type in self.sticky_mappings:
            account_id = self.sticky_mappings[task.task_type]
            account = self.cookie_pool._load_account(account_id)
            
            if account and account.is_available():
                task.assigned_account = account_id
                logger.debug(
                    f"Sticky assignment: {task.task_type} -> {account_id}"
                )
                return account
            else:
                # Mapping expired, remove it
                del self.sticky_mappings[task.task_type]
        
        # Create new sticky mapping
        account = self.cookie_pool.get_account(strategy='weighted')
        self.sticky_mappings[task.task_type] = account.account_id
        task.assigned_account = account.account_id
        
        logger.info(
            f"Created sticky mapping: {task.task_type} -> {account.account_id}"
        )
        
        return account
```

##### 1.2.2 验收标准
- [ ] 能分配任务到最优账号
- [ ] Sticky策略同类任务用同一账号
- [ ] Rebalance恢复冷却账号

---

### 1.3 健康检查器 (Day 2-3, 6-8小时)

#### 文件位置
```
src/pools/health_checker.py
```

#### 实现清单

```python
"""Health checker for pool maintenance."""

import asyncio
from typing import List
from datetime import datetime

from .cookie_pool import CookiePoolManager
from .ip_pool import IPPoolManager
from .models import AccountSession
from ..anti_detection.curl_client import CurlCffiClient
from ..common.logger import get_logger

logger = get_logger(__name__)


class PoolHealthChecker:
    """Health checker for cookie and IP pools."""
    
    def __init__(
        self,
        cookie_pool: CookiePoolManager,
        ip_pool: IPPoolManager
    ):
        """Initialize health checker.
        
        Args:
            cookie_pool: Cookie pool manager
            ip_pool: IP pool manager
        """
        self.cookie_pool = cookie_pool
        self.ip_pool = ip_pool
        
        logger.info("PoolHealthChecker initialized")
    
    async def check_all_accounts(self) -> List[dict]:
        """Check all active accounts.
        
        Returns:
            List of check results
        """
        active_ids = self.cookie_pool.redis.smembers('accounts:active')
        
        results = []
        failed_count = 0
        
        for acc_id in active_ids:
            session = self.cookie_pool._load_account(acc_id)
            if not session:
                continue
            
            is_alive = await self._test_account(session)
            
            if is_alive:
                logger.info(f"Account {acc_id} health check passed")
            else:
                logger.warning(f"Account {acc_id} failed health check")
                failed_count += 1
                
                # Attempt recovery
                recovery_success = await self._attempt_recovery(session)
                
                if not recovery_success:
                    # Suspend account
                    self.cookie_pool.suspend_account(
                        acc_id,
                        reason='health_check_failed'
                    )
            
            results.append({
                'account_id': acc_id,
                'alive': is_alive,
                'health_score': session.health_score,
                'timestamp': datetime.now().isoformat()
            })
        
        logger.info(
            f"Account health check completed: "
            f"{len(results)} checked, {failed_count} failed"
        )
        
        return results
    
    async def check_all_proxies(self) -> List[dict]:
        """Check all active proxies.
        
        Returns:
            List of check results
        """
        return self.ip_pool.health_check_all()
    
    async def _test_account(self, session: AccountSession) -> bool:
        """Test if account is alive.
        
        Args:
            session: AccountSession to test
        
        Returns:
            True if alive
        """
        try:
            # Get bound proxy
            proxy = self.ip_pool.get_proxy_for_account(session.account_id)
            
            # Create client with cookies and proxy
            client = CurlCffiClient()
            client.session.proxies = {
                'http': proxy.proxy_url,
                'https': proxy.proxy_url
            }
            
            headers = {
                'Cookie': '; '.join([f'{k}={v}' for k, v in session.cookies.items()])
            }
            
            # Test request
            response = client.get(
                'https://www.tiktok.com/api/user/info/',
                headers=headers,
                timeout=10
            )
            
            if response.status_code == 200:
                data = response.json()
                return data.get('statusCode') == 0
            
            return False
            
        except Exception as e:
            logger.error(f"Account test error for {session.account_id}: {e}")
            return False
    
    async def _attempt_recovery(self, session: AccountSession) -> bool:
        """Attempt to recover failed account.
        
        Args:
            session: AccountSession to recover
        
        Returns:
            True if recovered
        """
        logger.info(f"Attempting to recover account {session.account_id}...")
        
        try:
            # TODO: Implement auto re-login
            # For now, just trigger cooling
            self.cookie_pool.trigger_cooling(session.account_id, duration_hours=4)
            
            return False
            
        except Exception as e:
            logger.error(f"Recovery failed for {session.account_id}: {e}")
            return False
```

##### 1.3.3 验收标准
- [ ] 能检测账号存活状态
- [ ] 失效账号自动尝试恢复
- [ ] 恢复失败后自动暂停

---

### 1.4 集成到主模块

#### 更新 `src/pools/__init__.py`

```python
"""Pool management for Cookie Pool and IP Pool."""

from .cookie_pool import CookiePoolManager, AccountSession
from .ip_pool import IPPoolManager, ProxyNode
from .fingerprint_pool import FingerprintPoolManager, BrowserFingerprint
from .scheduler import PoolScheduler, SchedulingStrategy
from .health_checker import PoolHealthChecker
from .exceptions import (
    NoAvailableAccountException,
    NoAvailableProxyException,
    AccountSuspendedException,
    ProxyFailedException
)

__all__ = [
    'CookiePoolManager',
    'AccountSession',
    'IPPoolManager',
    'ProxyNode',
    'FingerprintPoolManager',
    'BrowserFingerprint',
    'PoolScheduler',
    'SchedulingStrategy',
    'PoolHealthChecker',
    'NoAvailableAccountException',
    'NoAvailableProxyException',
    'AccountSuspendedException',
    'ProxyFailedException'
]
```

---

## 🧪 Task 2: 编写完整的测试脚本

**目标**: 验证池化系统的正确性和性能

### 2.1 单元测试 (Day 4上午, 2-3小时)

#### 文件位置
```
tests/test_pools.py
```

#### 测试用例清单

```python
"""Unit tests for pool management."""

import pytest
import redis
from unittest.mock import Mock, patch

from src.pools import (
    CookiePoolManager,
    IPPoolManager,
    FingerprintPoolManager,
    PoolScheduler,
    PoolHealthChecker
)
from src.pools.models import Task


@pytest.fixture
def redis_client():
    """Mock Redis client."""
    client = redis.Redis(host='localhost', port=6379, db=15, decode_responses=True)
    yield client
    # Cleanup
    client.flushdb()


@pytest.fixture
def cookie_pool(redis_client):
    """Cookie pool fixture."""
    return CookiePoolManager(redis_client)


@pytest.fixture
def ip_pool(redis_client):
    """IP pool fixture."""
    return IPPoolManager(redis_client)


class TestCookiePool:
    """Test Cookie Pool Manager."""
    
    def test_add_account(self, cookie_pool):
        """Test adding account to pool."""
        account_id = cookie_pool.add_account(
            username='test_user',
            cookies={'sessionid': 'test_session'},
            proxy_id='proxy_1',
            daily_quota=200
        )
        
        assert account_id.startswith('acc_')
        assert len(cookie_pool.accounts) == 1
    
    def test_get_account_weighted(self, cookie_pool):
        """Test getting account with weighted strategy."""
        # Add 3 accounts
        for i in range(3):
            cookie_pool.add_account(
                username=f'user_{i}',
                cookies={'sessionid': f'session_{i}'},
                proxy_id=f'proxy_{i}',
                daily_quota=200
            )
        
        account = cookie_pool.get_account(strategy='weighted')
        assert account is not None
        assert account.is_available()
    
    def test_release_account_success(self, cookie_pool):
        """Test releasing account after success."""
        account_id = cookie_pool.add_account(
            username='test_user',
            cookies={'sessionid': 'test'},
            proxy_id='proxy_1'
        )
        
        account = cookie_pool._load_account(account_id)
        initial_health = account.health_score
        
        cookie_pool.release_account(account_id, success=True)
        
        account = cookie_pool._load_account(account_id)
        assert account.health_score >= initial_health
        assert account.daily_request_count == 1
    
    def test_trigger_cooling(self, cookie_pool):
        """Test cooling mechanism."""
        account_id = cookie_pool.add_account(
            username='test_user',
            cookies={'sessionid': 'test'},
            proxy_id='proxy_1'
        )
        
        cookie_pool.trigger_cooling(account_id, duration_hours=2)
        
        account = cookie_pool._load_account(account_id)
        assert account.status == 'cooling'
        assert account.cooling_until is not None


class TestIPPool:
    """Test IP Pool Manager."""
    
    @patch('src.pools.ip_pool.CurlCffiClient')
    def test_add_proxy(self, mock_client, ip_pool):
        """Test adding proxy to pool."""
        # Mock health check success
        mock_response = Mock()
        mock_response.status_code = 200
        mock_client.return_value.get.return_value = mock_response
        
        proxy_id = ip_pool.add_proxy(
            proxy_url='http://user:pass@proxy.com:8080',
            region='US'
        )
        
        assert proxy_id.startswith('proxy_')
        assert len(ip_pool.proxies) == 1
    
    def test_bind_proxy_to_account(self, ip_pool):
        """Test binding proxy to account."""
        # Add proxy first
        with patch('src.pools.ip_pool.CurlCffiClient'):
            proxy_id = ip_pool.add_proxy('http://test:test@proxy.com:8080')
        
        # Bind to account
        success = ip_pool.bind_proxy_to_account(proxy_id, 'acc_123')
        
        assert success
        assert ip_pool.get_bound_proxy_id('acc_123') == proxy_id


class TestScheduler:
    """Test Pool Scheduler."""
    
    def test_assign_task(self, cookie_pool, ip_pool):
        """Test task assignment."""
        scheduler = PoolScheduler(cookie_pool, ip_pool)
        
        # Add account
        cookie_pool.add_account(
            username='test',
            cookies={'sessionid': 'test'},
            proxy_id='proxy_1'
        )
        
        task = Task(
            task_id='task_1',
            task_type='scrape_product',
            target='7123456789'
        )
        
        account = scheduler.assign_task(task)
        
        assert account is not None
        assert task.assigned_account is not None
    
    def test_sticky_strategy(self, cookie_pool, ip_pool):
        """Test sticky scheduling strategy."""
        scheduler = PoolScheduler(cookie_pool, ip_pool)
        
        # Add 2 accounts
        for i in range(2):
            cookie_pool.add_account(
                username=f'user_{i}',
                cookies={'sessionid': f'session_{i}'},
                proxy_id=f'proxy_{i}'
            )
        
        # Assign 2 tasks of same type
        task1 = Task(task_id='t1', task_type='scrape_product', target='p1')
        task2 = Task(task_id='t2', task_type='scrape_product', target='p2')
        
        acc1 = scheduler.assign_task(task1, strategy='sticky')
        acc2 = scheduler.assign_task(task2, strategy='sticky')
        
        # Should use same account
        assert acc1.account_id == acc2.account_id
```

##### 2.1.2 运行测试

```bash
# 安装测试依赖
pip install pytest pytest-asyncio pytest-cov

# 运行测试
pytest tests/test_pools.py -v

# 生成覆盖率报告
pytest tests/test_pools.py --cov=src/pools --cov-report=html
```

---

### 2.2 端到端测试 (Day 4下午, 3-4小时)

#### 文件位置
```
tests/test_e2e_pool.py
```

#### 测试场景

```python
"""End-to-end test for pool system."""

import asyncio
import pytest
from src.pools import (
    CookiePoolManager,
    IPPoolManager,
    PoolScheduler,
    FingerprintPoolManager
)
from src.pools.models import Task
from src.scrapers import ShopProductScraper


@pytest.mark.asyncio
async def test_10_accounts_concurrent_scraping():
    """Test 10 accounts concurrent scraping."""
    
    # Initialize pools
    cookie_pool = CookiePoolManager()
    ip_pool = IPPoolManager()
    scheduler = PoolScheduler(cookie_pool, ip_pool)
    
    # Add 10 accounts (mock data)
    for i in range(10):
        # Add proxy
        proxy_id = f"proxy_{i}"
        # Note: Use real proxies in production
        
        # Add account
        account_id = cookie_pool.add_account(
            username=f'tiktok_user_{i}',
            cookies={
                'sessionid': f'mock_session_{i}',
                # ... other cookies
            },
            proxy_id=proxy_id,
            daily_quota=200
        )
        
        print(f"Added account {i+1}/10: {account_id}")
    
    # Create 100 tasks
    tasks = [
        Task(
            task_id=f'task_{i}',
            task_type='scrape_product',
            target=f'product_{i}'
        )
        for i in range(100)
    ]
    
    # Execute tasks
    results = []
    for task in tasks:
        try:
            # Assign account
            account = scheduler.assign_task(task)
            proxy = ip_pool.get_proxy(account.account_id)
            
            # Scrape (mock)
            # In production: use real ShopProductScraper
            await asyncio.sleep(0.1)  # Simulate scraping
            success = True
            
            # Release account
            scheduler.release_task(task, success=success)
            
            results.append({'task_id': task.task_id, 'success': success})
            
        except Exception as e:
            print(f"Task {task.task_id} failed: {e}")
            results.append({'task_id': task.task_id, 'success': False})
    
    # Check results
    success_count = sum(1 for r in results if r['success'])
    print(f"\nResults: {success_count}/100 tasks succeeded")
    
    # Check pool stats
    stats = scheduler.get_scheduler_stats()
    print(f"\nPool stats:")
    print(f"  Active accounts: {stats['cookie_pool']['active_accounts']}")
    print(f"  Avg health score: {stats['cookie_pool']['avg_health_score']}")
    
    assert success_count >= 95  # At least 95% success rate
```

##### 2.2.2 压力测试脚本

```python
"""Stress test for pool system."""

import time
import asyncio
from src.pools import CookiePoolManager, IPPoolManager, PoolScheduler


async def stress_test_2000_products():
    """Stress test: 2000 products in one day."""
    
    cookie_pool = CookiePoolManager()
    ip_pool = IPPoolManager()
    scheduler = PoolScheduler(cookie_pool, ip_pool)
    
    # Add 10 accounts
    # ... (same as above)
    
    start_time = time.time()
    
    # Simulate 2000 products
    for i in range(2000):
        account = scheduler.assign_task(Task(
            task_id=f'stress_{i}',
            task_type='scrape_product',
            target=f'product_{i}'
        ))
        
        # Simulate scraping time (7-10s)
        await asyncio.sleep(0.01)  # Mock fast execution
        
        scheduler.release_task(Task(task_id=f'stress_{i}'), success=True)
        
        if (i + 1) % 100 == 0:
            elapsed = time.time() - start_time
            print(f"Progress: {i+1}/2000 ({elapsed:.1f}s)")
    
    total_time = time.time() - start_time
    print(f"\nCompleted 2000 products in {total_time:.1f}s")
    print(f"Average: {total_time/2000:.2f}s per product")
```

---

## 📊 Task 3: 配置Grafana监控面板

**目标**: 可视化池状态与性能指标

### 3.1 Prometheus Metrics暴露 (Day 5上午, 2小时)

#### 文件位置
```
src/pools/metrics.py
```

#### 实现清单

```python
"""Prometheus metrics for pool monitoring."""

from prometheus_client import Gauge, Counter, Histogram
from .cookie_pool import CookiePoolManager
from .ip_pool import IPPoolManager


# Cookie Pool Metrics
accounts_total = Gauge('pool_accounts_total', 'Total accounts')
accounts_active = Gauge('pool_accounts_active', 'Active accounts')
accounts_cooling = Gauge('pool_accounts_cooling', 'Cooling accounts')
accounts_suspended = Gauge('pool_accounts_suspended', 'Suspended accounts')
avg_health_score = Gauge('pool_avg_health_score', 'Average health score')

# IP Pool Metrics
proxies_total = Gauge('pool_proxies_total', 'Total proxies')
proxies_active = Gauge('pool_proxies_active', 'Active proxies')
avg_proxy_latency = Gauge('pool_avg_proxy_latency_ms', 'Average proxy latency')

# Request Metrics
requests_total = Counter('pool_requests_total', 'Total requests', ['account_id', 'status'])
request_duration = Histogram('pool_request_duration_seconds', 'Request duration')

# WAF Metrics
waf_blocks_total = Counter('pool_waf_blocks_total', 'WAF blocks', ['account_id'])


def collect_pool_metrics(cookie_pool: CookiePoolManager, ip_pool: IPPoolManager):
    """Collect and update pool metrics."""
    
    # Cookie pool metrics
    cookie_stats = cookie_pool.get_pool_stats()
    accounts_total.set(cookie_stats['total_accounts'])
    accounts_active.set(cookie_stats['active_accounts'])
    accounts_cooling.set(cookie_stats['cooling_accounts'])
    accounts_suspended.set(cookie_stats['suspended_accounts'])
    avg_health_score.set(cookie_stats['avg_health_score'])
    
    # IP pool metrics
    ip_stats = ip_pool.get_pool_stats()
    proxies_total.set(ip_stats['total_proxies'])
    proxies_active.set(ip_stats['active_proxies'])
    avg_proxy_latency.set(ip_stats['avg_latency_ms'])
```

### 3.2 Grafana Dashboard配置 (Day 5下午, 2小时)

#### 文件位置
```
docs/monitoring/pool_dashboard.json
```

#### Dashboard面板配置

```json
{
  "dashboard": {
    "title": "TikTok爬虫 - Cookie池 & IP池监控",
    "tags": ["pool", "tiktok"],
    "timezone": "browser",
    "panels": [
      {
        "id": 1,
        "title": "账号状态分布",
        "type": "piechart",
        "gridPos": {"x": 0, "y": 0, "w": 8, "h": 8},
        "targets": [
          {
            "expr": "pool_accounts_active",
            "legendFormat": "Active"
          },
          {
            "expr": "pool_accounts_cooling",
            "legendFormat": "Cooling"
          },
          {
            "expr": "pool_accounts_suspended",
            "legendFormat": "Suspended"
          }
        ]
      },
      {
        "id": 2,
        "title": "平均健康分数",
        "type": "gauge",
        "gridPos": {"x": 8, "y": 0, "w": 8, "h": 8},
        "targets": [
          {
            "expr": "pool_avg_health_score",
            "legendFormat": "Health Score"
          }
        ],
        "options": {
          "min": 0,
          "max": 1,
          "thresholds": [
            {"value": 0, "color": "red"},
            {"value": 0.5, "color": "yellow"},
            {"value": 0.8, "color": "green"}
          ]
        }
      },
      {
        "id": 3,
        "title": "请求成功率(24h)",
        "type": "graph",
        "gridPos": {"x": 0, "y": 8, "w": 12, "h": 8},
        "targets": [
          {
            "expr": "rate(pool_requests_total{status=\"success\"}[5m])",
            "legendFormat": "Success Rate"
          },
          {
            "expr": "rate(pool_requests_total{status=\"failed\"}[5m])",
            "legendFormat": "Failure Rate"
          }
        ]
      },
      {
        "id": 4,
        "title": "WAF拦截率",
        "type": "graph",
        "gridPos": {"x": 12, "y": 8, "w": 12, "h": 8},
        "targets": [
          {
            "expr": "rate(pool_waf_blocks_total[1h])",
            "legendFormat": "WAF Blocks/hour"
          }
        ],
        "alert": {
          "name": "WAF拦截率过高",
          "conditions": [
            {
              "evaluator": {"type": "gt", "params": [10]},
              "query": {"model": "rate(pool_waf_blocks_total[1h])"}
            }
          ],
          "notifications": [{"uid": "slack-alert"}]
        }
      },
      {
        "id": 5,
        "title": "代理延迟",
        "type": "graph",
        "gridPos": {"x": 0, "y": 16, "w": 12, "h": 8},
        "targets": [
          {
            "expr": "pool_avg_proxy_latency_ms",
            "legendFormat": "Avg Latency (ms)"
          }
        ]
      }
    ]
  }
}
```

---

## 🔐 Task 4: 编写自动登录脚本

**目标**: 批量获取TikTok账号Cookie

### 4.1 登录脚本 (Day 6-7, 2天)

#### 文件位置
```
src/pools/bootstrapper.py
```

#### 实现清单

```python
"""Account bootstrapper for batch login."""

import asyncio
import random
from typing import List, Dict
from playwright.async_api import async_playwright, Browser, Page

from .cookie_pool import CookiePoolManager
from .ip_pool import IPPoolManager
from .fingerprint_pool import FingerprintPoolManager
from ..common.logger import get_logger

logger = get_logger(__name__)


class AccountBootstrapper:
    """Batch account login and cookie extraction."""
    
    def __init__(
        self,
        cookie_pool: CookiePoolManager,
        ip_pool: IPPoolManager,
        fingerprint_pool: FingerprintPoolManager
    ):
        """Initialize bootstrapper.
        
        Args:
            cookie_pool: Cookie pool manager
            ip_pool: IP pool manager
            fingerprint_pool: Fingerprint pool manager
        """
        self.cookie_pool = cookie_pool
        self.ip_pool = ip_pool
        self.fingerprint_pool = fingerprint_pool
        
        logger.info("AccountBootstrapper initialized")
    
    async def bootstrap_accounts(
        self,
        accounts: List[Dict[str, str]],
        proxy_provider_url: str = None
    ) -> List[str]:
        """Bootstrap multiple accounts.
        
        Args:
            accounts: List of {'username': 'xxx', 'password': 'xxx'}
            proxy_provider_url: Optional proxy provider API URL
        
        Returns:
            List of successfully added account IDs
        """
        results = []
        
        for i, account_info in enumerate(accounts):
            logger.info(f"Bootstrapping account {i+1}/{len(accounts)}: {account_info['username']}")
            
            try:
                # 1. Get proxy
                if proxy_provider_url:
                    proxy_url = await self._get_proxy_from_provider(proxy_provider_url)
                    proxy_id = self.ip_pool.add_proxy(proxy_url, region='US')
                else:
                    # Use existing proxy
                    proxy = self.ip_pool.get_proxy()
                    proxy_id = proxy.proxy_id
                
                # 2. Login and extract cookies
                cookies = await self._login_with_browser(
                    username=account_info['username'],
                    password=account_info['password'],
                    proxy_id=proxy_id
                )
                
                # 3. Generate fingerprint
                fingerprint = self.fingerprint_pool.generate_fingerprint(
                    account_id=f"temp_{i}"
                )
                
                # 4. Add to cookie pool
                account_id = self.cookie_pool.add_account(
                    username=account_info['username'],
                    cookies=cookies,
                    proxy_id=proxy_id,
                    fingerprint_id=fingerprint.fingerprint_id,
                    daily_quota=200
                )
                
                results.append(account_id)
                logger.info(f"✅ Successfully bootstrapped {account_info['username']} -> {account_id}")
                
                # 5. Random delay to avoid batch detection
                delay = random.uniform(30, 60)
                logger.info(f"Waiting {delay:.1f}s before next account...")
                await asyncio.sleep(delay)
                
            except Exception as e:
                logger.error(f"❌ Failed to bootstrap {account_info['username']}: {e}")
                continue
        
        logger.info(f"Bootstrap completed: {len(results)}/{len(accounts)} succeeded")
        return results
    
    async def _login_with_browser(
        self,
        username: str,
        password: str,
        proxy_id: str
    ) -> Dict[str, str]:
        """Login to TikTok using browser.
        
        Args:
            username: TikTok username
            password: TikTok password
            proxy_id: Proxy ID
        
        Returns:
            Cookie dictionary
        """
        proxy = self.ip_pool._load_proxy(proxy_id)
        
        async with async_playwright() as p:
            # Launch browser with proxy
            browser = await p.firefox.launch(
                headless=False,  # Set to True in production
                proxy={
                    'server': proxy.proxy_url.replace('http://', ''),
                }
            )
            
            context = await browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent=(
                    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/131.0.0.0 Safari/537.36'
                ),
                locale='en-US',
                timezone_id='America/Los_Angeles',
            )
            
            page = await context.new_page()
            
            try:
                # Navigate to login page
                logger.info(f"Navigating to TikTok login page...")
                await page.goto('https://www.tiktok.com/login/phone-or-email/email')
                await page.wait_for_load_state('networkidle')
                
                # Fill login form
                logger.info("Filling login form...")
                await page.fill('input[name="username"]', username)
                await asyncio.sleep(random.uniform(1, 2))
                
                await page.fill('input[type="password"]', password)
                await asyncio.sleep(random.uniform(1, 2))
                
                # Click login button
                logger.info("Clicking login button...")
                await page.click('button[type="submit"]')
                
                # Wait for login success
                logger.info("Waiting for login to complete...")
                await page.wait_for_url('https://www.tiktok.com/', timeout=60000)
                
                # Handle 2FA if needed
                # TODO: Implement 2FA handling
                
                # Extract cookies
                cookies_list = await context.cookies()
                cookies = {cookie['name']: cookie['value'] for cookie in cookies_list}
                
                logger.info(f"Extracted {len(cookies)} cookies")
                
                return cookies
                
            finally:
                await browser.close()
    
    async def _get_proxy_from_provider(self, provider_url: str) -> str:
        """Get proxy from provider API.
        
        Args:
            provider_url: Provider API URL
        
        Returns:
            Proxy URL
        """
        # TODO: Implement proxy provider integration
        # Example: https://api.proxy-provider.com/get_proxy?region=US
        pass
```

#### 4.1.2 使用脚本

```python
"""Bootstrap accounts script."""

import asyncio
from src.pools import (
    CookiePoolManager,
    IPPoolManager,
    FingerprintPoolManager
)
from src.pools.bootstrapper import AccountBootstrapper


async def main():
    """Bootstrap 10 accounts."""
    
    # Initialize
    cookie_pool = CookiePoolManager()
    ip_pool = IPPoolManager()
    fingerprint_pool = FingerprintPoolManager()
    
    bootstrapper = AccountBootstrapper(
        cookie_pool,
        ip_pool,
        fingerprint_pool
    )
    
    # Account list
    accounts = [
        {'username': 'user1@example.com', 'password': 'password1'},
        {'username': 'user2@example.com', 'password': 'password2'},
        # ... add more
    ]
    
    # Bootstrap
    results = await bootstrapper.bootstrap_accounts(accounts)
    
    print(f"\n✅ Successfully bootstrapped {len(results)} accounts")
    print(f"Account IDs: {results}")


if __name__ == '__main__':
    asyncio.run(main())
```

---

## ✅ 验收清单

### Task 1: P0核心模块
- [ ] 指纹池能生成5种不同设备指纹
- [ ] 调度器能按策略分配任务
- [ ] 健康检查器能检测失效账号

### Task 2: 测试脚本
- [ ] 单元测试覆盖率>80%
- [ ] 端到端测试10账号并发通过
- [ ] 压力测试2000商品模拟成功

### Task 3: 监控面板
- [ ] Prometheus metrics正常暴露
- [ ] Grafana Dashboard显示实时数据
- [ ] WAF告警规则生效

### Task 4: 自动登录
- [ ] 能成功登录TikTok
- [ ] 提取27个Cookie完整
- [ ] 批量登录间隔30-60秒

---

## 📅 时间表

| Day | 任务 | 交付物 | 验收 |
|-----|------|--------|------|
| Day 1 | 指纹池实现 | fingerprint_pool.py | 生成不同指纹 |
| Day 2 | 调度器实现 | scheduler.py | 任务分配正确 |
| Day 3 | 健康检查器 | health_checker.py | 检测失效账号 |
| Day 4 | 测试脚本 | test_pools.py + test_e2e_pool.py | 测试通过 |
| Day 5 | 监控面板 | metrics.py + dashboard.json | Dashboard展示 |
| Day 6-7 | 自动登录 | bootstrapper.py | 批量登录成功 |

---

## 🚀 执行建议

1. **优先级**: Task 1 (P0核心) → Task 2 (测试) → Task 3/4 (并行)
2. **每日站会**: 同步进度,解决阻塞
3. **风险控制**: Day 1-3完成后立即小规模测试
4. **代码审查**: 每完成一个模块提交PR审查

---

**准备好了吗? 让我们开始实现! 🎯**

需要我立即开始编写代码吗?我建议从Task 1.1 (指纹池)开始!
