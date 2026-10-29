# TKCrawler 账号池系统总览

> 更新时间: 2026-10-08
> 代码位置: `src/pools/`
> 相关文档: [docs/POOL_ARCHITECTURE.md](docs/POOL_ARCHITECTURE.md) · [docs/MONITORING_SETUP.md](docs/MONITORING_SETUP.md)

账号池系统为 TikTok 规模化采集提供账号隔离、故障容错、负载均衡与健康监控能力。目标：**10 账号并发采集 200 商品 < 30 分钟，账号故障自动切换 < 10 秒**。

---

## 架构图

```
                        ┌────────────────────────────────────┐
                        │        调用方 (scrapers/main.py)     │
                        └──────────────┬─────────────────────┘
                                       │ Task(task_id, task_type, target)
                        ┌──────────────▼─────────────────────┐
                        │   PoolScheduler (scheduler.py)      │
                        │  assign_task() -> (账号, 代理)        │
                        │  策略: round_robin/least_loaded/     │
                        │       weighted/sticky               │
                        └──────┬─────────────────────┬───────┘
                               │                     │
                 ┌─────────────▼─────────┐ ┌─────────▼───────────┐
                 │ CookiePoolManager      │ │ IPPoolManager        │
                 │ (cookie_pool.py)       │ │ (ip_pool.py)         │
                 │ 账号增删/调度/冷却/配额  │ │ 代理增删/账号长期绑定  │
                 └─────────┬──────────────┘ └─────────┬───────────┘
                           │   account.proxy_id        │
                 ┌─────────▼───────────────────────────▼───────────┐
                 │              FingerprintPoolManager              │
                 │              (fingerprint_pool.py)               │
                 │        设备模板 + Canvas 噪声 + 时区/locale        │
                 └───────────────────────────┬─────────────────────┘
                                             │
                        ┌────────────────────▼─────────────────────┐
                        │  HealthChecker (health_checker.py)        │
                        │  check_all() / auto_recover()             │
                        │  curl_cffi 探活 + 失败自动恢复              │
                        └────────────────────┬─────────────────────┘
                                             │
        ┌────────────────────────┐   ┌───────▼──────────────────┐
        │ AutoLoginBootstrapper   │   │ PoolMetricsExporter       │
        │ (bootstrapper.py)       │   │ (metrics.py)              │
        │ camoufox 批量登录       │   │ /metrics :9090            │
        │ Cookie 提取→导入池      │   │ Prometheus + Grafana      │
        └────────────────────────┘   └───────────────────────────┘

                     所有池状态持久化于 Redis (docker-compose redis 服务)
```

数据流向：调度器按策略选账号 → 账号绑定唯一代理 + 唯一指纹（防关联）→ 请求结果回写健康分与配额 → 健康检查器周期探活并自动恢复 → 指标导出器将池状态暴露给 Prometheus。

---

## 模块与核心类

### 1. Cookie 账号池 — `src/pools/cookie_pool.py`

**类: `CookiePoolManager(redis_client=None)`**（redis_client 为空时按 `src/common/config.py` 中的 REDIS_* 配置自建连接）

- 管理 `AccountSession` 账号会话，Redis 持久化（`account:{id}` hash + `accounts:active/cooling/suspended` set）
- 健康分机制：成功 +0.01 / 失败 -0.05，health_score < 0.3 自动冷却 2 小时
- 配额管理：每账号默认 `daily_quota=200` 请求/天，`reset_daily_counts()` 每日重置

| 方法 | 说明 |
|------|------|
| `add_account(username, cookies, proxy_id, fingerprint_id='', daily_quota=200) -> str` | 添加账号，返回 `acc_xxxxxxxx` |
| `remove_account(account_id) -> bool` | 移除账号 |
| `get_account(strategy='weighted') -> AccountSession` | 按策略取可用账号（round_robin / least_loaded / weighted） |
| `release_account(account_id, success, response_time=0)` | 归还账号并更新健康分/计数 |
| `trigger_cooling(account_id, duration_hours=2)` | 触发冷却 |
| `resume_cooling_accounts()` | 恢复冷却到期的账号 |
| `suspend_account(account_id, reason='')` | 挂起账号 |
| `get_pool_stats() -> dict` | 池统计 |

