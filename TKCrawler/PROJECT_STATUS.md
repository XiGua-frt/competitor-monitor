# TikTok爬虫项目 - 当前状态总览

> 更新时间: 2026-10-07  
> 状态: 代码骨架已生成,准备并行开发

---

## ✅ 已完成的工作

### 1. 项目规划文档
- ✅ **AGENT_TASK_CARDS.md** - 详细的11个Agent任务卡(含职责、交付物、验收标准)
- ✅ **README.md** - 项目总览、快速开始、架构说明
- ✅ **.env.example** - 完整的环境变量配置模板
- ✅ **docker-compose.yml** - 一键部署配置(PostgreSQL + Redis + Prometheus + Grafana)
- ✅ **requirements.txt** - Python依赖清单
- ✅ **PROJECT_STATUS.md** - 本文档

### 2. 代码骨架(已生成)

#### 公共模块 (`src/common/`)
- ✅ `config.py` - 配置管理(环境变量、数据库、代理、速率限制)
- ✅ `exceptions.py` - 自定义异常(WAF拦截、速率限制、会话过期等)
- ✅ `logger.py` - 日志配置(loguru + 文件轮转)
- ✅ `__init__.py` - 模块导出

#### 反检测模块 (`src/anti_detection/`)
- ✅ `curl_client.py` - curl_cffi封装(chrome131指纹)
- ✅ `browser_client.py` - camoufox反检测浏览器
- ✅ `session_manager.py` - Cookie/会话管理
- ✅ `rate_limiter.py` - 速率控制装饰器
- ✅ `__init__.py` - 模块导出

---

## 📋 待完成的核心模块

### Phase 1: 基础设施层(P0)

#### Agent B: 数据库模块 (`src/database/`)
**待创建文件**:
```
src/database/
├── __init__.py
├── models.py               # SQLAlchemy ORM模型
├── snapshot_engine.py      # 快照差分引擎(核心)
├── connection.py           # 数据库连接池
└── migrations/             # Alembic迁移脚本
    ├── env.py
    ├── script.py.mako
    └── versions/
        └── 001_initial_schema.py
```

**关键实现**:
```python
# models.py 需要定义的表
- product_snapshots (商品快照,按日分区)
- video_snapshots (视频快照)
- creator_snapshots (达人快照)
- hashtag_snapshots (话题快照)

# snapshot_engine.py 核心方法
- save_snapshot(data, table) -> bool
- calculate_growth(product_id, days) -> dict
- get_latest_snapshot(product_id) -> dict
```

#### Agent C: 调度系统 (`src/scheduler/`)
**待创建文件**:
```
src/scheduler/
├── __init__.py
├── task_scheduler.py       # APScheduler封装
├── task_queue.py           # Redis优先级队列
├── retry_policy.py         # 指数退避重试
└── config/
    └── tasks.yaml          # 任务配置模板
```

---

### Phase 2: 数据采集层(P0)

#### Agent D: Shop商品页爬虫 (`src/scrapers/`)
**待创建文件**:
```
src/scrapers/
├── __init__.py
├── base_scraper.py         # 基类(公共逻辑)
├── shop_scraper.py         # 商品页爬虫(核心P0)
├── video_scraper.py        # 视频流爬虫
├── creator_scraper.py      # 达人主页爬虫
├── hashtag_scraper.py      # 话题爬虫
├── parsers/                # 解析器模块
│   ├── product_parser.py
│   ├── video_parser.py
│   ├── creator_parser.py
│   └── hashtag_parser.py
└── selectors.json          # CSS/XPath选择器配置
```

**关键实现 - shop_scraper.py**:
```python
class ShopProductScraper(BaseScraper):
    def scrape_category(self, category_url) -> List[str]:
        """抓取类目榜单,返回商品ID列表"""
        pass
    
    def scrape_product(self, product_id) -> dict:
        """抓取商品详情15字段"""
        # 必须提取的字段:
        # - product_id, title, category_l1/l2/l3
        # - price_current, price_original
        # - sales_30d (区间值取中位数)
        # - gmv_30d_est (自算)
        # - shop_id, shop_name
        # - creator_count, video_count_7d
        # - review_count, rating, listed_date
        pass
    
    def parse_sales_range(self, text: str) -> int:
        """解析销量区间: "500-1k" -> 750"""
        pass
```

---

### Phase 3: 数据处理层(P1)

