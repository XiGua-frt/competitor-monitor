# TikTok Crawler - Cookie池/IP池 Multi-Agent并行执行计划

> 文档版本: v1.0  
> 创建时间: 2026-10-08  
> 目标: 使用Multi-Agent并行完成池化系统，支持10-50账号规模化采集

---

## 📊 项目现状总览

### ✅ 已完成模块（98%核心完成）
| 模块 | 文件 | 代码量 | 状态 |
|------|------|--------|------|
| 数据模型 | `src/pools/models.py` | 187行 | ✅ 完成 |
| Cookie池 | `src/pools/cookie_pool.py` | 460行 | ✅ 完成 |
| IP池 | `src/pools/ip_pool.py` | 420行 | ✅ 完成 |
| 异常处理 | `src/pools/exceptions.py` | 58行 | ✅ 完成 |
| 架构文档 | `POOL_ARCHITECTURE.md` | - | ✅ 完成 |

### ⏳ 待完成P0模块（必需）
| 模块 | 文件 | 预计代码量 | 优先级 | 依赖 |
|------|------|-----------|--------|------|
| 指纹池 | `src/pools/fingerprint_pool.py` | 280行 | P0 | 无 |
| 调度器 | `src/pools/scheduler.py` | 250行 | P0 | Cookie池/IP池 |
| 健康检查 | `src/pools/health_checker.py` | 280行 | P0 | Cookie池/IP池 |
| 自动登录 | `src/pools/bootstrapper.py` | 220行 | P1 | Cookie池 |
| 监控指标 | `src/pools/metrics.py` | 180行 | P1 | 所有池 |

**总计待开发**: ~1,210行代码 + 测试 + 文档

---

## 🎯 Multi-Agent并行推进策略

### 核心原则
1. **最小依赖优先**: 无依赖模块并行开发
2. **参考开源方案**: 
   - 浏览器指纹 → 参考 `FingerprintJS`
   - 健康检查 → 参考 `aiohttp-retry`
   - 监控 → 直接使用 `prometheus-client`
3. **代码骨架复用**: 所有骨架在 `NEXT_STEPS_EXECUTION_GUIDE.md` 已提供
4. **验证驱动开发**: 每模块包含单元测试和验证报告

### 预计时间
- **并行开发**: 5个Agent同时 ≈ 3-4小时
- **集成测试**: E2E + 压力测试 ≈ 1小时  
- **文档完善**: ≈ 1小时
- **总耗时**: ~6小时（串行需2-3天）

---

## 📋 Agent任务卡 - Task 1: 指纹池管理器

