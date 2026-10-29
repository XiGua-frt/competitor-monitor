# TKCrawler 项目架构文档

> 生成日期：2026-10-09 ｜ 基于 2026-10-08 前后的代码状态（PROJECT_STATUS 进度约 98%，469 passed / 4 skipped）

## 1. 项目定位

TKCrawler 是一个面向 **TikTok 跨境电商（美区 TikTok Shop）的选品数据采集与分析系统**，目标是将"什么好卖、谁在推、推得怎么样"变成可量化、可追踪、可复盘的结构化数据，替代人工刷广场选品。

业务逻辑是一条选品决策漏斗：

```
发现(什么在涨) → 初筛(能不能做) → 深筛(做有没有得赚) → 测试(内容行不行) → 放大(要不要压货)
```

**核心设计思想 —— 快照差分**：单次抓取只能看到存量，真正值钱的是增量。对同一实体周期快照（主键含 snapshot_date），差分算出 `gmv_growth_7d/30d`、`views_growth_7d` 等增长字段，在爆款生命周期（2-8 周）前半段发现飙升品。

**技术栈**：Python 3.9+ / PostgreSQL 13（按日分区快照表）/ Redis（队列 + 池状态）/ curl_cffi（chrome131 TLS 指纹主通道）+ Camoufox（反检测浏览器登录态）/ APScheduler / Prometheus + Grafana / Docker Compose。

---

## 2. 整体分层架构

```
┌─────────────────────────────────────────────────────────────┐
│  入口/调度层   main.py (CLI)  +  src/scheduler/              │
│               APScheduler 周期触发 → Redis 优先级队列 → 重试  │
├─────────────────────────────────────────────────────────────┤
│  资源池层      src/pools/                                    │
│               Cookie池 + IP池 + 指纹池 + lease 租约 + 接入层  │
├─────────────────────────────────────────────────────────────┤
│  抓取/反检测层  src/anti_detection/  +  src/scrapers/         │
│               TLS指纹/限速/登录态/WAF判定 ←→ 5类爬虫+解析器   │
├─────────────────────────────────────────────────────────────┤
│  存储层        src/database/                                 │
│               PostgreSQL 4张按日分区快照表 + 差分引擎 + 迁移   │
├─────────────────────────────────────────────────────────────┤
│  计算/观测/富化  src/metrics/ + src/monitoring/ + src/external_data/ │
│               4个选品派生指标 ｜ Prometheus ｜ 1688/Amazon/Trends │
└─────────────────────────────────────────────────────────────┘
        底座：src/common/ (config / logger / exceptions)
```

---

## 3. 入口与启动流程（main.py）

CLI 子命令（argparse）：

| 命令 | 说明 |
|---|---|
| `scrape {shop\|video\|creator\|hashtag\|itemlist}` | 手动触发单次采集 |
| `scheduler --start\|--status\|--pause` | 调度主服务（Docker 中默认运行） |
| `init-db` | 执行 Alembic 迁移建表 + 校验 |
| `report` | 输出某商品 4 个派生指标 JSON |
| 全局 `--dry-run` | 只打印不请求不写库 |

scrape 类流程（以 shop 为例）：

1. `check_db()` — SQLAlchemy `SELECT 1` 探测 PostgreSQL，失败退出码 2
2. `SnapshotDiffEngine()` 建快照引擎
3. `pool_scoped_client(task_type)` 上下文 — 从 Cookie 池租约账号，注入 cookie
4. 构造对应 Scraper → 抓取/解析 → `save_snapshot()` 逐条 upsert 入库

**退出码约定**：0 成功 / 1 采集失败（WAF/限流/会话过期/爬虫异常）/ 2 配置或依赖错误（池不可用、配置错误）。

---

## 4. 模块清单（src/）

### 4.1 common/ — 基础设施（被所有模块依赖，最底层）

