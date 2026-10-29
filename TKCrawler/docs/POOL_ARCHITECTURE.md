# Cookie池 + IP池架构设计方案

> 设计日期: 2026-10-08  
> 目标: 为规模化采集打好基础,支持10+账号并发,单日配额提升至2000+

---

## 🎯 设计目标

### 业务目标
- **短期(1-3个月)**: 单日配额从200商品提升至2000商品
- **中期(3-6个月)**: 支持10-20个账号并发,单日1万+商品
- **长期(6-12个月)**: 支持50+账号,覆盖多区域(美区/欧区/东南亚)

### 技术目标
1. **账号隔离**: 每个账号独立指纹 + 独立IP,避免关联封禁
2. **故障容错**: 单账号失效不影响整体采集
3. **负载均衡**: 智能分配任务,避免单账号过载
4. **健康监控**: 实时监控账号状态,自动剔除异常账号

---

## 🏗️ 整体架构

```
                    ┌─────────────────────────────────┐
                    │   调度层 (TaskScheduler)          │
                    │   - 任务分发                      │
                    │   - 负载均衡                      │
                    └──────────────┬──────────────────┘
                                   │
                    ┌──────────────▼──────────────────┐
                    │   池管理层 (PoolManager)          │
                    │   - Cookie Pool                  │
                    │   - IP Pool                      │
                    │   - 账号健康检查                  │
                    └──────────────┬──────────────────┘
                                   │
        ┌──────────────────────────┼──────────────────────────┐
        │                          │                          │
┌───────▼────────┐      ┌──────────▼────────┐      ┌─────────▼───────┐
│  Account #1    │      │  Account #2        │      │  Account #N     │
│  Cookie + IP   │      │  Cookie + IP       │      │  Cookie + IP    │
│  Fingerprint   │      │  Fingerprint       │      │  Fingerprint    │
└────────────────┘      └───────────────────┘      └─────────────────┘
        │                          │                          │
        └──────────────────────────┼──────────────────────────┘
                                   │
                    ┌──────────────▼──────────────────┐
                    │   反检测层 (AntiDetection)        │
                    │   - curl_cffi (主通道)           │
                    │   - camoufox (登录态获取)        │
                    └─────────────────────────────────┘
```

---

## 📦 核心模块设计

### 1. Cookie池管理器 (CookiePoolManager)

**职责**:
- 管理多个TikTok账号的Cookie
- 自动轮换账号
- 健康检查与失效剔除
- 账号优先级调度

**数据结构**:
```python
class AccountSession:
    account_id: str           # 账号唯一标识
    username: str             # TikTok用户名
    cookies: Dict[str, str]   # Cookie字典
    proxy_id: str             # 绑定的代理ID
    fingerprint_id: str       # 绑定的指纹ID
    
    # 状态管理
    status: str               # active/cooling/suspended/failed
    health_score: float       # 健康分数 0-1
    last_request_time: datetime
    daily_request_count: int
    daily_quota: int          # 每日配额
    
    # 风控指标
    waf_block_count: int      # WAF拦截次数
    rate_limit_count: int     # 429次数
    last_waf_time: datetime
    cooling_until: datetime   # 冷却截止时间
    
    # 业务指标
    success_rate: float       # 成功率
    avg_response_time: float  # 平均响应时间
    total_requests: int       # 总请求数
```

**核心接口**:
```python
class CookiePoolManager:
    def add_account(self, username: str, password: str, proxy_id: str) -> str
    def remove_account(self, account_id: str) -> bool
    def get_account(self, strategy: str = 'round_robin') -> AccountSession
    def release_account(self, account_id: str, success: bool)
    def health_check_all(self) -> List[dict]
    def suspend_account(self, account_id: str, reason: str)
    def get_pool_stats(self) -> dict
```

---

### 2. IP池管理器 (IPPoolManager)

**职责**:
- 管理代理IP池(HTTP/SOCKS5)
- IP可用性检测
- IP与账号绑定(长期绑定,避免关联)
- IP轮换策略

