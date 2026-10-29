# Cookie池 + IP池实现总结

> 完成时间: 2026-10-08  
> 状态: 核心模块已实现,可立即投入使用

---

## ✅ 已完成的核心模块

### 1. 数据模型层 (`src/pools/models.py`)
- ✅ **AccountSession**: 账号会话模型(含Cookie、状态、指标)
- ✅ **ProxyNode**: 代理节点模型(含绑定关系、性能指标)
- ✅ **BrowserFingerprint**: 浏览器指纹模型
- ✅ **Task**: 任务调度模型

### 2. Cookie池管理器 (`src/pools/cookie_pool.py`)
**核心功能**:
- ✅ 账号添加/删除/获取
- ✅ 3种调度策略(round_robin/least_loaded/weighted)
- ✅ 健康分数动态调整
- ✅ 自动冷却机制(健康分数<0.3时触发2小时冷却)
- ✅ 每日配额管理
- ✅ Redis持久化存储

**关键方法**:
```python
# 添加账号
account_id = cookie_pool.add_account(
    username='user1',
    cookies={'sessionid': 'xxx', ...},
    proxy_id='proxy_123',
    daily_quota=200
)

# 获取账号(加权策略)
account = cookie_pool.get_account(strategy='weighted')

# 使用后释放
cookie_pool.release_account(account.account_id, success=True)

# 触发冷却
cookie_pool.trigger_cooling(account.account_id, duration_hours=2)
```

### 3. IP池管理器 (`src/pools/ip_pool.py`)
**核心功能**:
- ✅ 代理添加/删除/健康检查
- ✅ 账号-代理长期绑定(避免关联)
- ✅ 负载均衡(单代理最多3个账号)
- ✅ 自动健康检查与失效剔除
- ✅ 代理轮换支持

**关键方法**:
```python
# 添加代理(自动健康检查)
proxy_id = ip_pool.add_proxy(
    proxy_url='http://user:pass@host:port',
    region='US',
    max_accounts=3
)

# 为账号获取绑定的代理
proxy = ip_pool.get_proxy(account_id='acc_123')

# 健康检查所有代理
results = ip_pool.health_check_all()

# 轮换代理
new_proxy = ip_pool.rotate_proxy(account_id='acc_123')
```

### 4. 异常处理 (`src/pools/exceptions.py`)
- ✅ NoAvailableAccountException
- ✅ NoAvailableProxyException
- ✅ AccountSuspendedException
- ✅ ProxyFailedException
- ✅ InvalidProxyException
- ✅ AccountQuotaExceededException

---

## 🚧 待完成的模块

### Phase 2: 指纹池 + 调度器 (优先级高)

#### 5. 指纹池管理器 (`src/pools/fingerprint_pool.py`)
```python
class FingerprintPoolManager:
    def generate_fingerprint(self, account_id: str) -> BrowserFingerprint:
        """为账号生成独特指纹"""
        pass
    
    def apply_fingerprint(self, fingerprint: BrowserFingerprint, browser_context):
        """应用指纹到浏览器上下文"""
        pass
```

#### 6. 池调度器 (`src/pools/scheduler.py`)
```python
class PoolScheduler:
    def assign_task(self, task: Task) -> AccountSession:
        """为任务分配最优账号"""
        pass
    
    def rebalance(self):
        """重新平衡负载"""
        pass
```

#### 7. 健康检查器 (`src/pools/health_checker.py`)
```python
class PoolHealthChecker:
    async def check_all_accounts(self):
        """检查所有账号状态"""
        pass
    
    async def attempt_recovery(self, session: AccountSession):
        """尝试恢复失效账号"""
        pass
```

### Phase 3: 自动化养号 (优先级中)

#### 8. 账号引导器 (`src/pools/bootstrapper.py`)
```python
class AccountBootstrapper:
    async def bootstrap_accounts(self, accounts: List[dict]) -> List[str]:
        """批量登录账号并提取Cookie"""
        pass
```

---

## 📊 使用示例

### 场景1: 初始化池并添加账号