| 模块 | 职责 |
|---|---|
| `config.py` | 集中配置（.env：TikTok 账号、POSTGRES_*、Redis、限流/配额） |
| `logger.py` | loguru 日志（控制台 + logs/crawler.log） |
| `exceptions.py` | 异常体系：`CrawlerException` 基类，`WAFBlocked / RateLimit / SessionExpired / QuotaExceeded` |

### 4.2 anti_detection/ — 反检测/请求层

| 模块 | 职责 |
|---|---|
| `curl_client.py` | `CurlCffiClient`：TLS/JA3 指纹请求，WAF 挑战页启发式判定 |
| `browser_client.py` | `CamoufoxClient`：Playwright + Camoufox 浏览器，登录状态机（自动/半自动、验证码、人工等待） |
| `session_manager.py` | TikTok 登录态 cookie 存取（data/session.json） |
| `rate_limiter.py` | 随机间隔限速 + Redis 日配额 |
| `__init__.py` | `AntiDetectionClient` 编排：curl 失败自动降级 browser |

### 4.3 scrapers/ — 抓取层（5 条通道）

| 模块 | 职责 |
|---|---|
| `base_scraper.py` | `BaseScraper`：客户端持有、限速、日配额、429/403 软冷却 1 小时 |
| `shop_scraper.py` | shop.tiktok.com 商品详情/类目 |
| `video_scraper.py` | 商品关联视频流 |
| `creator_scraper.py` | 达人主页 + 橱窗（含 nano/micro/mid/macro 分层） |
| `hashtag_scraper.py` | 话题页 views 快照 |
| `item_list_scraper.py` | 签名 API `/api/post/item_list/` 达人视频列表（含分页） |
| `signer.py` | `TikTokSigner`：X-Bogus 签名（纯 stdlib，可注入替换） |
| `parsers/` | 5 个解析器：从嵌入 JSON（`__UNIVERSAL_DATA_FOR_REHYDRATION__` / `SIGI_STATE` / `__MODERN_ROUTER_DATA__`）抽取字段 |

选择器全部外置 JSON 配置：`selectors.json`、`creator_selectors.json`、`hashtag_selectors.json`、`video_selectors.json`（结构变更只改配置不改代码）。

### 4.4 database/ — 存储层

| 模块 | 职责 |
|---|---|
| `connection.py` | SQLAlchemy 2.0 engine/sessionmaker 连接池 |
| `models.py` | 4 张快照表 ORM：product / video / creator / hashtag_snapshots，主键 `(实体ID, snapshot_date)`，product 表按月 RANGE 分区 |
| `snapshot_engine.py` | `SnapshotDiffEngine`：`save_snapshot`（幂等 upsert）、`calculate_growth` 差分、`get_latest_snapshot` |
| `migrations/` | Alembic 迁移（001 初始分区 schema） |

### 4.5 pools/ — 账号/IP 资源池（7 模块）

**目的**：从单账号日配额 200 商品扩展到 10 账号并发 2000 商品/天（<30 分钟），账号故障自动切换 <10 秒。

```
调用方(main.py/scrapers) → integration.pool_scoped_client（开关/影子模式/故障策略）
    → lease.py          任务级账号租约（Redis SETEX，一次 acquire、任务内固定身份、release 幂等）
    ├─ cookie_pool.py       账号池：健康分(成功+0.01/失败-0.05)、<0.3 自动冷却 2h、日配额 200
    ├─ ip_pool.py           代理池：账号-代理长期绑定防关联、单代理最多 3 账号、自动探活
    ├─ fingerprint_pool.py  指纹池：5 类设备模板 + Canvas 噪声 + 时区/locale，每账号唯一
    ├─ scheduler.py         PoolScheduler：4 种调度策略（round_robin / least_loaded / weighted / sticky）
    ├─ health_checker.py    curl_cffi 异步探活、连续 3 次失败自动恢复、报告 7 天留存
    ├─ bootstrapper.py      Camoufox 批量登录 → 提取 27 个关键 Cookie → 导入池
    ├─ metrics.py           PoolMetricsExporter：Prometheus /metrics + Grafana 池面板
    └─ models.py / exceptions.py
```