**数据结构**:
```python
class ProxyNode:
    proxy_id: str             # 代理唯一标识
    proxy_url: str            # http://user:pass@host:port
    proxy_type: str           # http/socks5
    region: str               # 区域(US/EU/APAC)
    
    # 状态管理
    status: str               # active/failed/testing
    health_score: float       # 健康分数
    last_check_time: datetime
    
    # 绑定关系
    bound_accounts: List[str] # 绑定的账号ID列表
    
    # 性能指标
    avg_latency: float        # 平均延迟(ms)
    success_rate: float       # 成功率
    bandwidth_limit: int      # 带宽限制(MB/day)
    daily_usage: int          # 当日使用量
```

**核心接口**:
```python
class IPPoolManager:
    def add_proxy(self, proxy_url: str, region: str) -> str
    def remove_proxy(self, proxy_id: str) -> bool
    def get_proxy(self, account_id: str = None) -> ProxyNode
    def bind_proxy_to_account(self, proxy_id: str, account_id: str)
    def health_check_all(self) -> List[dict]
    def rotate_proxy(self, account_id: str) -> ProxyNode
```

---

### 3. 指纹池管理器 (FingerprintPoolManager)

**职责**:
- 管理浏览器指纹配置
- 为每个账号生成独特指纹
- 指纹与账号绑定

**指纹要素**:
```python
class BrowserFingerprint:
    fingerprint_id: str
    
    # 基础参数
    user_agent: str
    viewport: Dict[str, int]  # {'width': 1920, 'height': 1080}
    timezone: str
    locale: str
    
    # WebGL指纹
    webgl_vendor: str
    webgl_renderer: str
    
    # Canvas指纹
    canvas_noise: bytes       # Canvas噪声
    
    # 其他
    screen_resolution: str
    cpu_cores: int
    device_memory: int
    hardware_concurrency: int
```

---

### 4. 池调度器 (PoolScheduler)

**职责**:
- 任务分配策略
- 负载均衡
- 账号冷却管理

**调度策略**:
```python
class SchedulingStrategy(Enum):
    ROUND_ROBIN = 'round_robin'      # 轮询
    LEAST_LOADED = 'least_loaded'    # 最小负载
    WEIGHTED = 'weighted'            # 加权(按健康分数)
    STICKY = 'sticky'                # 粘性(同类任务用同一账号)
```

**核心逻辑**:
```python
class PoolScheduler:
    def assign_task(self, task: Task) -> AccountSession:
        """为任务分配账号"""
        # 1. 过滤可用账号(active状态 + 未达配额)
        # 2. 按策略选择账号
        # 3. 更新账号状态
        pass
    
    def trigger_cooling(self, account_id: str, duration: int):
        """触发账号冷却"""
        # 遇到WAF/429时,立即冷却1-4小时
        pass
```

---

## 🔧 实现优先级

### Phase 1: 基础池化 (Week 1-2)

#### 任务1.1: Cookie池核心实现
```
src/pools/
├── __init__.py
├── cookie_pool.py          # CookiePoolManager
├── models.py               # AccountSession/ProxyNode/Fingerprint数据模型
└── storage.py              # Redis存储层
```

**关键实现**:
```python
# cookie_pool.py
class CookiePoolManager:
    def __init__(self, redis_client: Redis):
        self.redis = redis_client
        self.accounts: Dict[str, AccountSession] = {}
        
    def add_account(self, username: str, cookies: dict, proxy_id: str) -> str:
        """添加账号到池中"""
        account_id = f"acc_{uuid.uuid4().hex[:8]}"
        session = AccountSession(
            account_id=account_id,
            username=username,
            cookies=cookies,
            proxy_id=proxy_id,
            status='active',
            daily_quota=200  # 初始保守配额
        )
        
        # 持久化到Redis
        self.redis.hset(f'account:{account_id}', mapping=session.to_dict())
        self.redis.sadd('accounts:active', account_id)
        
        logger.info(f"Added account {username} to pool")
        return account_id
    
    def get_account(self, strategy: str = 'weighted') -> AccountSession:
        """获取可用账号(加权策略)"""
        active_ids = self.redis.smembers('accounts:active')
        
        candidates = []
        for acc_id in active_ids:
            session = self._load_account(acc_id)
            
            # 过滤条件
            if (session.status == 'active' and 
                session.daily_request_count < session.daily_quota and
                session.cooling_until < datetime.now()):
                
                # 加权: health_score + 剩余配额比例
                weight = session.health_score * (
                    1 - session.daily_request_count / session.daily_quota
                )
                candidates.append((session, weight))
        
        if not candidates:
            raise NoAvailableAccountException("No active accounts available")
        
        # 加权随机选择
        session, _ = random.choices(
            candidates, 
            weights=[w for _, w in candidates]
        )[0]
        
        return session
    
    def release_account(self, account_id: str, success: bool):
        """释放账号并更新状态"""
        session = self._load_account(account_id)
        
        session.last_request_time = datetime.now()
        session.daily_request_count += 1
        session.total_requests += 1
        
        if success:
            # 成功: 提升健康分数
            session.health_score = min(1.0, session.health_score + 0.01)
        else:
            # 失败: 降低健康分数
            session.health_score = max(0.0, session.health_score - 0.05)
            
            # 连续失败触发冷却
            if session.health_score < 0.3:
                self.trigger_cooling(account_id, duration_hours=2)
        
        self._save_account(session)
    
    def trigger_cooling(self, account_id: str, duration_hours: int):
        """触发账号冷却"""
        session = self._load_account(account_id)
        session.status = 'cooling'
        session.cooling_until = datetime.now() + timedelta(hours=duration_hours)
        
        self.redis.srem('accounts:active', account_id)
        self.redis.sadd('accounts:cooling', account_id)
        
        logger.warning(f"Account {account_id} cooling until {session.cooling_until}")
        self._save_account(session)
```