### 2. IP 代理池 — `src/pools/ip_pool.py`

**类: `IPPoolManager(redis_client=None)`**

- 代理入库前强制健康检查（`ProxyStatus.TESTING -> ACTIVE`），失败抛 `ProxyFailedException`
- 账号-代理**长期绑定**（防关联），单代理默认最多绑定 3 个账号（`max_accounts=3`）

| 方法 | 说明 |
|------|------|
| `add_proxy(proxy_url, proxy_type='http', region='US', max_accounts=3) -> str` | 添加代理，返回 `proxy_xxxxxxxx` |
| `remove_proxy(proxy_id) -> bool` | 移除代理 |
| `get_proxy(account_id=None) -> ProxyNode` | 取绑定代理或分配新代理 |
| `bind_proxy_to_account(proxy_id, account_id) -> bool` | 建立绑定 |
| `unbind_proxy_from_account(account_id) -> bool` | 解除绑定 |
| `get_bound_proxy_id(account_id) -> Optional[str]` | 查询绑定关系 |
| `health_check_all() -> List[dict]` | 全量代理探活 |
| `rotate_proxy(account_id) -> ProxyNode` | 为账号轮换代理 |
| `get_pool_stats() -> dict` | 池统计 |

### 3. 浏览器指纹池 — `src/pools/fingerprint_pool.py`

**类: `FingerprintPoolManager(redis_client=None)`**

- 内置设备模板（`DEVICE_TEMPLATES`，含 UA/viewport/WebGL/CPU/内存）+ 随机 timezone/locale
- 每账号一个指纹（`fingerprint_id = fp_{account_id}`），Canvas 噪声独立生成（`_generate_canvas_noise()`）
- 可直接产出 Playwright 浏览器上下文参数

| 方法 | 说明 |
|------|------|
| `add_fingerprint(account_id, device_type=None) -> BrowserFingerprint` | 生成/重生成指纹 |
| `get_fingerprint(account_id) -> Optional[BrowserFingerprint]` | 按账号取指纹 |
| `remove_fingerprint(account_id) -> bool` | 删除指纹 |
| `apply_to_browser(...)` / `get_context_options(fingerprint) -> dict` | 应用到浏览器上下文 |
| `get_all_fingerprints() -> Dict[str, BrowserFingerprint]` | 全量指纹 |
| `get_statistics() -> dict` | 指纹统计 |

### 4. 池调度器 — `src/pools/scheduler.py`

**类: `PoolScheduler(cookie_pool, ip_pool, default_strategy='weighted')`**，任务模型 `Task(task_id, task_type, target, priority=0, ...)`

- 4 种策略：`round_robin` / `least_loaded` / `weighted`（按健康分 × 剩余配额加权）/ `sticky`（同类任务粘住同一账号）
- 分配时自动跳过冷却账号并校验代理健康度（`_find_healthy_proxy`）

| 方法 | 说明 |
|------|------|
| `assign_task(task, strategy=None) -> Tuple[AccountSession, ProxyNode]` | 分配账号+代理；无可用资源抛 `NoAvailableAccountException` / `NoAvailableProxyException` |
| `release_task(...)` | 归还任务结果 |
| `rebalance_cooling_accounts() -> int` | 把冷却账号上的在途任务重分配 |
| `get_scheduler_stats() -> dict` | 调度统计 |

### 5. 健康检查器 — `src/pools/health_checker.py`

**类: `HealthChecker(cookie_pool, ip_pool, redis_client=None)`**

- curl_cffi `AsyncSession` 探活：账号超时 10s、代理超时 5s；延迟 > 3s 判定不健康、> 5s 判定严重异常
- 自动恢复：账号连续失败 3 次（`ACCOUNT_FAIL_THRESHOLD`）触发恢复流程；并发检查上限 10（`MAX_CONCURRENT_CHECKS`）
- 报告按 7 天留存写入 Redis，供 Grafana/排查使用

| 方法 | 说明 |
|------|------|
| `await check_account(account_id) -> Tuple[bool, float]` | 单账号探活（是否健康, 响应秒数） |
| `await check_proxy(proxy) -> Tuple[bool, float]` | 单代理探活 |
| `await check_all() -> Dict` | 全量检查（账号 + 代理） |
| `await auto_recover() -> Dict` | 失败账号自动恢复 |
| `get_recent_reports(limit=10) / get_health_summary() -> Dict` | 历史报告与摘要 |

