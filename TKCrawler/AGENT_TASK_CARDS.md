# TikTok 爬虫项目 - Agent 任务卡

> 生成时间: 2026-10-07  
> 项目目标: 构建TikTok跨境选品数据采集系统  
> 并行策略: 能力域分层 + 数据源分离

---

## 📋 任务优先级矩阵

| Phase | Agent | 优先级 | 预估工时 | 依赖关系 | 交付物 |
|-------|-------|--------|---------|---------|--------|
| Phase 1 | Agent A: 反检测基建 | P0 | 1-2天 | 无 | AntiDetectionClient + SessionManager |
| Phase 1 | Agent B: 数据库架构 | P0 | 1-2天 | 无 | 数据库迁移脚本 + SnapshotDiffEngine |
| Phase 1 | Agent C: 调度系统 | P0 | 1-2天 | 无 | TaskScheduler + 任务配置模板 |
| Phase 2 | Agent D: Shop商品页 | P0 | 2-3天 | Agent A | ShopProductScraper |
| Phase 2 | Agent E: 视频流采集 | P0 | 2-3天 | Agent A | VideoStreamScraper |
| Phase 2 | Agent F: 达人主页 | P0 | 1-2天 | Agent A | CreatorProfileScraper |
| Phase 2 | Agent G: 话题采集 | P1 | 1-2天 | Agent A | HashtagTrendScraper |
| Phase 3 | Agent H: 外部数据 | P1 | 2天 | 无 | ExternalDataEnricher |
| Phase 3 | Agent I: 派生指标 | P1 | 1天 | Agent B | MetricsCalculator |
| Phase 4 | Agent J: 监控系统 | P1 | 2天 | 无 | 监控Dashboard |
| Phase 4 | Agent K: 文档工程 | P1 | 1天 | 无 | 部署文档 + API文档 |

---

## 🎯 Phase 1: 基础设施层

### Agent A: 反检测基建工程师

**角色定位**: 反检测技术专家,负责构建绕过TikTok风控的请求通道

**核心职责**:
1. 集成curl_cffi实现chrome131指纹模拟
2. 配置camoufox反检测浏览器获取登录态
3. 实现会话保活与Cookie管理
4. 建立请求速率控制机制(5-8s随机间隔)

**技术选型**:
- **主通道**: curl_cffi (TLS/JA3/JA4/HTTP2指纹)
- **登录态**: camoufox (反检测Firefox + Playwright)
- **备用通道**: invisible_playwright (绕PerimeterX/DataDome)

**参考仓库**:
- lexiforest/curl_cffi (6.7k⭐)
- daijro/camoufox (12.4k⭐)
- feder-cr/invisible_playwright (3.0k⭐)

**交付清单**:
```python
# 文件结构
src/anti_detection/
├── __init__.py
├── curl_client.py          # curl_cffi封装
├── browser_client.py       # camoufox封装
├── session_manager.py      # Cookie/会话管理
├── rate_limiter.py         # 速率控制装饰器
└── config.py               # 指纹配置

# 核心类签名
class AntiDetectionClient:
    def request(self, url, method='GET', headers=None, **kwargs) -> Response
    def switch_channel(self, channel: str)  # 切换curl/browser通道

class SessionManager:
    def get_session(self) -> Session
    def refresh_cookie(self) -> bool
    def is_alive(self) -> bool

@rate_limit(min_interval=5, max_interval=8)
def scrape_with_limit(url):
    pass
```

**验收标准**:
- [ ] curl_cffi成功请求TikTok商品页(返回200且包含商品数据)
- [ ] camoufox成功获取登录态Cookie(验证/api/user/info返回登录用户)
- [ ] 速率控制生效(连续10次请求间隔在5-8s范围内)
- [ ] 遇到WAF时自动切换browser通道

**风险点**:
- TikTok指纹升级导致curl_cffi失效 → 预备invisible_playwright
- 登录态频繁失效 → 实现自动检测+重新登录

---

### Agent B: 数据库架构师

**角色定位**: 数据建模专家,负责设计支持快照差分的时序数据库

