# amazonCrawler — Agent 接口契约（所有子 Agent 必须遵守）

本文件是并行开发的唯一契约源。写代码前先读完本文件。

## 项目路径
- 项目根: /Users/wuwenbo/Documents/competitor-monitor/amazonCrawler
- Python: /Library/Frameworks/Python.framework/Versions/3.11/bin/python3
- 运行测试: cd 项目根 && /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 -m pytest tests/ -x -q
- 真实 HTML fixture: test/amazon/amzpy/us-preferences-fixed/{B0DLQ8MVF7,B0GWVTMR4Q,B0H22J6F1B}.html
  （17KB/13KB 裁剪样本，含 productTitle/apex price/BSR 项/acrCustomerReviewText/Date First Available/In Stock/变体 dropdown）

## 模块归属（禁止越界写别人目录）
- Agent A: src/common/ + src/database/ + scripts/init_db.py + requirements.txt + .env.example + pyproject/test 配置
- Agent B: src/anti_detection/ + src/pools/
- Agent C: src/scrapers/ + tests/fixtures/amazon/*.html 拷贝 + 对应单测
- Agent D(后派): src/scheduler/ + src/metrics/ + src/monitoring/ + src/service/
- main.py 与 mcp server 由主线写

## 代码风格约定（沿用 TKCrawler）
- SQLAlchemy 2.0 Mapped/mapped_column 风格
- 日志: from src.common.logger import get_logger; logger = get_logger(__name__)
- 异常基类 CrawlerException，全部放 src/common/exceptions.py
- 中文 docstring 允许，注释解释"为什么"而非"是什么"
- 每个模块带离线单测；测试不发起真实网络请求；DB 测试用 SQLite 内存库（引擎双方言：PG 生产 / SQLite 测试）
- __init__.py 导出公共符号

## 关键数据契约

### 表结构（Agent A 实现 src/database/models.py，三张表，主键=实体ID+snapshot_date）
1. asin_snapshots: asin(String 10, PK), snapshot_date(Date, PK), marketplace(String 20),
   title(String 500), brand(String 200), seller_name(String 200), buybox_winner(String 200),
   price_current(Numeric 10,2), price_original(Numeric 10,2), currency(String 3, default 'USD'),
   price_scope(String 30), availability(String 100), coupon_text(String 200),
   rating(Numeric 2,1), rating_count(Integer),
   bsr_root_category(String 200), bsr_ranks(JSONB Text — 用 sqlalchemy JSON 列，双方言),
   variant_status(String 20), variant_asin(String 10),
   comparison_ready(Boolean), region_verified(Boolean), created_at(DateTime server_default now)
   索引: (marketplace, snapshot_date), (bsr_root_category)
2. keyword_rank_snapshots: keyword(String 200, PK), asin(String 10, PK), snapshot_date(Date, PK),
   natural_rank(Integer), ad_rank(Integer), page(Integer), result_count(Integer), created_at
3. offer_snapshots: asin(String 10, PK), seller_id(String 50, PK), snapshot_date(Date, PK),
   seller_name(String 200), price(Numeric 10,2), is_buybox(Boolean), is_amazon_offered(Boolean),
   offer_count(Integer), created_at

### 快照引擎（Agent A 实现 src/database/snapshot_engine.py，类 SnapshotDiffEngine）
参考 TKCrawler/src/database/snapshot_engine.py 的实现，契约：
- save_snapshot(data: dict, table: str) -> bool   # upsert, 白名单三张表
- get_latest_snapshot(asin: str, table='asin_snapshots') -> dict|None
- calculate_growth(asin: str, days: int, field='price_current') -> dict
  # 返回 {latest, past, change, change_rate}，None 语义=不可算（除零/缺快照），不返回0兜底
- PG 用 pg_insert.on_conflict_do_update；SQLite 用 sqlite_insert.on_conflict_do_update
- 构造器接收可选 session_factory（测试注入 SQLite 内存库）

### 异常体系（Agent A 实现 src/common/exceptions.py）
- CrawlerException(Exception) 基类
- BotBlockedException(CrawlerException)  # Amazon 503/captcha 页（对应 TK 的 WAFBlockedException）
- RateLimitException, SessionExpiredException, ParsingException(field, reason), QuotaExceededException(resource_type, limit)

### 配置（Agent A 实现 src/common/config.py，参考 TKCrawler/src/common/config.py）
环境变量前缀 AMZ_ 或沿用通用名：POSTGRES_*(DATABASE_URL 优先)、REDIS_*、
PROXY_ENABLED/PROXY_HOST/PROXY_PORT/PROXY_TYPE、REQUEST_MIN_INTERVAL=8、REQUEST_MAX_INTERVAL=15、
DAILY_QUOTA_ASINS=200、DAILY_QUOTA_SEARCH=50、SOFT_COOLDOWN_SECONDS=3600、
MARKETPLACE=www.amazon.com、TIMEZONE、LOG_LEVEL、LOG_FILE、EXPORT_DIR、DB_PATH(备用SQLite路径)

### 配置（Agent B）
- CurlCffiClient: impersonate='chrome131'，方法 request(url, method='GET', headers=None, **kw)->Response；
  响应正文启发式 looks_like_bot_challenge(text)->bool：
  强特征= HTTP503状态(由调用方判) + 正文: <title>含 "Sorry! Something went wrong"/"Robot Check"/"Access Denied"/"captcha"；
  正文含 api-services-support@amazon.com；含 captcha-image / aws-waf-token script。
  注意：正常页正文含 "captcha" 资源名不误报（参考 TK curl_client 的四强特征设计）。
- RateLimiter: 随机区间 wait()、Redis 日配额 check_quota(resource_type, limit)（Redis 不可用降级本地计数）— 参考 TK rate_limiter.py
- SessionManager: data/amz_session.json 存 cookie dict，load/save/clear/has_session
- IP池: pools/ip_pool.py — 简版：list[str] 代理 URL 轮询 + 失败标记冷却，acquire()/report_failure(url)/healthy_count()
- integration.py: get_client() 上下文 — 构造 CurlCffiClient 并注入代理（PROXY_ENABLED 或 IP池）

### 爬虫层（Agent C）
- BaseScraper 参考 TKCrawler/src/scrapers/base_scraper.py：
  client 注入、RateLimiter、日配额、503/ BotBlocked 软冷却（cooldown 状态 + 抛 BotBlockedException）、Prometheus 埋点降级 no-op
- PdpScraper.scrape_product(asin, marketplace='US') -> dict|None（字段名=models 的列名，直接可喂 save_snapshot('asin_snapshots')）
- 解析器 parsers/pdp_parser.py：PdpParser.parse(html:str)->dict，BeautifulSoup 实现：
  title(#productTitle), price(.a-price .a-offscreen / apex_desktop 优先 priceToPay), rating(#acrPopover title="4.6 out of 5 stars"),
  rating_count(#acrCustomerReviewText "(104)"), availability(#availability 含 In Stock),
  brand(#bylineInfo), bsr_ranks(详情项 Best Sellers Rank: 主类目数字 + ul.zg_hrsr 子项, 解析 "#729,415 in Clothing, Shoes & Jewelry" → [{category, rank}]),
  date_first_available, variant_asin 从 dropdown option "0,MC_Custom_1##B0752XJYNL" 提取,
  comparison_ready/region_verified/price_scope=('display_page_price') 固定口径声明
- SearchScraper / OfferScraper：结构写好、解析器给最佳努力实现 + 单测用构造的最小 HTML fixture（真实搜索/offer页样本没有，不阻塞）

## 测试约束
- tests/test_*.py 命名；conftest.py 放项目根 tests/
- DB 单测: sqlite memory + Base.metadata.create_all
- 解析单测: 读 tests/fixtures/amazon/*.html（Agent C 负责从 test/amazon 拷贝）
- 全部测试必须无网络

## 完成定义
- 模块可 import：PYTHONPATH=项目根 python3 -c "from src.xxx import Yyy" 无错
- pytest 该模块单测全绿
- 不修改本契约文件