### 6. 自动登录引导器 — `src/pools/bootstrapper.py`

**类: `AutoLoginBootstrapper(cookie_pool, ip_pool, headless=False, save_cookies_dir=None)`**（依赖 `camoufox`，未安装则构造时抛 `ImportError`）

- 手机号 + 密码登录（`LOGIN_CONFIG`），验证码环节默认无头关闭以便人工介入，等待 30s
- 登录后提取核心 Cookie（`REQUIRED_COOKIES` 27 个关键校验字段），备份至 `./exports/cookies/`
- 登录间默认串行（`max_concurrent=1`），登录后延迟 30-60s 防批量检测

| 方法 | 说明 |
|------|------|
| `await batch_login(accounts, max_concurrent=1) -> Dict[str, str]` | 批量登录，返回 `{phone: account_id}` |
| `await import_to_pool(...)` | 将登录成功的 Cookie 导入 Cookie 池并关联代理 |
| `await verify_login_status(account_id) -> bool` | 复验登录态 |
| `await bootstrap_accounts_from_file(...)`（模块级函数） | 从文件批量引导 |

### 7. 监控指标导出器 — `src/pools/metrics.py`

**类: `PoolMetricsExporter(cookie_pool=None, ip_pool=None, update_interval=15)`**

- 聚合 Cookie 池 / IP 池状态为 Prometheus 指标，后台线程每 15s 自动刷新
- `record_request(scraper, duration, success, waf_blocked=False, rate_limited=False)` 记录请求；`track_request(scraper)` 上下文管理器自动计时 + WAF/限流识别
- `start_server(port=None, addr='0.0.0.0', auto_update=True)` 启动 `/metrics` HTTP 服务，默认端口 9090（可用环境变量 `POOL_METRICS_PORT` 覆盖）
- 模块级便捷函数：`get_exporter(...)`（全局单例）、`start_metrics_server(...)`

指标清单与部署方式见 **[docs/MONITORING_SETUP.md](docs/MONITORING_SETUP.md)**。

---

## 数据模型（`src/pools/models.py`）

| 模型 | 说明 |
|------|------|
| `AccountSession` | 账号会话：cookies、proxy_id、fingerprint_id、status（active/cooling/suspended/failed）、health_score、daily_quota、daily_request_count、风控计数等；提供 `is_available()` / `get_weight()` |
| `ProxyNode` | 代理节点：proxy_url、proxy_type、region、status、avg_latency、success_rate、bound_accounts |
| `BrowserFingerprint` | 指纹：user_agent、viewport、timezone、locale、webgl_vendor/renderer、canvas_noise、cpu_cores、device_memory 等 |
| `Task` | 调度任务：task_id、task_type、target、priority、assigned_account/assigned_proxy |

**异常**（`src/pools/exceptions.py`，基类 `PoolException`）：`NoAvailableAccountException`、`NoAvailableProxyException`、`AccountSuspendedException`、`ProxyFailedException`、`InvalidProxyException`、`AccountQuotaExceededException`（前四个由包导出）。

---

## 快速开始

### 1. 安装依赖

```bash
cd /Users/wuwenbo/Documents/TK爬虫/TKCrawler
pip install -r requirements.txt
# 指标导出需要 prometheus_client（requirements 已含）
# 自动登录需要 camoufox：pip install "camoufox>=0.4.0"
```

### 2. 启动 Redis / PostgreSQL

```bash
# 只起存储层（账号池仅强依赖 Redis）
docker compose up -d redis postgres

# 验证
docker exec tkcrawler_redis redis-cli ping   # PONG
```

Redis 连接参数通过环境变量/`src/common/config.py` 提供：`REDIS_HOST`（默认 localhost）、`REDIS_PORT=6379`、`REDIS_PASSWORD`、`REDIS_DB`。

### 3. 最小可运行示例