**核心职责**:
1. 设计PostgreSQL表结构(商品/视频/达人快照表)
2. 实现快照差分逻辑(计算gmv_growth_7d/30d)
3. 配置分区策略与归档策略
4. 编写数据迁移脚本

**数据库设计**:
```sql
-- 商品快照表(按日分区)
CREATE TABLE product_snapshots (
    id BIGSERIAL,
    product_id VARCHAR(50) NOT NULL,
    snapshot_date DATE NOT NULL,
    title VARCHAR(500),
    price_current DECIMAL(10,2),
    sales_30d INT,
    gmv_30d_est DECIMAL(15,2),
    creator_count INT,
    video_count_7d INT,
    -- ... 其他15字段
    created_at TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (product_id, snapshot_date)
) PARTITION BY RANGE (snapshot_date);

-- 视频快照表
CREATE TABLE video_snapshots (
    video_id VARCHAR(50) PRIMARY KEY,
    product_id VARCHAR(50),
    snapshot_date DATE NOT NULL,
    views BIGINT,
    likes INT,
    -- ...
);

-- GMV增速视图
CREATE VIEW product_growth AS
SELECT 
    curr.product_id,
    curr.gmv_30d_est - prev.gmv_30d_est AS gmv_growth_7d,
    (curr.gmv_30d_est - prev.gmv_30d_est) / NULLIF(prev.gmv_30d_est, 0) AS growth_rate_7d
FROM product_snapshots curr
LEFT JOIN product_snapshots prev 
    ON curr.product_id = prev.product_id 
    AND prev.snapshot_date = curr.snapshot_date - INTERVAL '7 days';
```

**参考架构**:
- Evil0ctal/Douyin_TikTok_Download_API 的PostgreSQL归档设计

**交付清单**:
```python
# 文件结构
src/database/
├── __init__.py
├── models.py               # SQLAlchemy ORM模型
├── snapshot_engine.py      # 快照差分引擎
├── migrations/             # Alembic迁移脚本
│   └── versions/
└── config.py

# 核心类签名
class SnapshotDiffEngine:
    def save_snapshot(self, data: dict, table: str) -> bool
    def calculate_growth(self, product_id: str, days: int) -> dict
    def get_latest_snapshot(self, product_id: str) -> dict
```

**验收标准**:
- [ ] 数据库迁移脚本执行成功(创建所有表和分区)
- [ ] 插入模拟数据后计算gmv_growth_7d正确
- [ ] 分区自动创建(每天创建次日分区)
- [ ] Docker Compose一键启动PostgreSQL

---

### Agent C: 调度系统工程师

**角色定位**: 任务编排专家,负责周期调度与任务队列

**核心职责**:
1. 实现周期调度器(2-3天/次快照)
2. 任务优先级队列(飙升品优先重抓)
3. 故障恢复与指数退避重试
4. 任务状态持久化

**技术选型**:
- **调度器**: APScheduler (轻量级) 或 Celery (分布式)
- **队列**: Redis (任务队列 + 去重)
- **持久化**: PostgreSQL (任务状态表)

**交付清单**:
```python
# 文件结构
src/scheduler/
├── __init__.py
├── task_scheduler.py       # 核心调度器
├── task_queue.py           # 优先级队列
├── retry_policy.py         # 重试策略
└── config/
    └── tasks.yaml          # 任务配置模板

# 核心类签名
class TaskScheduler:
    def schedule_periodic(self, task_name: str, interval: int, priority: int)
    def run_task(self, task_name: str, **kwargs)
    def pause_task(self, task_id: str)
    
class TaskQueue:
    def enqueue(self, task: Task, priority: int)
    def dequeue(self) -> Task
    def mark_complete(self, task_id: str)
```

**任务配置示例**:
```yaml
# tasks.yaml
tasks:
  - name: scrape_shop_products
    type: periodic
    interval: 2d  # 2天一次
    priority: 1
    retry:
      max_attempts: 3
      backoff: exponential
    
  - name: scrape_videos
    type: periodic
    interval: 1d
    priority: 2
```

