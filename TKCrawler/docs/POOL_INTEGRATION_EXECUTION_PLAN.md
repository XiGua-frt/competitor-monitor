# 池系统接入抓取流程：下一步执行计划

> 编制日期：2026-10-08  
> 状态：待执行  
> 计划依据：`PROJECT_STATUS.md`、`README_POOL_SYSTEM.md`、`POOL_IMPLEMENTATION_SUMMARY.md` 及 2026-10-08 项目现状报告  
> 目标：以可回滚、可观测、受控的方式，让现有采集任务使用账号池；先验证串行账号分配，再评估是否需要代理池及有限并发。  
> 估算：约 2–4 个工程日（不含真实账号/代理的外部验证）；按实际代码结构复核后调整。

---

## 1. 执行原则

1. **先验证，再接入**：先补池模块独立测试和生命周期契约，再改采集入口。
2. **渐进上线**：默认关闭池接入；先影子模式，再单账号串行灰度，最后评估并发。
3. **故障可回退**：Redis、池服务或分配流程异常时，必须有明确策略；不得静默进入无限重试。
4. **任务级固定身份**：一个采集任务在开始时取得账号租约，任务内保持一致；不在请求中途无记录地切换账号。
5. **遵守平台授权与速率限制**：仅在授权和平台规则允许的范围内采集。遇到 429、WAF 或认证异常时应停止/降速、记录并按冷却策略处理，不以持续轮换账号、代理或指纹来规避限制。
6. **控制变更面**：第一阶段只接 Cookie 池；代理池、指纹池和并发提升另设决策门槛，避免同时改变多个变量。

## 2. 当前基线与范围

### 已知基线

- 五类 scraper 与单实例 `AntiDetectionClient` 已用于实际抓取。
- Cookie、IP、指纹池及池调度器已有实现；当前尚未成为抓取流程的消费者。
- 周期任务调度器 `src/scheduler/` 与池调度器 `src/pools/scheduler.py` 是不同职责模块。
- 2026-10-08 报告记录的相关测试基线为 159 passed、1 skipped；其中 bootstrapper 曾有挂死测试问题，现已修复。
- 当前 Redis 可连通；抓取依赖 PostgreSQL；当前代理配置仍为单一 `config.PROXY_URL`。

### 本计划包含

- 复核池接口与采集客户端生命周期。
- 补 Cookie/IP 池单元测试。
- 设计并实现账号租约/释放/失败处理。
- 在 `main.py` 客户端创建与 scraper 生命周期中接入 Cookie 池。
- 增加功能开关、影子模式、日志/指标与回退路径。
- 使用 mock/本地依赖完成自动化验证；真实账号 E2E 留待用户提供授权测试环境后执行。

### 本计划不包含

- 重做已完成的指纹池、池调度器、健康检查器或 Prometheus 面板。
- 自动批量登录、人工滑块流程或真实账号导入。
- 默认开启 10 账号并发。
- 通过身份、代理或指纹轮换绕过平台访问控制、WAF 或速率限制。

## 3. 分阶段执行计划

### Phase 0：核对现状并冻结接口（约 2–3 小时）

**任务**

- [ ] 检查 `main.py::_make_client()`、`BaseScraper` 和五类 scraper 的客户端创建、请求、结束/异常路径。
- [ ] 检查 `src/pools/cookie_pool.py`、`src/pools/ip_pool.py`、`src/pools/scheduler.py` 的实际公开接口及 Redis 数据模型。
- [ ] 标明每个 scraper 是否持有长生命周期 session，确认客户端是否可安全绑定一个账号 Cookie。
- [ ] 写下接口契约：分配、租约过期、释放、暂停/冷却、池不可用时的行为。
- [ ] 确认现有配置和 CLI 参数的命名，避免另造一套相似配置。

**交付物**

- 本计划中的接口决策记录（更新“决策记录”一节）。
- 一张简短调用链：CLI/调度任务 → 客户端工厂 → scraper → 请求 → 释放。

**验收条件**

- 关键生命周期和错误处理路径已定位；没有依赖猜测来定义接入接口。
- 明确池失败时采用 `fail_closed` 还是显式单会话 fallback。生产默认不得未经配置自动回退到共享身份。

### Phase 1：补足池模块单元测试（约 0.5–1 天）

**任务**