故障策略：`POOL_FAIL_POLICY = fail_closed`（池不可用即停）或 `fallback_single`（回退单账号）；`POOL_ENABLED` / 影子模式开关控制接入。

### 4.6 scheduler/ — 调度层

| 模块 | 职责 |
|---|---|
| `task_scheduler.py` | `TaskScheduler`：APScheduler 周期触发 + 队列编排 + data/scheduler_state.json 状态持久化 |
| `task_queue.py` | Redis sorted set 优先级队列（score = priority + 时间），Redis 不可用降级内存 heapq |
| `retry_policy.py` | 指数退避 1m → 2m → 4m，max_attempts=3 |

任务定义在 `src/scheduler/config/tasks.yaml`：shop 每 2 天 / video 每日 / hashtag 每日 / itemlist 每日。

### 4.7 metrics / monitoring / external_data

| 模块 | 职责 |
|---|---|
| `metrics/calculator.py` | `MetricsCalculator`（只读）：内容效率（GMV÷视频数）、达人渗透率、饱和度指数、飙升系数 4 个派生指标，除零返回 None |
| `monitoring/metrics.py` | Prometheus 指标：scraper_requests_total、WAF blocks、数据质量空字段率 |
| `external_data/` | `alibaba/amazon/trends_client.py` + `enricher.py`：1688 供应商 / Amazon BSR（SigV4）/ Google Trends 三源富化，均可选凭据、失败不阻塞主流程 |

---

## 5. 数据流向

```
tasks.yaml 定义周期任务
  → TaskScheduler 入队 Redis（优先级队列）
  → main.py handler 领取任务
  → pool_scoped_client 从 Cookie 池租约账号（绑定代理/指纹）
  → Scraper 经 AntiDetectionClient（限速/登录态/WAF 判定，curl→browser 降级）请求 TikTok
  → Parser 按 selectors*.json 从页面嵌入 JSON / API 响应抽取字段
  → SnapshotDiffEngine.save_snapshot 幂等 upsert 进 PostgreSQL 快照表
  → MetricsCalculator 只读快照差分计算选品指标；ExternalDataEnricher 旁路富化供应链/热度数据
  → Prometheus 全程记录请求计数 / WAF 拦截 / 数据质量
```

## 6. 数据 Schema 概要（tk-selection-data-schema.md）

| 维度 | 关键字段 | 要点 |
|---|---|---|
| **商品（P0 主表，15 字段）** | product_id、三级类目、price、sales_30d（区间取中位）、gmv_30d_est、**gmv_growth_7d/30d（快照差分）**、listed_date、shop_id、creator_count、video_count_7d、review、is_promotion | gmv_growth 是漏斗入口、全系统最值钱字段 |
| **视频（P0）** | video_id、product_id（购物车锚点）、author_id、互动数、duration、is_paid、hook_type | 素材模板库与内容效率评估 |
| **达人（P0 简版）** | creator_id、follower_count（腰部 1-10 万性价比最高） | 建联名单与分销策略 |
| **话题（P1）** | tag_name、views_total、views_growth_7d（差分） | 入场/离场时机 |
| **供应链（P1, 1688）** | 采购价、MOQ、交期、pod_supported | 毛利测算（POD 主线必采） |
| **外部验证（P1）** | trends_score_12mo、amazon_bsr/reviews | 区分真趋势 vs 内容幻觉 |
| **派生指标（入库计算，不采集）** | 内容效率、达人渗透率、饱和度指数、飙升系数 | Pool 榜单核心排序字段 |

**诚实性约束**：销量是区间值、GMV 是估算，只用于排序不做财务决策；风控优先于数据完整性（WAF 即停，宁缺不冒账号风险）。