**验收标准**:
- [ ] 周期任务按配置时间触发(验证日志)
- [ ] 高优先级任务先执行(同时入队2个任务,P1先于P2)
- [ ] 失败任务按指数退避重试(1m, 2m, 4m)
- [ ] 进程重启后任务状态恢复

---

## 🕷️ Phase 2: 数据采集层

### Agent D: Shop商品页解析工程师

**角色定位**: 前端逆向专家,负责解析TikTok Shop商品页(核心P0)

**核心职责**:
1. 类目榜单爬取(/shop/category/...)
2. 商品详情15字段提取(按schema)
3. 销量区间值中位数计算
4. 动态内容渲染处理

**技术难点**:
- **无现成方案**: M4tth1/Tiktok-Shop-Playwright-Crawl已过时
- **动态加载**: 商品页使用React/Next.js,需等待XHR
- **销量区间**: "500-1k" → 取中位数750

**字段清单**(15个核心字段):
```python
REQUIRED_FIELDS = [
    'product_id',        # 从URL提取
    'title',
    'category_l1', 'category_l2', 'category_l3',  # 面包屑
    'price_current', 'price_original',
    'sales_30d',         # 区间值取中位数
    'gmv_30d_est',       # 自算: price * sales
    'listed_date',
    'shop_id', 'shop_name',
    'creator_count',     # "达人带货"模块
    'video_count_7d',
    'review_count', 'rating'
]
```

**参考解析逻辑**:
```python
# 销量区间处理
def parse_sales(sales_text: str) -> int:
    """
    "500-1k" → 750
    "10k+" → 10000
    """
    if '-' in sales_text:
        low, high = sales_text.split('-')
        return (parse_number(low) + parse_number(high)) // 2
    elif '+' in sales_text:
        return parse_number(sales_text.replace('+', ''))
```

**交付清单**:
```python
# 文件结构
src/scrapers/
├── __init__.py
├── shop_scraper.py         # 核心爬虫
├── parsers/
│   ├── product_parser.py   # 商品页解析
│   └── category_parser.py  # 类目榜解析
└── selectors.json          # CSS/XPath选择器配置

# 核心类签名
class ShopProductScraper:
    def scrape_category(self, category_url: str) -> List[str]  # 返回商品ID列表
    def scrape_product(self, product_id: str) -> dict          # 返回15字段
    def parse_sales_range(self, text: str) -> int
```

**验收标准**:
- [ ] 成功抓取类目榜Top100商品ID
- [ ] 商品详情15字段完整率>95%
- [ ] 销量区间值计算正确(人工抽查10个)
- [ ] 遇到空页面/404正确处理(记录日志,不崩溃)

**风险控制**:
- 每商品间隔7-10s
- 单日配额: 200商品页 (避免账号风险)

---

### Agent E: 视频流采集工程师

**角色定位**: 协议逆向专家,负责TikTok视频流数据采集

**核心职责**:
1. 商品关联视频列表抓取(每商品top 10视频)
2. 视频指标提取(views/likes/comments/shares)
3. 购物车锚点识别
4. 签名处理(X-Bogus/X-Secsdk-Csrf-Token)

**技术参考**:
- davidteather/TikTok-Api: 签名生成逻辑
- drawrowfly/tiktok-scraper: 翻页实现

**核心字段**:
```python
VIDEO_FIELDS = [
    'video_id',
    'product_id',          # 购物车锚点
    'author_id',
    'publish_time',
    'views', 'likes', 'comments', 'shares',
    'duration',
    'is_paid',             # 是否投流(Spark Ads标识)
]
```

**签名处理**:
```python
# 参考davidteather实现
class TikTokSigner:
    def generate_x_bogus(self, url: str, user_agent: str) -> str:
        """生成X-Bogus签名"""
        # 学习davidteather/TikTok-Api的实现
        pass
    
    def get_video_feed(self, product_id: str, count=10) -> List[dict]:
        """获取商品关联视频"""
        url = f"https://www.tiktok.com/api/shop/product/videos?product_id={product_id}"
        headers = {
            'X-Bogus': self.generate_x_bogus(url, self.user_agent),
            'User-Agent': self.user_agent,
        }
        return self.session.get(url, headers=headers).json()
```

