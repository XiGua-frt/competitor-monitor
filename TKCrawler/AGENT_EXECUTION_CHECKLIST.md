# 🚀 Multi-Agent执行清单

> 最后更新: 2026-10-08  
> 状态: 准备就绪，可立即执行

---

## ✅ 执行前检查

### 环境依赖
```bash
# 1. Redis
redis-cli ping  # 应返回 PONG

# 2. Python依赖
pip list | grep -E "curl-cffi|playwright|redis|prometheus"

# 3. 现有模块
python -c "from src.pools import CookiePoolManager, IPPoolManager; print('✅ 模块正常')"
```

---

## 📋 5个Agent任务（复制到独立Chat执行）

### 🔹 Agent 1: 指纹池管理器 (45分钟)
```
请按照 docs/AGENT_EXECUTION_PLAN.md 中 Task 1 的要求，实现 src/pools/fingerprint_pool.py 模块。

参考代码骨架: NEXT_STEPS_EXECUTION_GUIDE.md 第145-330行

核心功能:
1. 5种设备模板（MacBook M1/M2, Windows RTX3080/GTX1660, iPad Pro）
2. Canvas指纹生成（基于account_id固定种子，确保唯一性）
3. 应用到Playwright浏览器上下文

验证要求:
- 创建10个指纹并验证canvas hash 100%唯一
- 编写单元测试 tests/test_fingerprint_pool.py
- 生成验证报告（打印所有canvas hash）

交付物:
- src/pools/fingerprint_pool.py (280行)
- tests/test_fingerprint_pool.py (120行)
- 验证报告
```

**执行状态**: [ ] 未开始 / [ ] 进行中 / [ ] 已完成

---

### 🔹 Agent 2: 池调度器 (40分钟)
```
请按照 docs/AGENT_EXECUTION_PLAN.md 中 Task 2 的要求，实现 src/pools/scheduler.py 模块。

参考代码骨架: NEXT_STEPS_EXECUTION_GUIDE.md 第345-505行

核心功能:
1. 4种调度策略:
   - round_robin: 轮询分配
   - least_loaded: 选择负载最低的账号
   - weighted: health_score * (1 - load/quota)
   - sticky: 优先复用上次账号（降低关联风险）

2. 任务分配与释放
3. 冷却账号的任务重分配

验证要求:
- 测试100任务在4种策略下的分布
- 验证weighted策略偏好高健康度账号
- 验证sticky策略复用逻辑

交付物:
- src/pools/scheduler.py (250行)
- tests/test_scheduler.py (150行)
- 性能对比报告
```

**执行状态**: [ ] 未开始 / [ ] 进行中 / [ ] 已完成

---

### 🔹 Agent 3: 健康检查器 (50分钟)
```
请按照 docs/AGENT_EXECUTION_PLAN.md 中 Task 3 的要求，实现 src/pools/health_checker.py 模块。

参考代码骨架: NEXT_STEPS_EXECUTION_GUIDE.md 第520-675行

核心功能:
1. 账号健康检查（使用curl_cffi访问douyin.com，chrome131指纹）
2. 代理连通性检查（延迟<3s合格）
3. 自动恢复机制（3次失败→suspended，健康账号<5→告警）
4. 并发检查所有账号和代理

验证要求:
- 测试5个健康账号 + 2个失效账号的检测准确率
- 验证3次失败后自动暂停逻辑
- 验证并发检查10账号<10秒完成

交付物:
- src/pools/health_checker.py (280行)
- tests/test_health_checker.py (140行)
- 健康检查报告样例（JSON格式）
```

**执行状态**: [ ] 未开始 / [ ] 进行中 / [ ] 已完成

---

### 🔹 Agent 4: 自动登录器 (35分钟)
```
请按照 docs/AGENT_EXECUTION_PLAN.md 中 Task 4 的要求，实现 src/pools/bootstrapper.py 模块。

参考代码骨架: NEXT_STEPS_EXECUTION_GUIDE.md 第690-800行

核心功能:
1. 批量登录流程:
   - 使用Playwright + Camoufox浏览器
   - 打开douyin.com → 点击登录 → 手机号登录
   - 输入手机号/密码 → 等待人工滑块验证（30s超时）
   - 提取27个Cookies（sessionid, ttwid, __ac_nonce等）
   - 导入Cookie池并关联代理
   - 延迟30-60s

2. 单个账号登录
3. Cookie提取与验证
4. 导入到池并自动关联代理

验证要求:
- 测试3个账号的批量登录流程
- 验证提取的cookies包含必需字段
- 确认导入后账号可被调度

交付物:
- src/pools/bootstrapper.py (220行)
- docs/AUTO_LOGIN_GUIDE.md（使用说明）
- 测试日志：3账号登录成功的cookies样例
```

**执行状态**: [ ] 未开始 / [ ] 进行中 / [ ] 已完成

---