```python
import asyncio
import redis
from src.pools import (
    CookiePoolManager, IPPoolManager, PoolScheduler, FingerprintPoolManager,
    PoolMetricsExporter,
)
from src.pools.scheduler import Task

r = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)

# 1. 初始化池
cookie_pool = CookiePoolManager(redis_client=r)
ip_pool = IPPoolManager(redis_client=r)
fp_pool = FingerprintPoolManager(redis_client=r)

# 2. 添加代理与账号（add_proxy 会先做真实健康检查）
proxy_id = ip_pool.add_proxy('http://user:pass@proxy-host:port', region='US')
account_id = cookie_pool.add_account(
    username='my_account',
    cookies={'sessionid': 'xxx', 'ttwid': 'yyy'},
    proxy_id=proxy_id,
    daily_quota=200,
)
fp_pool.add_fingerprint(account_id)

# 3. 调度任务
scheduler = PoolScheduler(cookie_pool, ip_pool, default_strategy='weighted')
account, proxy = scheduler.assign_task(Task('t1', 'scrape_product', 'https://...'))
try:
    ...  # 使用 account.cookies + proxy.proxy_url 发起请求
    cookie_pool.release_account(account.account_id, success=True)
except Exception:
    cookie_pool.trigger_cooling(account.account_id, duration_hours=2)
    cookie_pool.release_account(account.account_id, success=False)

# 4. 暴露监控指标（后台线程每 15s 刷新池状态）
exporter = PoolMetricsExporter(cookie_pool=cookie_pool, ip_pool=ip_pool)
exporter.start_server(port=9090)   # http://localhost:9090/metrics
```

### 4. 运行测试

```bash
# 池系统测试（项目根目录）
python -m pytest tests/ -k "pool or scheduler or fingerprint or health or bootstrapper" -v

# 单文件示例
python -m pytest test_bootstrapper.py -v
python -m pytest tests/test_monitoring.py -v

# 监控栈（Prometheus + Grafana）
docker compose up -d prometheus grafana
```

---

## 目录结构

```
TKCrawler/
├── src/pools/                     # 账号池系统
│   ├── __init__.py                # 包导出（含 PoolMetricsExporter）
│   ├── models.py                  # AccountSession / ProxyNode / BrowserFingerprint / Task
│   ├── exceptions.py              # PoolException 异常族
│   ├── cookie_pool.py             # CookiePoolManager
│   ├── ip_pool.py                 # IPPoolManager
│   ├── fingerprint_pool.py        # FingerprintPoolManager
│   ├── scheduler.py               # PoolScheduler + Task
│   ├── health_checker.py          # HealthChecker
│   ├── bootstrapper.py            # AutoLoginBootstrapper
│   └── metrics.py                 # PoolMetricsExporter（Prometheus）
├── tests/                         # 单元/集成测试
├── docs/
│   ├── POOL_ARCHITECTURE.md       # 架构设计
│   ├── MONITORING_SETUP.md        # 监控部署指南
│   └── monitoring/                # prometheus.yml / alerting_rules.yml / alerts.yml / Grafana JSON
├── docker-compose.yml             # redis / postgres / prometheus / grafana
├── test_bootstrapper.py           # 登录引导验证脚本
└── sample_health_report.json      # 健康报告样例
```

---

## 性能目标

| 指标 | 目标 | 依据 |
|------|------|------|
| 10 账号并发采集 200 商品 | < 30 分钟 | AGENT_EXECUTION_CHECKLIST.md 成功标准 |
| 30 账号并发采集 2000 商品 | < 2 小时 | 同上 |
| 账号故障自动切换 | < 10 秒 | 调度器冷却重分配 + weighted 策略换号 |
| 10 账号并发健康检查 | < 10 秒 | HealthChecker 并发上限 10 |
| 单账号配额 | 200 商品/天（初始保守值） | `add_account(daily_quota=200)`，可调 |
| 单账号 QPS | 0.5 req/s | 配额 200/天 ≈ 均匀分摊 |
| 10 账号 QPS | 5 req/s（线性扩展） | 池化并发 |

### 扩容路径

- **加账号**：`AutoLoginBootstrapper.batch_login()` 批量登录 → `import_to_pool()` 入池
- **加代理**：`IPPoolManager.add_proxy()`，注意 `max_accounts=3` 限制
- **观察瓶颈**：Grafana `pool_account_quota_usage`（配额用尽）与 `pool_accounts_active < 3` 告警（账号不足）