- [ ] 新增 `tests/test_cookie_pool.py`：创建/查询/更新/删除、权重选择、冷却/恢复/暂停、空池、Redis 异常及序列化边界。
- [ ] 新增 `tests/test_ip_pool.py`：代理注册/删除、健康状态、账号绑定、不可用代理过滤、轮换和外部健康检查失败。
- [ ] 使用 fake/mock Redis 和 HTTP 响应；单元测试不访问 TikTok、不启动浏览器、不依赖真实代理。
- [ ] 固化 `AccountSession.to_dict()/from_dict()` 对 `None`、空字符串和已有记录格式的兼容测试。
- [ ] 验证并发获取租约时不会超出账号配置的并发配额（若当前实现尚无租约语义，则记录为 Phase 2 前置设计项）。

**验收条件**

- 新测试重复运行稳定，不依赖执行顺序或外部网络。
- 全量相关测试保持绿色；报告测试数与失败/跳过情况。
- 状态机测试明确覆盖正常转换和非法转换。

**建议验证命令**

```bash
pytest -q tests/test_cookie_pool.py tests/test_ip_pool.py
pytest -q tests/test_anti_detection.py tests/test_fingerprint_pool.py tests/test_health_checker.py tests/test_scheduler.py tests/test_metrics.py tests/test_bootstrapper.py
pytest -q
```

> 测试目录/文件名可能与实际仓库略有不同；Phase 0 复核后按现状调整命令，不为匹配本计划而重复造测试文件。

### Phase 2：定义并实现账号租约生命周期（约 0.5 天）

**任务**

- [ ] 增加一个任务级分配结果对象（例如 `AccountLease`），包含 account/session 信息、lease/task ID、分配时间和释放状态；敏感 Cookie 不得进入日志。
- [ ] 实现 `acquire` 与 `release` 的对称生命周期，释放必须幂等。
- [ ] 为成功、普通异常、取消、中断、超时、认证失效及限流响应定义释放语义。
- [ ] 明确冷却由谁触发、持续时间由何处配置、冷却失败时如何记录；不把所有 HTTP 错误都等同于账号失效。
- [ ] 设置任务/租约超时回收机制，避免进程异常退出后账号永久处于占用状态。
- [ ] 统一结构化日志字段：task_id、scraper_type、account_id（脱敏/内部 ID）、proxy_id（如适用）、结果类别、耗时；不记录 Cookie、完整代理凭据或其他秘密。

**验收条件**

- 所有退出路径最终释放或由过期机制回收租约。
- 重复释放不会造成负载计数错误。
- 账号选择失败时返回可识别错误，调用层可区分空池、Redis 故障与账号不可用。

### Phase 3：Cookie 池接入采集链路（约 0.5–1 天）

**任务**

- [ ] 在 `main.py::_make_client()` 或其上层任务边界注入池分配器；不要让 scraper 自行从全局池隐式取号。
- [ ] 让一个任务取得一个账号 lease，并将该账号 Cookie/session 显式注入 `AntiDetectionClient`。
- [ ] 在统一任务边界的 `try/finally` 中释放 lease，覆盖成功、异常与取消。
- [ ] 由统一响应处理层分类认证失败、429/限流、WAF/挑战和普通网络错误；只对策略允许的情形进行有界重试。
- [ ] 初始并发限制保持为 1；不要因为池中有多个账号就自动并行。
- [ ] 维持现有单会话模式，作为显式配置的回退路径；明确记录启用池时 Redis 不可用的行为。

**验收条件**

- 五种采集类型均能在 mock 池下完成“分配 → 请求 → 释放”测试。
- 任一 scraper 抛异常时 lease 仍被释放。
- 池关闭时现有采集行为不变；池开启但空池/Redis 故障时符合明确配置的失败策略。
- 账号切换不发生在同一个任务的请求序列中。
- 没有无限重试、隐式身份轮换或敏感凭据日志。

### Phase 4：影子模式、灰度和可观测性（约 0.5 天）

**任务**

- [ ] 增加配置开关：池总开关、Cookie 池开关、影子模式、明确的 fallback 策略及初始并发上限。
- [ ] 影子模式只执行候选账号选择并记录结果，不改变请求实际使用的 session。
- [ ] 在测试环境用 mock 流量验证分配分布、释放计数和异常告警。
- [ ] 核对现有 Prometheus 指标名称与 Grafana dashboard；只补缺失的接入指标，不重复建设已有池监控。
- [ ] 形成单任务/小批次灰度步骤和一键关闭方式。