### 🔹 Agent 5: 监控指标导出器 (30分钟)
```
请按照 docs/AGENT_EXECUTION_PLAN.md 中 Task 5 的要求，实现 src/pools/metrics.py 和监控配置。

参考代码骨架: NEXT_STEPS_EXECUTION_GUIDE.md 第815-900行

使用开源方案: prometheus-client（直接pip install）

核心功能:
1. Prometheus指标定义:
   - Gauge: pool_accounts_total, pool_accounts_active, pool_accounts_cooling
   - Counter: scraping_requests_total{status="success|failed"}
   - Histogram: request_duration_seconds

2. 指标更新方法（每15s）
3. HTTP服务暴露 /metrics 端点（端口9090）
4. Grafana Dashboard配置（3个面板：账号池状态、请求成功率、代理健康度）
5. Prometheus告警规则（失败率>20%触发）

验证要求:
- 启动metrics server，访问 http://localhost:9090/metrics
- 验证指标格式符合Prometheus标准
- 导入dashboard.json到Grafana

交付物:
- src/pools/metrics.py (180行)
- docs/monitoring/pool_dashboard.json（Grafana配置）
- docs/monitoring/alerts.yml（告警规则）
- docs/MONITORING_SETUP.md（部署文档）
```

**执行状态**: [ ] 未开始 / [ ] 进行中 / [ ] 已完成

---

## 📊 执行进度追踪

### 第一批（无依赖，可立即并行）
- [x] Agent 1: 指纹池 (45分钟) — fingerprint_pool.py 439行，18测试通过
- [x] Agent 2: 调度器 (40分钟) — scheduler.py 480行，17测试通过
- [x] Agent 3: 健康检查器 (50分钟) — health_checker.py 577行，14测试通过

**预计完成时间**: 50分钟（并行）

### 第二批（可与第一批并行）
- [x] Agent 4: 自动登录器 (35分钟) — bootstrapper.py 448行，21测试通过（2026-10-08修复挂死+None落库bug）
- [x] Agent 5: 监控指标 (30分钟) — metrics.py 551行，30测试通过，/metrics端点HTTP 200验证OK

**预计完成时间**: 35分钟（并行）

### 总计
- **并行总耗时**: ~50分钟
- **串行总耗时**: ~200分钟
- **效率提升**: 75%

---

## ✅ 完成后验证

### 1. 单元测试
```bash
pytest tests/test_fingerprint_pool.py -v
pytest tests/test_scheduler.py -v
pytest tests/test_health_checker.py -v
```

### 2. E2E测试
```bash
pytest tests/test_e2e_concurrent.py -v
```

### 3. 功能验证
```python
from src.pools import *

# 初始化所有组件
cookie_pool = CookiePoolManager()
ip_pool = IPPoolManager()
fp_pool = FingerprintPoolManager()
scheduler = PoolScheduler(cookie_pool, ip_pool, fp_pool)
health_checker = HealthChecker(cookie_pool, ip_pool)

print("✅ 所有模块加载成功")
```

### 4. 性能验证
- [ ] 单账号QPS: 0.5 req/s
- [ ] 10账号QPS: 5 req/s
- [ ] 健康检查延迟: <5s

---

## 📁 最终交付物

### 代码（5个模块）
- [ ] `src/pools/fingerprint_pool.py` (280行)
- [ ] `src/pools/scheduler.py` (250行)
- [ ] `src/pools/health_checker.py` (280行)
- [ ] `src/pools/bootstrapper.py` (220行)
- [ ] `src/pools/metrics.py` (180行)

### 测试（4个套件）
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

## 🎯 成功标准

### 功能
- [x] 核心基础模块完成（Cookie池/IP池）
- [ ] P0模块完成（指纹池/调度器/健康检查器）
- [ ] P1模块完成（自动登录/监控）
- [ ] 所有单元测试通过
- [ ] E2E测试成功率>90%

### 性能
- [ ] 10账号并发采集200商品 < 30分钟
- [ ] 30账号并发采集2000商品 < 2小时
- [ ] 账号故障自动切换 < 10秒

### 监控
- [ ] Grafana面板正常显示
- [ ] 告警规则触发正常
- [ ] 所有指标可视化清晰

---

## 📞 参考文档

| 文档 | 位置 |
|------|------|
| **本执行清单** | `AGENT_EXECUTION_CHECKLIST.md` |
| 快速启动指令 | `docs/QUICK_START_AGENTS.md` |
| 详细执行计划 | `docs/AGENT_EXECUTION_PLAN.md` |
| 项目总结 | `docs/POOL_PROJECT_SUMMARY.md` |
| 代码骨架 | `NEXT_STEPS_EXECUTION_GUIDE.md` |

---

## 🚀 立即开始

1. ✅ 环境检查完成
2. 📋 打开5个Codex Chat窗口
3. 📝 复制上述5个Agent命令
4. ▶️ 同时执行
5. ⏱️ 等待~50分钟
6. ✅ 运行验证测试

**准备好了吗？开始执行！** 🎉