#### 任务1.2: IP池核心实现
```python
# ip_pool.py
class IPPoolManager:
    def __init__(self, redis_client: Redis):
        self.redis = redis_client
        self.proxies: Dict[str, ProxyNode] = {}
        
    def add_proxy(self, proxy_url: str, region: str = 'US') -> str:
        """添加代理到池中"""
        proxy_id = f"proxy_{uuid.uuid4().hex[:8]}"
        node = ProxyNode(
            proxy_id=proxy_id,
            proxy_url=proxy_url,
            region=region,
            status='active',
            health_score=1.0
        )
        
        # 健康检查
        if self._health_check_proxy(node):
            self.redis.hset(f'proxy:{proxy_id}', mapping=node.to_dict())
            self.redis.sadd('proxies:active', proxy_id)
            logger.info(f"Added proxy {proxy_id} to pool")
            return proxy_id
        else:
            logger.error(f"Proxy {proxy_url} failed health check")
            raise InvalidProxyException()
    
    def get_proxy_for_account(self, account_id: str) -> ProxyNode:
        """为账号获取绑定的代理(长期绑定)"""
        # 查询绑定关系
        binding = self.redis.hget(f'account:{account_id}', 'proxy_id')
        
        if binding:
            return self._load_proxy(binding)
        else:
            # 首次分配: 选择负载最低的代理
            return self._assign_proxy_to_account(account_id)
    
    def _assign_proxy_to_account(self, account_id: str) -> ProxyNode:
        """为账号分配代理(长期绑定)"""
        active_ids = self.redis.smembers('proxies:active')
        
        # 选择绑定账号数最少的代理
        min_load = float('inf')
        selected_proxy = None
        
        for proxy_id in active_ids:
            proxy = self._load_proxy(proxy_id)
            load = len(proxy.bound_accounts)
            
            if load < min_load:
                min_load = load
                selected_proxy = proxy
        
        if not selected_proxy:
            raise NoAvailableProxyException()
        
        # 建立绑定关系
        selected_proxy.bound_accounts.append(account_id)
        self._save_proxy(selected_proxy)
        
        self.redis.hset(f'account:{account_id}', 'proxy_id', selected_proxy.proxy_id)
        
        logger.info(f"Bound account {account_id} to proxy {selected_proxy.proxy_id}")
        return selected_proxy
    
    def _health_check_proxy(self, proxy: ProxyNode) -> bool:
        """代理健康检查"""
        try:
            # 使用代理访问TikTok测试端点
            client = CurlCffiClient()
            client.session.proxies = {
                'http': proxy.proxy_url,
                'https': proxy.proxy_url
            }
            
            start_time = time.time()
            response = client.get('https://www.tiktok.com', timeout=10)
            latency = (time.time() - start_time) * 1000
            
            if response.status_code == 200:
                proxy.avg_latency = latency
                proxy.last_check_time = datetime.now()
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"Proxy {proxy.proxy_id} health check failed: {e}")
            return False
```

---

### Phase 2: 指纹隔离 (Week 3)