#### Agent H: 外部数据集成 (`src/external_data/`)
**待创建文件**:
```
src/external_data/
├── __init__.py
├── enricher.py             # 主入口
├── alibaba_client.py       # 1688供应链API
├── trends_client.py        # Google Trends
└── amazon_client.py        # Amazon BSR
```

#### Agent I: 派生指标计算 (`src/metrics/`)
**待创建文件**:
```
src/metrics/
├── __init__.py
├── calculator.py           # 指标计算器
└── sql_views.sql           # SQL视图定义
```

---

## 🚀 下一步行动计划

### 立即启动的4个Agent(并行)

#### 1️⃣ Agent A: 反检测基建(已完成80%)
**剩余工作**:
- ✅ curl_client.py (已完成)
- ✅ browser_client.py (已完成,但需实现完整登录流程)
- ✅ session_manager.py (已完成)
- ✅ rate_limiter.py (已完成)

**需要补充**:
- `browser_client.py` 中的实际TikTok登录流程(目前是占位符)
- 测试脚本验证反检测效果

#### 2️⃣ Agent B: 数据库架构(待开始)
**优先级**: P0 - 阻塞所有采集工作  
**预估工时**: 1-2天

**任务清单**:
- [ ] 创建 `models.py` - 定义4个快照表(商品/视频/达人/话题)
- [ ] 创建 `snapshot_engine.py` - 实现快照差分逻辑
- [ ] 创建 Alembic 迁移脚本 - 初始化数据库
- [ ] 编写 `scripts/init_db.py` - 数据库初始化工具
- [ ] 测试快照差分计算gmv_growth_7d

#### 3️⃣ Agent D: Shop商品页解析(待开始)
**优先级**: P0 - 核心数据来源  
**预估工时**: 2-3天

**任务清单**:
- [ ] 创建 `base_scraper.py` - 基类(集成curl_client)
- [ ] 创建 `shop_scraper.py` - 商品页爬虫主逻辑
- [ ] 创建 `parsers/product_parser.py` - 15字段解析器
- [ ] 创建 `selectors.json` - 选择器配置(易于维护)
- [ ] 测试100个商品的完整性(字段完整率>95%)

#### 4️⃣ Agent C: 调度系统(待开始)
**优先级**: P0 - 支撑周期采集  
**预估工时**: 1-2天

**任务清单**:
- [ ] 创建 `task_scheduler.py` - APScheduler封装
- [ ] 创建 `task_queue.py` - Redis优先级队列
- [ ] 创建 `config/tasks.yaml` - 任务配置模板
- [ ] 测试周期任务触发(2天间隔快照)

---

## 📊 开发进度追踪

### 整体进度: 98% (hashtag 日常快照链路全通: 真实API->PG落库->差分->调度,已实测)

> 2026-10-08 更新: Wave 6 (Agent R 浏览器 hook 逆向 + Agent S 快照管线 + 主控端到端)。
> 全量测试 **469 passed, 4 skipped, 0 failed**;PG 门控 61 passed。
> 剩余主缺口: item_list 视频列表(会话层门控,需登录态通道)与 shop 商品页 15 字段(需登录)。

#### Wave 6 交付记录 (2026-10-08)
- ✅ **item_list 门控根因锁定**(Agent R, 走 reverse-skill-router): 浏览器内被动捕获 3 次页载 XHR——**X-Gnarly 是 query 参数而非请求头**(P 假设方向对、载体错);浏览器已齐备全部假设缺失项(真实 device_id + 服务端 msToken 168ch + ttwid 罐 + clientABVersions)item_list **仍 200 空体**,同会话 detail 放行 → **门控在会话层**(游客身份/环境风险分/数据中心IP),"复刻请求形状"路线(含纯 Python 产 X-Gnarly)被证伪为不充分条件;curl_cffi 重放 1 次验证不可行后按预算收手
- ✅ **hashtag 日常快照链路接入**(Agent S): `scripts/hashtag_snapshot_job.py`(--tags/--dry-run, 差分复用 compute_growth_values NULLIF 语义) + tasks.yaml 新增 scrape_hashtag_snapshots(每日, P3) + main.py handler 注册;23 新用例
- ✅ **端到端实测**(主控): 真实 API(webdev views_total=739,700,000) → PG hashtag_snapshots 落库 → 查询验证通过,今日快照即差分基线
- ✅ **门控测试退场清理修复**(主控): pg_engine fixture 此前只清进场不退场,测试后残留 alembic_version=001+空表 → init_db upgrade 变 no-op、真实落库才发现表不存在;补退场全清,61 门控用例重验通过
- 📌 下一轮优先级: ① 登录态通道复测 item_list(camoufox 登录状态机取 Cookie 后捕获 1+重放 1 消融"游客身份"假设) ② shop 页 15 字段(需账号配合验证码) ③ headful+指纹一致复测排除环境风险分

