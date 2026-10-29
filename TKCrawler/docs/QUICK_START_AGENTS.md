# 🚀 Multi-Agent 快速启动指令

> 复制以下命令到独立的 Codex Chat 中执行

---

## 📋 5个并行任务

### Agent 1: 指纹池管理器 ⚡ 无依赖，立即开始

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

---

### Agent 2: 池调度器 ⚡ 依赖Cookie池/IP池（已完成）

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

---

### Agent 3: 健康检查器 ⚡ 依赖Cookie池/IP池（已完成）

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

---

### Agent 4: 自动登录器 ⚡ 可并行开发

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

---

### Agent 5: 监控指标导出器 ⚡ 依赖所有池模块

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

---

## 📊 执行时间线

```
第一批（无依赖，立即开始）:
├─ Agent 1: 指纹池 (45分钟)
├─ Agent 2: 调度器 (40分钟)
└─ Agent 3: 健康检查器 (50分钟)

第二批（可并行）:
├─ Agent 4: 自动登录器 (35分钟)
└─ Agent 5: 监控指标 (30分钟)

总耗时: ~2.5小时（串行需6+小时）
```

---

## ✅ 验收标准

### 功能验收
- [ ] 所有5个模块通过单元测试
- [ ] E2E测试: 10账号并发采集200商品，成功率>90%
- [ ] 压力测试: 30账号采集2000商品，2小时内完成

### 性能验收
- [ ] 单账号QPS: 0.5 req/s
- [ ] 10账号QPS: 5 req/s（线性扩展）
- [ ] 健康检查延迟: <5s

### 监控验收
- [ ] Grafana面板正常显示所有指标
- [ ] WAF拦截告警规则触发正常
- [ ] 账号池状态可视化清晰

---

## 📁 最终交付物清单

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

## ⚠️ 注意事项

1. **Redis依赖**: 确保Redis运行在 localhost:6379
2. **代理服务**: IP池需要真实代理节点（测试可用mock）
3. **Cookie有效期**: 账号cookies需定期刷新（每7天）
4. **反检测**: 务必使用curl_cffi的chrome131指纹
5. **并发限制**: 单账号并发数建议≤3

---

## 📞 参考文档

- **本快速启动**: `docs/QUICK_START_AGENTS.md`
- **详细执行计划**: `docs/AGENT_EXECUTION_PLAN.md`
- **代码骨架**: `NEXT_STEPS_EXECUTION_GUIDE.md`
- **架构设计**: `POOL_ARCHITECTURE.md`
- **实现总结**: `POOL_IMPLEMENTATION_SUMMARY.md`