#### 任务2.1: 指纹池实现
```python
# fingerprint_pool.py
class FingerprintPoolManager:
    def __init__(self):
        self.fingerprints: Dict[str, BrowserFingerprint] = {}
        
    def generate_fingerprint(self, account_id: str) -> BrowserFingerprint:
        """为账号生成独特指纹"""
        fp_id = f"fp_{account_id}"
        
        # 从预定义设备模板中随机选择
        device_templates = [
            # MacBook Pro
            {
                'viewport': {'width': 1920, 'height': 1080},
                'user_agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)...',
                'webgl_renderer': 'ANGLE (Apple, Apple M1 Pro, OpenGL 4.1)',
                'cpu_cores': 10,
                'device_memory': 16
            },
            # Windows Desktop
            {
                'viewport': {'width': 2560, 'height': 1440},
                'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)...',
                'webgl_renderer': 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3080)',
                'cpu_cores': 16,
                'device_memory': 32
            },
            # MacBook Air
            {
                'viewport': {'width': 1440, 'height': 900},
                'user_agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)...',
                'webgl_renderer': 'ANGLE (Apple, Apple M2, OpenGL 4.1)',
                'cpu_cores': 8,
                'device_memory': 8
            }
        ]
        
        template = random.choice(device_templates)
        
        fingerprint = BrowserFingerprint(
            fingerprint_id=fp_id,
            **template,
            timezone='America/Los_Angeles',
            locale='en-US',
            canvas_noise=self._generate_canvas_noise()
        )
        
        self.fingerprints[fp_id] = fingerprint
        return fingerprint
    
    def _generate_canvas_noise(self) -> bytes:
        """生成Canvas指纹噪声"""
        # 为每个账号生成独特的Canvas噪声
        return os.urandom(32)
```

---

### Phase 3: 自动化养号 (Week 4)

#### 任务3.1: 账号自动登录
```python
# account_bootstrapper.py
class AccountBootstrapper:
    """批量账号登录与Cookie提取"""
    
    def __init__(self, cookie_pool: CookiePoolManager, ip_pool: IPPoolManager):
        self.cookie_pool = cookie_pool
        self.ip_pool = ip_pool
        
    async def bootstrap_accounts(self, accounts: List[dict]) -> List[str]:
        """批量登录账号
        
        Args:
            accounts: [{'username': 'xxx', 'password': 'xxx'}, ...]
        
        Returns:
            成功添加的account_id列表
        """
        results = []
        
        for account_info in accounts:
            try:
                # 1. 分配代理
                proxy_id = self.ip_pool.add_proxy(
                    proxy_url=self._get_next_proxy_from_provider(),
                    region='US'
                )
                
                # 2. 使用camoufox登录
                cookies = await self._login_with_browser(
                    username=account_info['username'],
                    password=account_info['password'],
                    proxy_id=proxy_id
                )
                
                # 3. 添加到Cookie池
                account_id = self.cookie_pool.add_account(
                    username=account_info['username'],
                    cookies=cookies,
                    proxy_id=proxy_id
                )
                
                results.append(account_id)
                logger.info(f"Bootstrapped account {account_info['username']}")
                
                # 4. 间隔30-60秒,避免批量检测
                await asyncio.sleep(random.uniform(30, 60))
                
            except Exception as e:
                logger.error(f"Failed to bootstrap {account_info['username']}: {e}")
                continue
        
        return results
    
    async def _login_with_browser(
        self, 
        username: str, 
        password: str, 
        proxy_id: str
    ) -> Dict[str, str]:
        """使用camoufox浏览器登录"""
        proxy = self.ip_pool._load_proxy(proxy_id)
        
        client = CamoufoxClient()
        # TODO: 配置代理到browser launch
        
        await client.start()
        
        # 导航到登录页
        await client.page.goto('https://www.tiktok.com/login/phone-or-email/email')
        
        # 填写表单
        await client.page.fill('input[name="username"]', username)
        await client.page.fill('input[type="password"]', password)
        await client.page.click('button[type="submit"]')
        
        # 等待登录成功(检测URL跳转或特定元素)
        await client.page.wait_for_url('https://www.tiktok.com/', timeout=30000)
        
        # 提取Cookie
        cookies = await client.get_cookies()
        
        await client.close()
        
        return cookies
```