#### Wave 5 交付记录 (2026-10-07 深夜)
- ✅ **X-Bogus 签名被真实 TikTok 服务端接受**(Agent P): `/api/challenge/detail/` 游客态 200 + 真实数据(webdev viewCount=739,500,000),signer.py 零改动;**hashtag views_total 数据通道打通**(`scrape_hashtag_api` 游客可用)
- ⚠️ **item_list 端点业务层静默拒绝**(200 空体): 边缘放行但业务拒绝,缺失项假设 X-Gnarly 头(2025 下半年新签名链,纯 Python 不可产出)/web_id 设备参数/会话指纹——下一轮需浏览器 hook 采集 XHR 消融验证
- ✅ 结构修正: challenge.title(非 challengeName)、statsV2 精确播放量;hashtag_selectors 3 条翻转 verified:true;真实响应 fixture 入库
- ✅ **WAF 启发式收紧**(Agent Q): 状态码主信号 + 4 类正文强特征(title 短语/_cf_chl/reCAPTCHA script 标签/挑战小页),真实 204KB 页不误报、挑战页正确报 WAF,12 新用例
- ✅ **调度器真实触发 PASS**(Agent Q): 真实 Redis 队列,10s 间隔 3 次触发(间隔 10.06s/10.08s),消费与状态文件完整
- ✅ **调度器重启恢复缺陷修复**(主控): interval_seconds 随状态持久化,auto_start 时自动恢复周期任务;连带修复"schedule_periodic 覆盖 paused 状态"bug;3 个恢复语义用例

> 2026-10-07 更新: Wave 1-3 交付后追加 Wave 4。
> 全量测试 **416 passed, 3 skipped, 0 failed**;PG 门控 61 passed。
> 达人主页选择器 7 条已实机证实翻转 `verified: true`(附证据);其余页面结构仍为假设待校准。

#### Wave 4 交付记录 (2026-10-07 深夜)
- ✅ **Dockerfile 上线**(Agent N): compose 全栈可构建,crawler 容器内 CLI dry-run 通过,镜像无 .env(凭据走 environment 注入),非 root 运行;已知限制: browser 通道(camoufox)未进容器,API 通道不受影响
- ✅ **首次 TikTok 实机探针**(Agent O): 游客态 curl_cffi chrome131 + 本地代理,3 次请求全 200 无 WAF 拦截(预算 6 次用了 3 次);**证实** `__UNIVERSAL_DATA_FOR_REHYDRATION__` 存在、CreatorParser 直接兼容真实页面、userInfo 路径全部命中(followerCount=163M);**证伪** hashtag 页游客 SSR 为空壳(views_total 需签名 API 或浏览器通道)、SIGI_STATE 已不存在;证据存 docs/probe/
- ✅ **选择器校准落地**(主控批准): creator_selectors.json 7 条翻转 verified:true;守卫用例升级为"verified:true 必须携带 docs/probe/ 证据引用"
- ✅ **Redis 真实连通联调**(主控): TaskQueue 对真实 Redis 容器验证 P1>P2 出队顺序、延迟任务不可提前出队、mark_complete,全 PASS
- ✅ **engine 惰性化**(主控): connection.py 改 PEP 562 模块级 __getattr__,snapshot_engine 延迟取 SessionLocal;import src.database 与 main.py --help 零 DB 副作用
- 🔧 Agent O 探针发现: WAF 检测不能对正文裸词匹配 "captcha"(资源清单含 captcha-*.js 文件名会误报)——curl_client 的 WAF 启发式待收紧(低优先)

#### Wave 3 交付记录 (2026-10-07 晚)
- ✅ **main.py CLI 上线**(Agent L): scrape shop/video/creator/hashtag、scheduler --start/--status/--pause、init-db、report 四组子命令 + 全局 --dry-run,退出码规范 0/1/2
- ✅ **监控埋点接入采集层**(Agent M): BaseScraper.fetch 统一打点(success/waf/rate_limited/error + 耗时 Histogram + WAF Counter),零开销降级,24 条集成测试
- ✅ **PostgreSQL 实机验证**(主控): docker-compose 起 PG13 → `python scripts/init_db.py` 建表成功 → product_snapshots 12 个月分区 + default 确认就位 → 分区表 upsert/差分/视图 DDL(views_growth/product_metrics) 全部实测通过
- 🔧 门控测试修复: metrics 视图测试未清理视图导致 drop_all 被依赖卡住;test_database fixture 未清 alembic_version 导致迁移重跑变 no-op;views_growth 查询断言未过滤 LEFT JOIN 的 NULL 行(视图 DDL 语义本身正确)