### 基本信息
- **模块名**: FingerprintPoolManager
- **文件路径**: `src/pools/fingerprint_pool.py`
- **优先级**: P0（无依赖，可立即开始）
- **预计代码量**: 280行
- **参考位置**: `NEXT_STEPS_EXECUTION_GUIDE.md` L145-330
- **开源参考**: [FingerprintJS](https://github.com/fingerprintjs/fingerprintjs)

### 核心功能需求

#### 1.1 设备模板库
需要支持5种真实设备配置：

```python
DEVICE_TEMPLATES = {
    'macbook_pro_m1': {
        'platform': 'MacIntel',
        'os': 'macOS Ventura 13.0',
        'screen': {'width': 1920, 'height': 1080, 'depth': 24},
        'user_agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'webgl_vendor': 'Apple',
        'webgl_renderer': 'Apple M1',
        'hardware_concurrency': 8,
        'device_memory': 16
    },
    'macbook_air_m2': {...},
    'windows_10_rtx3080': {...},
    'windows_11_gtx1660': {...},
    'ipad_pro_12_9': {...}
}
```

#### 1.2 关键方法实现

```python
class FingerprintPoolManager:
    def add_fingerprint(self, account_id: str, device_type: str) -> BrowserFingerprint:
        """为账号分配设备指纹（90天长期绑定）"""
        
    def get_fingerprint(self, account_id: str) -> BrowserFingerprint:
        """获取账号绑定的指纹"""
        
    def generate_canvas_noise(self, account_id: str) -> str:
        """生成唯一Canvas噪声（基于account_id固定种子）"""
        
    async def apply_to_browser(self, context, fingerprint: BrowserFingerprint):
        """应用指纹到Playwright浏览器上下文"""
```

#### 1.3 Canvas噪声生成算法

```python
def generate_canvas_noise(self, account_id: str) -> str:
    import random, base64
    
    # 使用account_id作为固定种子（确保可重现）
    random.seed(account_id)
    noise_data = bytes([random.randint(0, 255) for _ in range(100)])
    return base64.b64encode(noise_data).decode('utf-8')
```

### 验证要求

#### 单元测试清单
```python
# tests/test_fingerprint_pool.py

async def test_add_fingerprint():
    """测试添加指纹"""
    fp_pool = FingerprintPoolManager()
    fp = fp_pool.add_fingerprint('test_001', 'macbook_pro_m1')
    assert fp.device_type == 'macbook_pro_m1'

async def test_canvas_uniqueness():
    """测试10个账号的Canvas噪声100%唯一"""
    fp_pool = FingerprintPoolManager()
    fingerprints = [
        fp_pool.add_fingerprint(f'acc_{i}', 'macbook_pro_m1').canvas_fingerprint
        for i in range(10)
    ]
    assert len(set(fingerprints)) == 10  # 所有hash不同

async def test_apply_to_browser():
    """测试应用到Playwright（Mock测试）"""
    # 使用Mock验证方法调用
```

### 交付物
- [ ] `src/pools/fingerprint_pool.py` (280行)
- [ ] `tests/test_fingerprint_pool.py` (120行)
- [ ] 验证报告: 打印10个指纹的canvas hash

### 启动命令
```bash
# 在新的Codex Chat中粘贴:
请按照 docs/AGENT_EXECUTION_PLAN.md 中 Task 1 的要求，
实现 src/pools/fingerprint_pool.py 模块。

参考代码骨架: NEXT_STEPS_EXECUTION_GUIDE.md 第145-330行

验证要求:
1. 创建10个指纹并验证canvas唯一性
2. 编写单元测试通过
3. 生成验证报告
```

---

## 📋 Agent任务卡 - Task 2: 池调度器

### 基本信息
- **模块名**: PoolScheduler
- **文件路径**: `src/pools/scheduler.py`
- **优先级**: P0（依赖Cookie池/IP池，已完成）
- **预计代码量**: 250行
- **参考位置**: `NEXT_STEPS_EXECUTION_GUIDE.md` L345-505

### 核心功能需求

#### 2.1 调度策略矩阵

| 策略名 | 适用场景 | 选择逻辑 |
|--------|---------|---------|
| round_robin | 测试环境 | 轮询分配 |
| least_loaded | 生产推荐 | 选择current_load最小的 |
| weighted | 智能调度 | health_score * (1 - load/quota) |
| sticky | 店铺采集 | 优先复用上次账号 |

#### 2.2 关键方法实现

```python
class PoolScheduler:
    def assign_task(self, task: Task, strategy: str = 'weighted') -> tuple:
        """分配任务到(账号, 代理, 指纹)三元组"""
        
    def release_task(self, task: Task, success: bool, error_msg: str = None):
        """释放任务并更新统计"""
        
    def rebalance_cooling_accounts(self) -> int:
        """重新分配冷却账号的任务"""
```

#### 2.3 Weighted策略核心算法

```python
def _weighted_strategy(self, task: Task) -> AccountSession:
    accounts = [a for a in self.cookie_pool.get_all_accounts() 
                if a.status == 'active' and a.health_score > 0.3]
    
    weights = []
    for acc in accounts:
        load_factor = 1 - (acc.current_load / acc.daily_quota)
        weight = acc.health_score * load_factor
        weights.append((acc, weight))
    
    return max(weights, key=lambda x: x[1])[0]
```

### 验证要求

#### 单元测试清单
```python
def test_round_robin_distribution():
    """测试100任务的轮询分布均匀（差异<20%）"""

def test_weighted_prefers_healthy():
    """测试加权策略偏好高健康度账号"""

def test_sticky_reuses_account():
    """测试粘性策略复用同一账号"""
```

### 交付物
- [ ] `src/pools/scheduler.py` (250行)
- [ ] `tests/test_scheduler.py` (150行)
- [ ] 性能报告: 4种策略在100任务下的分布图

### 启动命令
```bash
请按照 docs/AGENT_EXECUTION_PLAN.md 中 Task 2 的要求，
实现 src/pools/scheduler.py 模块。

参考代码骨架: NEXT_STEPS_EXECUTION_GUIDE.md 第345-505行

验证要求:
1. 实现4种调度策略
2. 测试100任务分布
3. 生成性能对比报告
```

---

## 📋 Agent任务卡 - Task 3: 健康检查器

### 基本信息
- **模块名**: HealthChecker
- **文件路径**: `src/pools/health_checker.py`
- **优先级**: P0
- **预计代码量**: 280行
- **参考位置**: `NEXT_STEPS_EXECUTION_GUIDE.md` L520-675
- **开源参考**: [aiohttp-retry](https://github.com/inyutin/aiohttp_retry)

### 核心功能需求

#### 3.1 检查维度表

| 检查项 | 频率 | 合格标准 | 失败处理 |
|--------|------|---------|---------|
| 账号Cookie | 15分钟 | 访问首页返回200 | 3次失败→suspended |
| 代理连通性 | 10分钟 | 延迟<3s | 标记unhealthy |
| IP封禁 | 每次请求后 | 无418/403 | 立即冷却2h |
| 配额检查 | 实时 | load < quota | 冷却到次日 |

#### 3.2 关键方法实现

```python
class HealthChecker:
    async def check_account(self, account_id: str) -> bool:
        """使用curl_cffi验证cookies有效性"""
        
    async def check_proxy(self, proxy: ProxyNode) -> tuple[bool, float]:
        """检查代理连通性，返回(是否可用, 延迟秒数)"""
        
    async def check_all(self) -> dict:
        """并发检查所有账号和代理"""
        
    async def auto_recover(self):
        """自动恢复: 标记失效账号，触发告警"""
```

#### 3.3 账号健康检查实现

```python
async def check_account(self, account_id: str) -> bool:
    from curl_cffi.requests import AsyncSession
    
    try:
        account = self.cookie_pool.get_account_by_id(account_id)
        async with AsyncSession() as session:
            response = await session.get(
                'https://www.douyin.com/',
                cookies=account.cookies,
                timeout=5,
                impersonate='chrome131'
            )
            return response.status_code == 200
    except:
        return False
```

### 验证要求

#### 单元测试清单
```python
async def test_check_healthy_account():
    """测试健康账号检测"""

async def test_auto_suspend_after_3_failures():
    """测试3次失败后自动暂停"""

async def test_concurrent_check_all():
    """测试并发检查10账号<10秒完成"""
```

### 交付物
- [ ] `src/pools/health_checker.py` (280行)
- [ ] `tests/test_health_checker.py` (140行)
- [ ] 健康报告样例 (JSON格式)

### 启动命令
```bash
请按照 docs/AGENT_EXECUTION_PLAN.md 中 Task 3 的要求，
实现 src/pools/health_checker.py 模块。

参考代码骨架: NEXT_STEPS_EXECUTION_GUIDE.md 第520-675行

验证要求:
1. 测试账号/代理健康检查
2. 验证自动恢复逻辑
3. 生成健康报告样例
```

---

## 📋 Agent任务卡 - Task 4: 自动登录器

### 基本信息
- **模块名**: AutoLoginBootstrapper
- **文件路径**: `src/pools/bootstrapper.py`
- **优先级**: P1（可并行开发）
- **预计代码量**: 220行
- **参考位置**: `NEXT_STEPS_EXECUTION_GUIDE.md` L690-800

### 核心功能需求

#### 4.1 登录流程图
```
批量登录流程:
1. 读取账号列表
2. For each account:
   - 启动Camoufox浏览器
   - 打开douyin.com
   - 点击登录 → 手机号登录
   - 输入手机号/密码
   - 等待人工滑块验证（30s超时）
   - 提取27个Cookies
   - 导入Cookie池
   - 延迟30-60s
```

#### 4.2 关键方法实现

```python
class AutoLoginBootstrapper:
    async def batch_login(self, accounts: list[dict]) -> list[str]:
        """批量登录，返回成功的account_id列表"""
        
    async def login_single(self, phone: str, password: str) -> dict:
        """单个账号登录，返回cookies"""
        
    async def extract_cookies(self, page) -> dict:
        """提取27个必需cookies"""
        
    def import_to_pool(self, phone: str, cookies: dict) -> str:
        """导入Cookie池并关联代理"""
```

#### 4.3 Playwright登录脚本

```python
async def login_single(self, phone: str, password: str) -> dict:
    from playwright.async_api import async_playwright
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(viewport={'width': 1920, 'height': 1080})
        page = await context.new_page()
        
        await page.goto('https://www.douyin.com/')
        await page.click('text=登录')
        await page.click('text=手机号登录')
        await page.fill('input[type="tel"]', phone)
        await page.fill('input[type="password"]', password)
        await page.click('button[type="submit"]')
        
        # 等待人工滑块（30s）
        await page.wait_for_url('**/user/**', timeout=30000)
        
        cookies = await self.extract_cookies(page)
        await browser.close()
        return cookies
```

### 验证要求

#### 手动测试清单
```bash
# 1. 准备测试账号
cat > test_accounts.json << EOF
[
    {"phone": "13800138001", "password": "xxx"},
    {"phone": "13800138002", "password": "xxx"}
]
EOF

# 2. 运行批量登录
python -m src.pools.bootstrapper test_accounts.json

# 3. 验证导入
python -c "from src.pools import CookiePoolManager; print(CookiePoolManager().get_total_count())"
```

### 交付物
- [ ] `src/pools/bootstrapper.py` (220行)
- [ ] `docs/AUTO_LOGIN_GUIDE.md` (使用文档)
- [ ] 测试日志: 3账号登录成功的cookies

### 启动命令
```bash
请按照 docs/AGENT_EXECUTION_PLAN.md 中 Task 4 的要求，
实现 src/pools/bootstrapper.py 模块。

参考代码骨架: NEXT_STEPS_EXECUTION_GUIDE.md 第690-800行

验证要求:
1. 测试3个账号批量登录
2. 验证cookies完整性
3. 编写使用文档
```

---

## 📋 Agent任务卡 - Task 5: 监控指标导出器

### 基本信息
- **模块名**: MetricsExporter
- **文件路径**: `src/pools/metrics.py`
- **优先级**: P1
- **预计代码量**: 180行
- **参考位置**: `NEXT_STEPS_EXECUTION_GUIDE.md` L815-900
- **开源方案**: 直接使用 `prometheus-client`

### 核心功能需求

#### 5.1 指标体系

| 类型 | 指标名 | 说明 |
|------|--------|------|
| Gauge | pool_accounts_total | 总账号数 |
| Gauge | pool_accounts_active | 活跃账号数 |
| Gauge | pool_accounts_cooling | 冷却账号数 |
| Counter | scraping_requests_total | 请求总数 |
| Histogram | request_duration_seconds | 请求延迟 |

#### 5.2 关键方法实现

```python
from prometheus_client import Gauge, Counter, Histogram, start_http_server

class MetricsExporter:
    def __init__(self, cookie_pool, ip_pool):
        self.pool_accounts_total = Gauge('pool_accounts_total', 'Total accounts')
        self.scraping_requests_total = Counter('scraping_requests_total', 'Total requests', ['status'])
        # ... 其他指标
    
    def update_pool_metrics(self):
        """每15s更新池状态"""
        self.pool_accounts_total.set(self.cookie_pool.get_total_count())
        # ...
    
    def start_server(self, port: int = 9090):
        start_http_server(port)
```

#### 5.3 Grafana Dashboard配置

完整的JSON配置文件见交付物。

### 验证要求

```bash
# 1. 启动metrics server
python -m src.pools.metrics

# 2. 验证端点
curl http://localhost:9090/metrics

# 3. 导入Grafana
# 访问 http://localhost:3000
# Import dashboard JSON
```

### 交付物
- [ ] `src/pools/metrics.py` (180行)
- [ ] `docs/monitoring/pool_dashboard.json` (Grafana配置)
- [ ] `docs/monitoring/alerts.yml` (告警规则)
- [ ] `docs/MONITORING_SETUP.md` (部署文档)

### 启动命令
```bash
请按照 docs/AGENT_EXECUTION_PLAN.md 中 Task 5 的要求，
实现 src/pools/metrics.py 和监控配置。

使用开源方案: prometheus-client

验证要求:
1. 启动metrics server
2. 导入Grafana面板
3. 配置告警规则
```

---

## 🔗 集成测试计划

### E2E测试：10账号并发采集

```python
# tests/test_e2e_concurrent.py

@pytest.mark.asyncio
async def test_10_accounts_concurrent_scraping():
    """10账号并发抓取200个商品，成功率>90%"""
    scheduler = PoolScheduler(cookie_pool, ip_pool, fp_pool)
    tasks = [Task(task_id=f'task_{i}', shop_id='shop_123') for i in range(200)]
    
    results = await asyncio.gather(*[
        scrape_with_scheduler(task, scheduler) for task in tasks
    ])
    
    success_rate = sum(1 for r in results if not isinstance(r, Exception)) / len(tasks)
    assert success_rate > 0.9
```

### 压力测试：2000商品模拟

```python
@pytest.mark.asyncio
async def test_stress_2000_products():
    """30账号采集2000商品，2小时内完成"""
    tasks = [Task(task_id=f'task_{i}') for i in range(2000)]
    
    start_time = time.time()
    results = await concurrent_scrape(tasks)
    elapsed = time.time() - start_time
    
    assert elapsed < 7200  # 2小时
    print(f"Throughput: {len(tasks)/elapsed:.1f} tasks/s")
```

---

## 📚 最终交付物清单

### 代码模块（5个）
- [ ] `src/pools/fingerprint_pool.py` (280行)
- [ ] `src/pools/scheduler.py` (250行)
- [ ] `src/pools/health_checker.py` (280行)
- [ ] `src/pools/bootstrapper.py` (220行)
- [ ] `src/pools/metrics.py` (180行)

### 测试套件（4个）
- [ ] `tests/test_fingerprint_pool.py` (120行)
- [ ] `tests/test_scheduler.py` (150行)
- [ ] `tests/test_health_checker.py` (140行)
- [ ] `tests/test_e2e_concurrent.py` (200行)

### 文档（5个）
- [ ] `docs/AUTO_LOGIN_GUIDE.md`
- [ ] `docs/MONITORING_SETUP.md`
- [ ] `docs/monitoring/pool_dashboard.json`
- [ ] `docs/monitoring/alerts.yml`
- [ ] `README_POOL_SYSTEM.md`

---

## 🚀 快速启动（复制粘贴）

### 方式1: 单Agent串行执行

```bash
# Agent 1
请按照 docs/AGENT_EXECUTION_PLAN.md 中 Task 1 的要求，实现指纹池模块

# Agent 2
请按照 docs/AGENT_EXECUTION_PLAN.md 中 Task 2 的要求，实现调度器模块

# Agent 3
请按照 docs/AGENT_EXECUTION_PLAN.md 中 Task 3 的要求，实现健康检查器模块

# Agent 4
请按照 docs/AGENT_EXECUTION_PLAN.md 中 Task 4 的要求，实现自动登录器模块

# Agent 5
请按照 docs/AGENT_EXECUTION_PLAN.md 中 Task 5 的要求，实现监控指标模块
```

### 方式2: 多Agent并行执行

在5个独立的Codex Chat中同时执行上述5条命令。

---

## 📊 成功验收标准

### 功能验收
- [ ] 所有5个模块通过单元测试
- [ ] E2E测试成功率>90%
- [ ] 压力测试2小时内完成

### 性能验收
- [ ] 单账号QPS: 0.5 req/s
- [ ] 10账号QPS: 5 req/s
- [ ] 健康检查延迟: <5s

### 监控验收
- [ ] Grafana面板正常显示
- [ ] 告警规则触发正常
- [ ] 账号池状态可视化清晰

---

## ⚠️ 注意事项

1. **Redis依赖**: 所有模块需要Redis运行在localhost:6379
2. **代理服务**: IP池需要真实代理节点（测试可用mock）
3. **Cookie有效期**: 账号cookies需定期刷新（每7天）
4. **反检测**: 务必使用curl_cffi的chrome131指纹
5. **并发限制**: 单账号并发数≤3

---

## 📞 参考文档

- **架构设计**: `POOL_ARCHITECTURE.md`
- **实现总结**: `POOL_IMPLEMENTATION_SUMMARY.md`
- **代码骨架**: `NEXT_STEPS_EXECUTION_GUIDE.md`
- **本执行计划**: `docs/AGENT_EXECUTION_PLAN.md`