#### 任务3.2: 每日健康检查
```python
# health_checker.py
class PoolHealthChecker:
    """池健康检查与自动恢复"""
    
    def __init__(self, cookie_pool: CookiePoolManager, ip_pool: IPPoolManager):
        self.cookie_pool = cookie_pool
        self.ip_pool = ip_pool
        
    async def check_all_accounts(self):
        """检查所有账号状态"""
        account_ids = self.cookie_pool.redis.smembers('accounts:active')
        
        for acc_id in account_ids:
            session = self.cookie_pool._load_account(acc_id)
            
            # 测试请求: 访问TikTok用户信息API
            is_alive = await self._test_account(session)
            
            if not is_alive:
                logger.warning(f"Account {acc_id} failed health check")
                self.cookie_pool.suspend_account(acc_id, reason='health_check_failed')
                
                # 尝试自动恢复(重新登录)
                await self._attempt_recovery(session)
            else:
                logger.info(f"Account {acc_id} health check passed")
    
    async def _test_account(self, session: AccountSession) -> bool:
        """测试账号是否存活"""
        try:
            proxy = self.ip_pool.get_proxy_for_account(session.account_id)
            
            client = CurlCffiClient()
            client.session.proxies = {
                'http': proxy.proxy_url,
                'https': proxy.proxy_url
            }
            
            headers = {
                'Cookie': '; '.join([f'{k}={v}' for k, v in session.cookies.items()])
            }
            
            response = client.get(
                'https://www.tiktok.com/api/user/info/',
                headers=headers
            )
            
            if response.status_code == 200:
                data = response.json()
                return data.get('statusCode') == 0
            
            return False
            
        except Exception as e:
            logger.error(f"Health check error for {session.account_id}: {e}")
            return False
    
    async def _attempt_recovery(self, session: AccountSession):
        """尝试恢复失效账号"""
        # TODO: 重新登录获取新Cookie
        # TODO: 如果多次失败,标记为permanently_failed
        pass
```

---

## 📊 监控与告警

### 关键指标

```python
# metrics.py
class PoolMetrics:
    def collect_metrics(self) -> dict:
        """收集池化指标"""
        return {
            # Cookie池指标
            'accounts_total': len(self.cookie_pool.accounts),
            'accounts_active': len(self.redis.smembers('accounts:active')),
            'accounts_cooling': len(self.redis.smembers('accounts:cooling')),
            'accounts_suspended': len(self.redis.smembers('accounts:suspended')),
            'avg_health_score': self._calculate_avg_health(),
            'daily_requests_total': self._get_daily_requests(),
            'daily_quota_utilization': self._get_quota_utilization(),
            
            # IP池指标
            'proxies_total': len(self.ip_pool.proxies),
            'proxies_active': len(self.redis.smembers('proxies:active')),
            'avg_proxy_latency': self._calculate_avg_latency(),
            
            # 业务指标
            'success_rate_24h': self._get_success_rate(hours=24),
            'waf_block_rate_1h': self._get_waf_rate(hours=1),
        }
```

### Grafana仪表盘

```yaml
# docs/monitoring/pool_dashboard.json
{
  "dashboard": {
    "title": "Cookie池 & IP池监控",
    "panels": [
      {
        "title": "账号状态分布",
        "type": "pie",
        "targets": [
          {
            "expr": "accounts_active",
            "legendFormat": "Active"
          },
          {
            "expr": "accounts_cooling",
            "legendFormat": "Cooling"
          },
          {
            "expr": "accounts_suspended",
            "legendFormat": "Suspended"
          }
        ]
      },
      {
        "title": "配额使用率",
        "type": "graph",
        "targets": [
          {
            "expr": "rate(daily_requests_total[5m])",
            "legendFormat": "请求速率"
          },
          {
            "expr": "daily_quota_utilization",
            "legendFormat": "配额利用率(%)"
          }
        ]
      },
      {
        "title": "WAF拦截率",
        "type": "graph",
        "targets": [
          {
            "expr": "rate(waf_block_count[1h])",
            "legendFormat": "WAF拦截/小时"
          }
        ],
        "alert": {
          "conditions": [
            {
              "evaluator": {
                "type": "gt",
                "params": [0.1]
              },
              "query": {
                "model": "rate(waf_block_count[1h])"
              }
            }
          ]
        }
      }
    ]
  }
}
```

---

## 🚀 部署与扩展

### 配置示例