---

## 7. 部署架构（Docker Compose，5 服务）

| 服务 | 镜像 | 说明 |
|---|---|---|
| postgres | postgres:13-alpine | 5432，健康检查 pg_isready，挂载 migrations 自动初始化 |
| redis | redis:6-alpine | 6379，AOF 持久化，可选密码 |
| crawler | 本地 Dockerfile 构建 | 依赖前两者健康后启动，命令 `python main.py scheduler --start`；代理指向 host.docker.internal:7892 |
| prometheus | prom/prometheus:latest | 9090，配置来自 docs/monitoring/ |
| grafana | grafana/grafana:latest | 3000，预置仪表盘自动 provisioning |

Dockerfile 要点：python:3.11-slim 基础镜像、无 ENTRYPOINT（避免与 compose command 冲突）、构建期过滤不可用的 amazon-paapi 包并补装 psycopg[binary]、先 COPY requirements 利用层缓存、非 root 用户运行。

**主要依赖**：curl-cffi、camoufox、playwright（反检测）；sqlalchemy 2.0 / alembic / psycopg2（存储）；apscheduler / redis（调度队列）；loguru / prometheus-client / sentry-sdk（观测）；pydantic / python-dotenv / pyyaml / tenacity（工具）。

---

## 8. scripts/ 运维与验证工具

分三类，所有真实请求类脚本均带严格请求预算与风控纪律：

| 类别 | 脚本 | 用途 |
|---|---|---|
| **运维工具** | `setup.sh` | 一键环境安装（Python 3.9+ / Docker 检查） |
| | `init_db.py` | Alembic 迁移建表（含分区）+ 校验 |
| | `login_tiktok.py` | 半自动登录（弹浏览器人工登录 → 存 session.json） |
| | `monitor_session.py` | 登录态健康监控，结果写 data/session_health.log |
| | `pool_add_account.py` | Cookie 池导入（登录导入或从 session 文件导入）+ 绑定代理 |
| **逆向探测/校准** | `probe_public_pages.py` | 公开页首次真实请求校准（证据存 docs/probe/） |
| | `probe_signed_api.py` | 实测 X-Bogus 签名算法的服务端接受度 |
| | `probe_itemlist_hook.py` | 被动捕获 tag 页 XHR，校准 item_list 签名参数 |
| **E2E/链路验证** | `e2e_shop_20261008.py` | shop 通道真实抓取 → 入库 → 核对（预算 ≤5 请求） |
| | `e2e_itemlist_20261008.py` | item_list 通道 E2E（预算 ≤8 请求，纯 curl_cffi） |
| | `scheduler_smoke.py` | 调度器全链路冒烟（无网络请求） |
| | `hashtag_snapshot_job.py` | hashtag 日常快照定时任务（标签间随机 8-12s） |

---

## 9. 测试组织（tests/，469 passed / 4 skipped）

扁平 pytest 布局，25 个 `test_*.py` + `fixtures/`（21 个页面/JSON 样本），按 6 组归纳：

1. **爬虫抓取器**（5）：对应 5 条抓取通道（test_shop / item_list / hashtag / video / creator_scraper）
2. **资源池**（6）：cookie_pool / ip_pool / fingerprint_pool / pool_integration / lease / bootstrapper
3. **基础设施**（6）：database / scheduler / cli×2 / health_checker / metrics
4. **反检测与管道**（4）：anti_detection / hashtag_pipeline / e2e_pipeline
5. **监控与运维**（4）：monitoring×2 / monitor_session / external_data
6. **部署**（1）：test_dockerfile.py（用测试校验 Dockerfile 构建约定）

---

## 10. 文档与工作流体系