**交付清单**:
```python
# 文件结构
src/scrapers/
├── video_scraper.py        # 视频爬虫
├── signer.py               # 签名生成器
└── parsers/
    └── video_parser.py

# 核心类签名
class VideoStreamScraper:
    def scrape_product_videos(self, product_id: str, limit=10) -> List[dict]
    def parse_video_stats(self, video_data: dict) -> dict
```

**验收标准**:
- [ ] 成功获取商品关联视频列表(至少5个测试商品)
- [ ] 视频指标解析完整率>90%
- [ ] 签名生成成功率>95%(100次请求<5次403)

**风控策略**:
- 采样策略: 每商品只抓top 10视频(非全量)
- 间隔: 8-12s随机
- 单日配额: 500视频(优先高GMV商品)

---

### Agent F: 达人主页解析工程师

**角色定位**: 社交数据专家,负责达人数据采集

**核心职责**:
1. 达人基础信息(follower_count, avg_views)
2. 橱窗商品列表
3. 带货类目标签提取
4. 达人分层(腰部1-10万粉丝优先)

**核心字段**:
```python
CREATOR_FIELDS = [
    'creator_id',
    'nickname',
    'follower_count',
    'avg_views_30d',       # 采样最近30个视频取中位数
    'category_tags',       # 主页bio + 视频类目
    'product_count',       # 橱窗数量
]
```

**交付清单**:
```python
# 文件结构
src/scrapers/
├── creator_scraper.py
└── parsers/
    └── creator_parser.py

# 核心类签名
class CreatorProfileScraper:
    def scrape_profile(self, creator_id: str) -> dict
    def get_showcase_products(self, creator_id: str) -> List[str]
    def classify_tier(self, follower_count: int) -> str  # nano/micro/mid/macro
```

**验收标准**:
- [ ] 达人主页解析成功率>85%
- [ ] 粉丝数/橱窗数提取准确(人工抽查20个)
- [ ] 腰部达人(1-10万)正确筛选

---

### Agent G: 话题/标签采集工程师

**角色定位**: 趋势分析专家,负责话题热度数据

**核心职责**:
1. 话题页播放量快照
2. Top视频列表(前30个)
3. 话题增速计算(7天差分)

**核心字段**:
```python
HASHTAG_FIELDS = [
    'tag_name',
    'views_total',         # 总播放量
    'snapshot_date',
    # 增速需差分计算
    'views_growth_7d',     # 由SnapshotDiffEngine计算
]
```

**交付清单**:
```python
# 文件结构
src/scrapers/
├── hashtag_scraper.py
└── parsers/
    └── hashtag_parser.py

# 核心类签名
class HashtagTrendScraper:
    def scrape_hashtag(self, tag_name: str) -> dict
    def get_top_videos(self, tag_name: str, limit=30) -> List[str]
```