```yaml
# config/pools.yaml
cookie_pool:
  max_accounts: 50              # 最大账号数
  min_active_accounts: 10       # 最小活跃账号数
  health_check_interval: 3600   # 健康检查间隔(秒)
  cooling_duration: 7200        # 冷却时长(秒)
  auto_recovery: true           # 自动恢复失效账号
  
  # 账号配额策略
  quota_strategy:
    initial_quota: 200          # 初始每日配额
    max_quota: 500              # 最大每日配额
    quota_increase_rate: 1.2    # 配额增长倍率(健康账号)
    quota_decrease_rate: 0.8    # 配额降低倍率(异常账号)

ip_pool:
  max_proxies: 100              # 最大代理数
  min_active_proxies: 20        # 最小活跃代理数
  health_check_interval: 1800   # 健康检查间隔(秒)
  
  # 代理绑定策略
  binding_strategy: 'long_term' # long_term(长期绑定) / rotate(定期轮换)
  rotation_interval: 86400      # 轮换间隔(秒, 仅rotate模式)
  max_accounts_per_proxy: 3     # 单代理最大账号数

scheduler:
  strategy: 'weighted'          # round_robin / least_loaded / weighted
  rebalance_interval: 300       # 负载均衡间隔(秒)
```

---

## 📝 使用示例

### 场景1: 规模化商品采集

```python
# scripts/batch_scrape_products.py
from src.pools import CookiePoolManager, IPPoolManager, PoolScheduler

async def batch_scrape_products(product_ids: List[str]):
    """批量采集商品(使用池化)"""
    
    cookie_pool = CookiePoolManager(redis_client)
    ip_pool = IPPoolManager(redis_client)
    scheduler = PoolScheduler(cookie_pool, ip_pool)
    
    results = []
    
    for product_id in product_ids:
        # 1. 获取账号
        account = scheduler.assign_task(
            Task(type='scrape_product', target=product_id)
        )
        
        # 2. 获取代理
        proxy = ip_pool.get_proxy_for_account(account.account_id)
        
        # 3. 发起请求
        try:
            scraper = ShopProductScraper(
                cookies=account.cookies,
                proxy=proxy.proxy_url
            )
            
            product_data = await scraper.scrape_product(product_id)
            results.append(product_data)
            
            # 4. 释放账号(成功)
            cookie_pool.release_account(account.account_id, success=True)
            
        except WAFBlockedException:
            # WAF拦截: 触发冷却
            cookie_pool.trigger_cooling(account.account_id, duration_hours=2)
            cookie_pool.release_account(account.account_id, success=False)
            
        except Exception as e:
            logger.error(f"Scrape failed: {e}")
            cookie_pool.release_account(account.account_id, success=False)
    
    return results

# 执行
product_ids = ['7123456789', '7234567890', ...]  # 2000个商品ID
results = asyncio.run(batch_scrape_products(product_ids))
```

---

## ⏱️ 实施时间表

| Week | 任务 | 交付物 |
|------|------|--------|
| Week 1 | Cookie池核心实现 | CookiePoolManager + Redis存储 |
| Week 2 | IP池核心实现 | IPPoolManager + 绑定逻辑 |
| Week 3 | 指纹池实现 | FingerprintPoolManager + 设备模板 |
| Week 4 | 自动化养号 | AccountBootstrapper + HealthChecker |
| Week 5 | 监控与告警 | Grafana Dashboard + 告警规则 |
| Week 6 | 集成测试 | 端到端测试 + 性能调优 |

---

## 🎯 预期效果

### 短期(1个月)
- 支持10个账号并发
- 单日配额: 2000商品 + 5000视频
- 账号存活率: >90%

### 中期(3个月)
- 支持30个账号并发
- 单日配额: 6000商品 + 15000视频
- 账号自动恢复率: >80%

### 长期(6个月)
- 支持50+账号并发
- 单日配额: 10000+商品
- 多区域覆盖(US/EU/APAC)

---

## ⚠️ 风险与缓解

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|---------|
| 批量账号关联封禁 | 中 | 高 | 指纹隔离 + IP长期绑定 + 行为差异化 |
| 代理成本过高 | 中 | 中 | 选择性价比高的住宅代理商 |
| 账号采购困难 | 低 | 中 | 建立多个账号来源渠道 |
| TikTok检测升级 | 高 | 高 | 持续跟进反检测技术 + 预留降级方案 |