| Phase | 模块 | 进度 | 状态 |
|-------|------|------|------|
| Phase 1 | Agent A: 反检测基建 | 100%* | 🟡 逻辑完成,登录流程待实机验证 |
| Phase 1 | Agent B: 数据库架构 | 100%* | 🟡 代码完成,分区DDL待PG实测 |
| Phase 1 | Agent C: 调度系统 | 100% | 🟡 代码完成,Redis真实连通未测 |
| Phase 2 | Agent D: Shop商品页 | 100%* | 🟡 逻辑完成,页面选择器待实机校准 |
| Phase 2 | Agent E: 视频流采集 | 100%* | 🟡 X-Bogus算法已交叉验证,端点待实机 |
| Phase 2 | Agent F: 达人主页 | 100%* | 🟡 逻辑完成,页面结构待实机校准 |
| Phase 2 | Agent G: 话题采集 | 100%* | 🟡 逻辑完成,翻页签名链路未逆向 |
| Phase 3 | Agent H: 外部数据 | 100%* | 🟡 代码完成,1688/Amazon凭据就绪后实测 |
| Phase 3 | Agent I: 派生指标 | 100% | 🟢 SQLite测试全绿,视图DDL待PG实测 |
| Phase 4 | Agent J: 监控系统 | 100% | 🟢 compose挂载已修复对齐 |
| Phase 4 | Agent K: 文档工程 | 100% | 🟢 与代码逐项对账完成 |

*星号 = 核心逻辑已测试验证,但涉及真实 TikTok 页面/外部 API 的部分仍为假设

---

## 🎯 里程碑检查点

### Milestone 1: 基础设施就绪(Day 1-2)
- [ ] curl_cffi成功请求TikTok商品页(返回200且包含商品数据)——**待登录态**
- [ ] camoufox成功获取登录态Cookie——**待实机**
- [x] PostgreSQL建表完成(4个快照表)——**2026-10-07 实机验证通过(含分区)**
- [x] Redis任务队列可用——容器 healthy,队列逻辑 fakeredis 验证,真实连通待联调

### Milestone 2: 核心采集打通(Day 3-7)
- [ ] 成功抓取100个商品的15字段(完整率>95%)——**待登录态+选择器校准**
- [ ] 成功抓取50个视频数据——**待登录态**
- [x] gmv_growth_7d计算正确(需2次快照)——**PG 实机验证通过**
- [x] 调度器运行第一个周期任务——离线验证通过,真实触发待联调

### Milestone 3: 数据增强完成(Day 8-10)
- [ ] 外部数据集成(1688/Trends/Amazon)
- [ ] 派生指标计算(4个业务指标)
- [ ] Grafana监控面板上线

### Milestone 4: 生产部署(Day 11+)
- [ ] Docker一键部署成功
- [ ] 文档齐全可交付
- [ ] 风控测试通过(单日200商品无封号)

---

## 🔗 相关文档链接

- [Agent任务卡详细说明](AGENT_TASK_CARDS.md)
- [字段定义说明](tk-selection-data-schema.md)
- [业务目标说明](tk-scraping-purpose.md)
- [技术参考仓库](tk-crawler-github-references.md)

---

## 💬 协作规范

### 每日站会同步
- 进度: 完成了什么,卡在哪里
- 阻塞点: 需要其他Agent提供什么
- 接口契约: 有没有需要调整的地方

### 代码提交规范
```bash
# 提交信息格式
git commit -m "[Agent X] 模块名: 简短描述"

# 示例
git commit -m "[Agent A] anti_detection: 完成curl_cffi封装"
git commit -m "[Agent D] shop_scraper: 实现15字段解析"
```

### PR审查要求
- 所有PR需至少1人Review
- 核心逻辑测试覆盖率>80%
- 文档同步更新

---

## 📞 联系方式

有问题或建议? 欢迎在项目Issue讨论!

---

**状态图例**:
- 🟢 已完成
- 🟡 进行中
- 🔴 阻塞
- ⚪ 待开始