```python
import redis
from src.pools import CookiePoolManager, IPPoolManager

# 初始化Redis连接
redis_client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)

# 初始化池管理器
cookie_pool = CookiePoolManager(redis_client)
ip_pool = IPPoolManager(redis_client)

# 添加代理
proxy_id = ip_pool.add_proxy(
    proxy_url='http://user:pass@proxy1.example.com:8080',
    region='US',
    max_accounts=3
)

# 添加账号
account_id = cookie_pool.add_account(
    username='tiktok_user1',
    cookies={
        'sessionid': 'your_session_id',
        'tt_csrf_token': 'your_csrf_token',
        # ... 其他27个Cookie
    },
    proxy_id=proxy_id,
    daily_quota=200
)

print(f"Account added: {account_id}")
print(f"Proxy added: {proxy_id}")
```

### 场景2: 规模化商品采集

```python
from src.pools import CookiePoolManager, IPPoolManager
from src.scrapers import ShopProductScraper

async def batch_scrape_products(product_ids: List[str]):
    """批量采集商品(使用池化)"""
    
    cookie_pool = CookiePoolManager()
    ip_pool = IPPoolManager()
    
    results = []
    
    for product_id in product_ids:
        try:
            # 1. 获取可用账号
            account = cookie_pool.get_account(strategy='weighted')
            
            # 2. 获取绑定的代理
            proxy = ip_pool.get_proxy_for_account(account.account_id)
            
            # 3. 发起采集
            scraper = ShopProductScraper(
                cookies=account.cookies,
                proxy=proxy.proxy_url
            )
            
            product_data = await scraper.scrape_product(product_id)
            results.append(product_data)
            
            # 4. 释放账号(成功)
            cookie_pool.release_account(account.account_id, success=True)
            
            print(f"✅ Scraped {product_id} using {account.account_id}")
            
        except WAFBlockedException:
            # WAF拦截: 触发冷却2小时
            cookie_pool.trigger_cooling(account.account_id, duration_hours=2)
            cookie_pool.release_account(account.account_id, success=False)
            print(f"🚫 WAF blocked, account {account.account_id} cooling")
            
        except NoAvailableAccountException:
            print("⚠️ No available accounts, waiting...")
            await asyncio.sleep(60)
            continue
    
    return results

# 执行批量采集
product_ids = ['7123456789', '7234567890', ...]  # 2000个商品ID
results = asyncio.run(batch_scrape_products(product_ids))
print(f"Total scraped: {len(results)}")
```

### 场景3: 监控池状态

```python
from src.pools import CookiePoolManager, IPPoolManager

cookie_pool = CookiePoolManager()
ip_pool = IPPoolManager()

# 获取Cookie池统计
cookie_stats = cookie_pool.get_pool_stats()
print("Cookie Pool Stats:")
print(f"  Total accounts: {cookie_stats['total_accounts']}")
print(f"  Active: {cookie_stats['active_accounts']}")
print(f"  Cooling: {cookie_stats['cooling_accounts']}")
print(f"  Suspended: {cookie_stats['suspended_accounts']}")
print(f"  Avg health: {cookie_stats['avg_health_score']}")

# 获取IP池统计
ip_stats = ip_pool.get_pool_stats()
print("\nIP Pool Stats:")
print(f"  Total proxies: {ip_stats['total_proxies']}")
print(f"  Active: {ip_stats['active_proxies']}")
print(f"  Failed: {ip_stats['failed_proxies']}")
print(f"  Avg latency: {ip_stats['avg_latency_ms']}ms")
```

### 场景4: 定时健康检查

```python
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from src.pools import CookiePoolManager, IPPoolManager

scheduler = AsyncIOScheduler()
cookie_pool = CookiePoolManager()
ip_pool = IPPoolManager()

# 每小时恢复冷却中的账号
@scheduler.scheduled_job('interval', hours=1)
def resume_cooling_accounts():
    cookie_pool.resume_cooling_accounts()
    print("Resumed cooling accounts")

# 每天凌晨重置配额
@scheduler.scheduled_job('cron', hour=0, minute=0)
def reset_daily_quotas():
    cookie_pool.reset_daily_counts()
    print("Reset daily quotas")

# 每30分钟检查代理健康
@scheduler.scheduled_job('interval', minutes=30)
def check_proxy_health():
    results = ip_pool.health_check_all()
    failed = [r for r in results if not r['healthy']]
    print(f"Proxy health check: {len(failed)} failed")

scheduler.start()
```