**重点观察指标**

- 采集成功率、任务耗时与失败类别。
- 账号池可用数、租约数、租约超时数及冷却数。
- 认证失败、429/限流响应、挑战/WAF 类响应的计数趋势。
- Redis 错误数、fallback 次数、重试次数和重复数据情况。

**验收条件**

- 指标中不含 Cookie、完整代理 URL、账号密码或其他可识别秘密。
- 可通过配置在不改代码的情况下关闭池接入并恢复原路径。
- 影子模式不改变实际请求身份和采集结果。

### Phase 5：小流量实机验证与扩大范围决策（时长取决于外部条件）

**前置条件**

- 有明确授权的测试账号与代理环境；确认平台条款、数据使用权限和允许的请求速率。
- PostgreSQL、Redis、监控和日志均可用；回滚开关已验证。
- 测试期间设定低请求速率、单账号串行和明确停止条件。

**步骤**

- [ ] 先运行不发起目标请求的配置/连接检查。
- [ ] 在授权范围内以一个测试任务验证 Cookie 注入、数据落库和 lease 释放。
- [ ] 小批次观察成功率、认证/限流响应、数据完整性及重复率。
- [ ] 若出现持续 429、挑战、授权异常或账号状态异常，立即停止测试并调查；不通过换账号/代理继续请求。
- [ ] 完成实机记录后，由项目负责人决定是否接入代理池；并发提升另开变更评审。

**验收条件**

- 小批次任务能够完成且状态可审计；无租约泄漏、凭据泄露或非预期重试。
- 结果、失败和停止原因均有记录。
- 通过评审后才进入代理池接入或并发评估。

## 4. 里程碑与决策门

| 里程碑 | 完成标志 | 决策 |
|---|---|---|
| M0：接口冻结 | 生命周期、错误策略、fallback 语义明确 | 批准开始改代码 |
| M1：池测试可信 | Cookie/IP 池与回归测试通过 | 批准实现任务级 lease |
| M2：串行接入 | mock 下五类 scraper 全链路通过 | 批准影子模式/灰度 |
| M3：灰度可回滚 | 指标、停止条件、开关和回退验证通过 | 在授权环境小批次验证 |
| M4：实机验证完成 | 低速小批次数据质量与稳定性有记录 | 单独评估代理池/并发，不自动扩容 |

任何里程碑失败时，先修复或回滚当前阶段，不跨阶段叠加新变量。

## 5. 风险与应对

| 风险 | 影响 | 应对 |
|---|---|---|
| scraper/客户端生命周期不一致 | session 未正确注入或资源泄漏 | Phase 0 先绘制调用链；在统一任务边界管理 lease |
| Redis 短暂故障 | 任务失败或行为不可预期 | 明确 fail-closed/fallback；记录并告警；不做隐式无限重试 |
| 请求中途换账号 | 数据与状态难以归因 | 一个任务固定一个 lease；身份切换仅允许发生在新任务边界 |
| 429/WAF 被当成换号信号 | 可能继续触发平台限制 | 以降速/停止/冷却为默认策略；设置硬停止条件 |
| 多变量同时上线 | 故障根因难定位 | Cookie、代理、指纹、并发分阶段评审和灰度 |
| 日志泄露凭据 | 账号/代理被滥用 | 日志字段白名单；Cookie、密码、完整代理凭据强制脱敏 |
| 旧执行文档过时 | 执行者按已完成事项重复开发 | 将本文件作为当前执行入口；后续同步标注旧文档状态 |

## 6. 完成定义（Definition of Done）

- [ ] Cookie/IP 池关键状态机和失败路径有稳定单元测试。
- [ ] 账号任务级 lease 生命周期覆盖成功、异常和取消。
- [ ] 五类 scraper 的池接入均有 mock 集成测试。
- [ ] 默认配置不改变现有采集行为；启用/关闭池有清楚说明。
- [ ] Redis 故障、空池、限流、认证失败都有有界且可观测的处理。
- [ ] 全量测试通过，测试结果记录在交付说明。
- [ ] 敏感信息不进入日志、测试输出或样例文件。
- [ ] 如进行实机验证，有授权、速率约束、停止记录及数据质量结果。
- [ ] 是否推进 IP 池和并发扩容由独立评审决定，不以“达到 10 账号并发”为默认完成标准。