**验收标准**:
- [ ] 话题播放量提取准确(测试#tiktokmademebuyit等热门标签)
- [ ] 7天增速计算正确(需要两次快照)

---

## 📊 Phase 3: 数据处理层

### Agent H: 外部数据集成工程师

**角色定位**: API集成专家,负责第三方数据源接入

**核心职责**:
1. 1688供应链API(采购价/MOQ/交期)
2. Google Trends验证接口
3. Amazon BSR交叉验证
4. 第三方工具适配(Kalodata/EchoTik可选)

**技术选型**:
- **不自建**: 使用现成SDK/API
- **1688**: 阿里巴巴开放平台API
- **Trends**: pytrends库
- **Amazon**: amazon-paapi或爬虫

**交付清单**:
```python
# 文件结构
src/external_data/
├── __init__.py
├── enricher.py             # 主入口
├── alibaba_client.py       # 1688接口
├── trends_client.py        # Google Trends
└── amazon_client.py        # Amazon BSR

# 核心类签名
class ExternalDataEnricher:
    def enrich_product(self, product_data: dict) -> dict
    def get_supplier_info(self, product_title: str) -> dict  # 1688搜索
    def get_trends_score(self, keyword: str) -> float
    def get_amazon_bsr(self, asin: str) -> dict
```

**验收标准**:
- [ ] 1688搜索返回供应商信息(测试10个商品标题)
- [ ] Trends得分提取成功(测试关键词"LED lights")
- [ ] Amazon BSR查询成功(测试ASIN: B08N5WRWNW)

---

### Agent I: 派生指标计算工程师

**角色定位**: 数据分析专家,负责业务指标计算

**核心职责**:
计算4个派生指标(不采集,入库后计算):
1. 内容效率 = gmv_30d_est ÷ video_count_7d
2. 达人渗透率 = creator_count ÷ sales_30d
3. 饱和度指数 = (creator_count × video_count_7d) / gmv_30d_est
4. 飙升系数 = gmv_growth_30d ÷ 类目平均增速

**交付清单**:
```python
# 文件结构
src/metrics/
├── __init__.py
├── calculator.py           # 指标计算器
└── sql_views.sql           # SQL视图定义

# 核心类签名
class MetricsCalculator:
    def calculate_content_efficiency(self, product_id: str) -> float
    def calculate_creator_penetration(self, product_id: str) -> float
    def calculate_saturation_index(self, product_id: str) -> float
    def calculate_surge_coefficient(self, product_id: str, category: str) -> float
```

**SQL视图示例**:
```sql
CREATE VIEW product_metrics AS
SELECT 
    p.product_id,
    p.gmv_30d_est / NULLIF(p.video_count_7d, 0) AS content_efficiency,
    p.creator_count / NULLIF(p.sales_30d, 0) AS creator_penetration,
    (p.creator_count * p.video_count_7d) / NULLIF(p.gmv_30d_est, 0) AS saturation_index
FROM product_snapshots p;
```

**验收标准**:
- [ ] 所有指标计算结果合理(无除零错误)
- [ ] SQL视图查询性能<100ms(测试1000条数据)

---

## 🔧 Phase 4: 工程化增强

### Agent J: 监控与可观测性工程师

**角色定位**: SRE专家,负责系统监控与告警

**核心职责**:
1. 请求成功率监控(Prometheus metrics)
2. WAF拦截告警(Slack/Email通知)
3. 数据质量检查(空字段率、增速异常)
4. Grafana Dashboard配置

**技术选型**:
- **监控**: Prometheus + Grafana
- **告警**: Alertmanager
- **追踪**: Sentry(错误追踪)

**监控指标**:
```python
# Prometheus metrics
scraper_requests_total{scraper="shop", status="success"}
scraper_request_duration_seconds{scraper="shop"}
scraper_waf_blocks_total{scraper="shop"}
data_quality_missing_fields_ratio{table="product_snapshots"}
```

**交付清单**:
```
docs/monitoring/
├── prometheus.yml          # Prometheus配置
├── alerting_rules.yml      # 告警规则
├── grafana_dashboard.json  # Dashboard JSON
└── README.md
```

**验收标准**:
- [ ] Grafana显示实时请求成功率
- [ ] WAF拦截触发Slack告警(模拟测试)
- [ ] 数据质量仪表盘展示空字段率

---

### Agent K: 文档与配置工程师

**角色定位**: DevOps专家,负责部署文档与配置管理

**核心职责**:
1. 环境配置模板(.env.example)
2. Docker一键部署(docker-compose.yml)
3. API文档(如需对外提供)
4. 字段说明文档

**交付清单**:
```
TKCrawler/
├── README.md               # 项目总览
├── .env.example            # 环境变量模板
├── docker-compose.yml      # Docker编排
├── docs/
│   ├── DEPLOYMENT.md       # 部署指南
│   ├── FIELD_SCHEMA.md     # 字段说明
│   └── API.md              # API文档(可选)
└── scripts/
    └── setup.sh            # 一键安装脚本
```

**环境变量模板**:
```bash
# .env.example
# TikTok账号(用于获取登录态)
TIKTOK_USERNAME=your_username
TIKTOK_PASSWORD=your_password

# 数据库
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=tiktok_crawler
POSTGRES_USER=crawler
POSTGRES_PASSWORD=secret

# Redis
REDIS_HOST=localhost
REDIS_PORT=6379

# 代理(可选)
PROXY_HOST=127.0.0.1
PROXY_PORT=7892

# 速率限制
REQUEST_MIN_INTERVAL=5
REQUEST_MAX_INTERVAL=8
DAILY_QUOTA_PRODUCTS=200
DAILY_QUOTA_VIDEOS=500
```

**验收标准**:
- [ ] docker-compose up -d成功启动所有服务
- [ ] README包含完整的快速开始指南
- [ ] FIELD_SCHEMA.md准确描述15字段含义

---

## 📅 实施路线图

### Week 1: 基础设施搭建
**Day 1-2**: 
- Agent A: 反检测基建 (并行)
- Agent B: 数据库架构 (并行)
- Agent C: 调度系统 (并行)

**验收里程碑**: 
- curl_cffi成功请求TikTok
- PostgreSQL建表完成
- 调度器运行第一个测试任务

---

### Week 2: 核心采集实现
**Day 3-5**:
- Agent D: Shop商品页 (阻塞后续)
- Agent E: 视频流 (并行)
- Agent F: 达人主页 (并行)

**验收里程碑**:
- 成功抓取100个商品的15字段
- 成功抓取50个视频数据
- 成功抓取20个达人数据

---

### Week 3: 数据增强与监控
**Day 6-8**:
- Agent G: 话题采集 (P1)
- Agent H: 外部数据集成 (并行)
- Agent I: 派生指标计算 (并行)
- Agent J: 监控系统 (并行)

**验收里程碑**:
- 完整数据流打通(商品→视频→达人→外部验证→指标计算)
- Grafana Dashboard上线

---

### Week 4: 工程化与部署
**Day 9-10**:
- Agent K: 文档与配置
- 全流程集成测试
- 生产环境部署

**验收里程碑**:
- Docker一键部署成功
- 文档齐全可交付

---

## 🚨 风险管理

### 技术风险
| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|---------|
| TikTok指纹升级 | 高 | 高 | 预备3个通道(curl/camoufox/invisible_playwright) |
| 登录态频繁失效 | 中 | 中 | 实现自动检测+重登录 |
| Shop页面结构变化 | 高 | 高 | 选择器配置外部化(JSON),易于更新 |
| 数据库性能瓶颈 | 低 | 中 | 分区表+索引优化 |

### 合规风险
| 风险 | 缓解措施 |
|------|---------|
| 账号封禁 | 单会话+严格速率限制+软冷却机制 |
| 法律风险 | 只抓公开数据,不做池化绕过 |

---

## 🔗 Agent间接口契约

### Agent A → Agents D/E/F/G
```python
# 接口: AntiDetectionClient.request()
# 输入: url, method, headers, **kwargs
# 输出: Response对象
# 异常: WAFBlockedException, RateLimitException
```

### Agent B → Agent I
```python
# 接口: SnapshotDiffEngine.calculate_growth()
# 输入: product_id, days
# 输出: {'gmv_growth': float, 'growth_rate': float}
```

### Agent C → Agents D/E/F/G
```python
# 接口: TaskScheduler.schedule_periodic()
# 输入: task_name, interval, priority
# 输出: task_id
```

### Agents D/E/F/G → Agent B
```python
# 接口: SnapshotDiffEngine.save_snapshot()
# 输入: data (dict), table (str)
# 输出: success (bool)
```

---

## 📞 协作规范

1. **每日站会**: 同步进度与阻塞点
2. **代码审查**: 所有PR需至少1人Review
3. **测试覆盖**: 核心逻辑测试覆盖率>80%
4. **文档同步**: 每完成一个模块更新README

---

## 📚 参考资源汇总

### 开源仓库
- Evil0ctal/Douyin_TikTok_Download_API (20.5k⭐) - 架构参考
- davidteather/TikTok-Api (6.7k⭐) - 签名处理
- lexiforest/curl_cffi (6.7k⭐) - 指纹模拟
- daijro/camoufox (12.4k⭐) - 反检测浏览器

### 文档
- `tk-selection-data-schema.md` - 字段定义
- `tk-scraping-purpose.md` - 业务目标
- `tk-crawler-github-references.md` - 技术参考