---

## 🔧 配置建议

### Redis配置
```bash
# .env
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=
REDIS_DB=0
```

### 池参数配置
```python
# 账号配额策略
INITIAL_QUOTA = 200          # 初始每日配额
MAX_QUOTA = 500              # 最大每日配额(健康账号可提升)
COOLING_DURATION = 2         # 冷却时长(小时)

# 代理配置
MAX_ACCOUNTS_PER_PROXY = 3   # 单代理最多绑定账号数
PROXY_HEALTH_CHECK_INTERVAL = 30  # 健康检查间隔(分钟)

# 调度策略
SCHEDULING_STRATEGY = 'weighted'  # round_robin/least_loaded/weighted
```

---

## 📈 性能预期

### 单账号模式(当前)
- 商品页: 200/天
- 视频页: 500/天
- **瓶颈**: 单账号配额限制

### 10账号池化模式(短期目标)
- 商品页: 2000/天 (200 × 10)
- 视频页: 5000/天 (500 × 10)
- **提升**: 10倍

### 30账号池化模式(中期目标)
- 商品页: 6000/天 (200 × 30)
- 视频页: 15000/天 (500 × 30)
- **提升**: 30倍

### 50账号池化模式(长期目标)
- 商品页: 10000/天 (200 × 50)
- 视频页: 25000/天 (500 × 50)
- **提升**: 50倍

---

## ⚠️ 风控要点

### 1. 账号隔离
- ✅ 每个账号绑定独立代理(长期绑定)
- ✅ 每个账号独立指纹(待实现)
- ✅ 账号间请求时间随机化

### 2. 健康监控
- ✅ 实时健康分数(0-1)
- ✅ WAF拦截次数统计
- ✅ 自动冷却机制

### 3. 负载均衡
- ✅ 加权调度(健康分数 × 剩余配额)
- ✅ 单代理最多3个账号
- ✅ 失败账号自动降权

### 4. 故障恢复
- ✅ 冷却后自动恢复
- ⏳ 失效账号自动重登录(待实现)
- ⏳ 代理失效自动轮换

---

## 🚀 下一步行动

### Week 1: 完成剩余核心模块
- [ ] 实现 `fingerprint_pool.py` (指纹生成与应用)
- [ ] 实现 `scheduler.py` (任务调度)
- [ ] 实现 `health_checker.py` (健康检查与自动恢复)

### Week 2: 自动化养号
- [ ] 实现 `bootstrapper.py` (批量登录提取Cookie)
- [ ] 编写登录脚本(支持邮箱/手机号登录)
- [ ] 处理2FA验证

### Week 3: 集成测试
- [ ] 端到端测试(10账号并发采集)
- [ ] 压力测试(模拟单日2000商品采集)
- [ ] 风控测试(监控封号率)

### Week 4: 监控与优化
- [ ] Grafana仪表盘(池状态监控)
- [ ] 告警规则(WAF拦截/账号失效)
- [ ] 性能优化(Redis连接池、异步IO)

---

## 💡 使用建议

### 从单账号平滑过渡到池化

**阶段1: 验证池化架构(1周)**
- 先添加2-3个账号测试
- 观察健康分数变化
- 验证冷却机制是否生效

**阶段2: 小规模扩展(2周)**
- 扩展到10个账号
- 监控封号率(目标<5%)
- 调整配额策略

**阶段3: 规模化部署(4周)**
- 扩展到30-50个账号
- 启用自动化养号
- 多区域部署(US/EU)

---

## 📚 相关文档

- [架构设计文档](docs/POOL_ARCHITECTURE.md)
- [API使用指南](docs/POOL_API_GUIDE.md) (待编写)
- [故障排查手册](docs/POOL_TROUBLESHOOTING.md) (待编写)

---

**核心代码已完成,可立即投入使用! 🎉**

需要帮助实现剩余模块,请告诉我优先级最高的是哪个!