## 7. 执行记录

| 日期 | 阶段 | 结果/证据 | 后续动作 |
|---|---|---|---|
| 2026-10-08 | 计划创建 | 基于项目现状报告整理 | 待执行 |
| 2026-10-08 | Phase 0 | 接口契约核实完毕（见决策记录）；调用链：CLI run_*_scrape → pool_scoped_client → _make_client/inject_cookie → scraper → lease_ctx.success → release | 已完成 |
| 2026-10-08 | Phase 1 | 新增 tests/test_cookie_pool.py (36 测试) + tests/test_ip_pool.py (43 测试，含 2 xfail 记录)，fakeredis 无网络 | 已完成 |
| 2026-10-08 | Phase 2 | 新增 src/pools/lease.py + tests/test_lease.py (12 测试)：AccountLease/LeaseManager，acquire/release 幂等，TTL 兜底回收，日志白名单无凭据 | 已完成 |
| 2026-10-08 | 顺带修复 | Wave 1 三个 Agent 上报 6 个产品 bug，修复 5 个：①ProxyNode.to_dict None→DataError ②AccountSession/ProxyNode from_dict ''→None 往返 ③ip_pool.py:103 日志泄露代理凭据（脱敏）④健康检查非 200 分支漏 update_success_rate ⑤reset_daily_counts 遗漏 suspended 集合；未修 2 个低优（remove_account 依赖内存缓存、account_id 8 位 hex 碰撞风险）已记录 | 已完成 |
| 2026-10-08 | Phase 3+4 | 新增 src/pools/integration.py（pool_scoped_client + inject_cookie_to_client，零侵入注入）；main.py 五个 run_*_scrape 接入 try/finally lease；config.py 新增 5 个开关（默认全关）；tests/test_pool_integration.py 21 测试。全量回归 713 passed, 4 skipped, 2 xpassed（test_monitor_session.py 因沙箱 tmpdir 权限需 --basetemp，15 passed 单独验证） | 已完成 |
| 2026-10-08 | Phase 5 | 未执行：需授权测试账号 + 代理环境 | 待用户提供实机条件 |

## 8. 决策记录

| 日期 | 决策 | 原因/影响 |
|---|---|---|
| 2026-10-08 | 先接 Cookie 池并保持串行，不默认开启多账号并发 | 降低变更面，先验证生命周期与数据质量 |
| 2026-10-08 | 429/WAF/挑战默认触发停止或降速，不以换身份继续请求 | 尊重平台访问限制，避免无界重试与规避行为 |
| 2026-10-08 | 旧的 `NEXT_STEPS_EXECUTION_GUIDE.md` 不作为本计划的执行清单 | 其内容描述的部分池模块/测试已完成，任务状态与当前报告不一致；后续应加过时提示或归档 |
| 2026-10-08 | Phase 0 接口契约：任务级固定身份经 LeaseManager.acquire（strategy 可配，默认 weighted），释放幂等且在 finally 中执行；lease TTL 600s 兜底进程异常退出；错误三分类 NoAvailableAccount（空池）/LeaseExhausted（全占用）/Redis 异常（原样传播）；POOL_FAIL_POLICY=fail_closed 默认（池开启但不可用→退出码 2），fallback_single 为显式配置的回退 | 满足验收条件"没有依赖猜测定义接口"；生产默认不静默回退共享身份 |
| 2026-10-08 | Phase 0 调用链：main.py run_*_scrape → pool_scoped_client(scraper_type, task_id) → acquire → _make_client() + inject_cookie_to_client（零侵入 client.session.cookies.update）→ scraper 串行采集 → lease_ctx.success → finally release。scraper 与 anti_detection 零改动 | 控制变更面；client 生命周期与任务边界一致 |
| 2026-10-08 | Cookie 注入采用零侵入方式（集成层 update session.cookies），不改 AntiDetectionClient 构造签名 | 避免 anti_detection 返工；session 定位兼容 client.session 与 client._curl.session 两种形态 |
| 2026-10-08 | IP 池/指纹池/并发提升不做自动接入，Phase 5 实机验证后由独立评审决定 | 与里程碑 M4 决策一致 |