| 位置 | 内容 |
|---|---|
| 根目录 | PROJECT_STATUS.md（进度与迭代史）、AGENT_TASK_CARDS.md（11 个 Agent A-K 任务卡）、AGENT_EXECUTION_CHECKLIST.md（池系统 5-Agent 清单，全部完成）、NEXT_STEPS_EXECUTION_GUIDE.md（池系统代码骨架级指南）、README_POOL_SYSTEM.md |
| docs/ 顶层 | POOL_ARCHITECTURE.md、DEPLOYMENT.md、FIELD_SCHEMA.md、AUTO_LOGIN_GUIDE.md、MONITORING_SETUP.md 等 9 个 md |
| docs/probe/ | **实机取证存档**（占 docs 绝大部分）：按时间戳命名的请求证据（*_body.html / *_meta.json）+ 结论文档，发现必须 Evidence → Finding → verified 标志，选择器翻转为 verified:true 必须附证据引用 |
| docs/reverse/ | creator/hashtag/video 三端点离线逆向假设（均标 verified:false 待实机校准） |
| docs/monitoring/ | prometheus.yml、alerts.yml、grafana_dashboard.json、pool_dashboard.json |
| work/tk-itemlist-hook/ | 标准化逆向 Case 工作目录（scope 授权契约 → timeline → workitems，append-only） |

**开发方式**：多 Agent 并行（Wave 派发 + 任务卡 + 评审循环）；证据驱动——所有实机结论必须有 probe 证据文件支撑。

---

## 11. 当前状态与已知缺口（截至 2026-10-08）

**已打通**：
- hashtag 快照链路端到端实测通过（真实 API → PG 落库 → 差分 → 调度）
- X-Bogus 签名被真实服务端接受（Wave 5）
- 页面嵌入 JSON 结构证实为 `__UNIVERSAL_DATA_FOR_REHYDRATION__`（证伪 SIGI_STATE）
- 池系统 7 模块全部交付；登录态健康监控已按周期运行

**剩余主缺口**：
1. item_list 门控根因：X-Gnarly 为 query 参数而非请求头，但参数备齐仍 200 空体 → **门控在会话层**（游客身份/环境风险分/数据中心 IP），"复刻请求形状"路线已被证伪；待登录态通道复测消融验证
2. shop 商品页 15 字段解析需账号配合验证码
3. headful + 指纹一致复测以排除环境风险分假设
4. exports/ 数据导出环节尚未正式跑起来

---

## 12. 模块依赖关系图

```
main.py (CLI 入口)
 ├─→ scheduler.TaskScheduler ──→ tasks.yaml ──→ TaskQueue(Redis) ──→ 回调 handler ──→ run_*_scrape
 ├─→ scrapers.* (Shop/Video/Creator/Hashtag/ItemList)
 │     ├─→ parsers.* (← selectors/*.json 配置驱动)
 │     ├─→ signer.py (ItemList 专用 X-Bogus 签名)
 │     └─→ base_scraper ──→ anti_detection.AntiDetectionClient (CurlCffi ⇄ Camoufox 降级)
 │                            ├─→ session_manager (data/session.json)
 │                            └─→ rate_limiter ──→ Redis
 ├─→ pools.integration.pool_scoped_client ──→ lease ──→ cookie_pool ──→ Redis
 │        (IP/指纹池已建成，暂未全量接入抓取链路)
 ├─→ database.SnapshotDiffEngine ──→ models (4 快照表) + connection ──→ PostgreSQL
 ├─→ metrics.MetricsCalculator ──→ database (只读快照)
 ├─→ external_data.Enricher ──→ alibaba/amazon/trends (独立旁路，可降级)
 └─→ monitoring.metrics (Prometheus，各层埋点)

common.{config,logger,exceptions} 被所有模块依赖（最底层）
```

**设计特点**：快照式存储（幂等 upsert、历史可回溯）；选择器与签名算法可注入替换（应对 TikTok 结构变更）；所有外部依赖（DB/Redis/池/外部 API）均有明确的降级路径与退出码语义。
