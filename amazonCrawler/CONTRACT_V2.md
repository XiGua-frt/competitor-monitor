# 契约补充 v2 — service 层与 scheduler 接口（Agent D/E 必读）

P1-P4 已完成并全量测试通过（基线：pytest tests/ 全绿）。本文件补充 service/scheduler/metrics/MCP 的接口契约，接续 CONTRACT.md 的风格约定。禁止修改 CONTRACT.md 与本文件。

## 已就绪的真实接口（直接使用，勿假设）

```python
# src/common
from src.common.config import config          # .DATABASE_URL/.REDIS_*/.DAILY_QUOTA_ASINS/.EXPORT_DIR 等
from src.common.logger import get_logger      # get_logger(__name__)
from src.common.exceptions import (CrawlerException, BotBlockedException,
    RateLimitException, SessionExpiredException, ParsingException, QuotaExceededException)

# src/database
from src.database import SnapshotDiffEngine   # save_snapshot(data, table)->bool;
                                              # get_latest_snapshot(asin, table)->dict|None;
                                              # calculate_growth(asin, days, field)->{latest,past,change,change_rate}
from src.database.models import AsinSnapshot, KeywordRankSnapshot, OfferSnapshot  # 表名以 models.py 实际类名为准（先读它）
from src.database.connection import get_session, SessionLocal  # 先读 connection.py 确认导出名

# src/scrapers
from src.scrapers.pdp_scraper import PdpScraper       # scrape_product(asin, marketplace='US')->dict|None
from src.scrapers.search_scraper import SearchScraper # search(keyword, page=1)->list[dict]
from src.scrapers.offer_scraper import OfferScraper   # scrape_offers(asin)->list[dict]
# PdpScraper 返回 dict 键 = asin_snapshots 列名；BotBlockedException 时抛出
```
⚠️ 动手前先 grep/读实际文件确认类名与签名（models.py 的表类名、scrapers 的 __init__ 导出），以上以实际代码为准。

## Agent D 交付物

### 1. src/monitoring/metrics.py（参考 TKCrawler/src/monitoring/metrics.py）
Prometheus 指标：scraper_requests_total{scraper,status} Counter、scraper_request_duration_seconds Histogram、
amazon_bot_blocks_total{scraper} Counter、data_quality_missing_ratio{table} Gauge；
辅助函数 observe_request(scraper,status,duration)/inc_bot_block(scraper)/set_missing_ratio(table,ratio)；
注册幂等；未装 prometheus_client 时全降级 no-op；可选 start_metrics_server(port)。

### 2. src/metrics/calculator.py（MetricsCalculator，只读）
- __init__(session_factory=None, engine=None)
- price_change(asin, days) -> {latest, past, change, change_rate}（复用 SnapshotDiffEngine.calculate_growth(field='price_current')）
- bsr_growth(asin, days)（field='bsr_root_category' 不适用差分——BSR 是 JSON，改从两次快照的 bsr_ranks 首条 rank 对比，自己查 get_latest_snapshot + 历史快照）
- review_velocity(asin, days)（rating_count 差分 → 每日均值；除零 None）
- availability_downtime(asin, days)（扫描快照区间 availability 含 unavailable/不可买的日期数）
- buybox_flips(asin, days)（offer_snapshots 按 seller_id+日期差分 BuyBox 归属变化次数）
- asin_report(asin, days) -> {asin, window_days, price_change, bsr_change, review_velocity, availability_downtime, buybox_flips}
- surging_products(min_review_velocity, window_days) -> list（扫 watchlist 全部 ASIN 的 review_velocity 排序）
全部除零/缺数据 None 语义；单测用 sqlite 内存库构造两三天快照数据。

### 3. src/service/（业务服务层——CLI 与 MCP 的唯一入口，薄逻辑）
- watchlist_service.py: WatchlistService — list_items()->list[dict]、add_item(asin, note='', variant_asin=None)->dict（ASIN 正则 ^B0[A-Z0-9]{8}$ 校验 + 读写 src/scheduler/config/watchlist.yaml）、remove_item(asin)->bool
- scrape_service.py: ScrapeService —
  scrape_pdp_now(asins:list)->list[{asin, ok, snapshot?/error?}]：逐个 PdpScraper.scrape_product → SnapshotDiffEngine.save_snapshot；BotBlocked 记 error 继续下一个
  scrape_keyword_now(keyword, page=1)->(ok, results)；scrape_offers_now(asin)->{ok, offers?/error?}
  submit_scrape(asins, channels=['pdp'])->task_id：Redis 队列提交（redis 不可用降级内存 dict），get_task_status(task_id)->{state, progress, errors}——后台线程执行，状态存 Redis hash 或内存
- metrics_service.py: MetricsService — asin_report(asin, days=30)->dict（包装 MetricsCalculator）、list_reports(sort_by='review_velocity')->list、find_surging(min_review_velocity=5, window_days=7)->list
- health_service.py: HealthService — status()->{db_ok, quota: {asin: {used, limit}}, cooldown_active, pool_healthy}（db 探测 SELECT 1；配额从 RateLimiter.get_quota_status 或 config limit 读取；BotBlocked 冷却状态查询 BaseScraper 类级状态或全局冷却登记表——如果 BaseScraper 冷却是实例级，就在 ScrapeService 里维护一个模块级冷却登记 dict 供查询）

### 4. src/scheduler/task_scheduler.py（参考 TKCrawler/src/scheduler/task_scheduler.py，可精简）
- TaskScheduler(queue=None, state_file, auto_start=False)：APScheduler BackgroundScheduler + IntervalTrigger
- load_config(path=src/scheduler/config/tasks.yaml)：解析 interval(2d/12h/30m/45s/纯数字秒)、priority、retry
- register_handler(name, fn)；start()/stop()/run_forever()/status()->dict
- submit 进简单内存优先队列 + 后台 worker 线程消费；失败按 retry.exponential(base_delay) 重新入队（最多 max_attempts）
- jobs.py（src/scheduler/jobs.py）：job_scrape_watchlist()（读 watchlist.yaml → WatchlistService.list_items → ScrapeService.scrape_pdp_now）、job_scrape_keywords()（keywords.yaml → scrape_keyword_now 逐词）、job_scrape_offers()；提供 start_with_handlers() 把三个 job 注册进调度
- tests/test_scheduler.py：interval 解析、状态持久化、job 注册与 dry-run（不真实请求，mock ScrapeService）
- tests/test_metrics_calculator.py、tests/test_service.py（watchlist 读写临时 yaml、scrape_service 全链路 mock scraper、task 提交与状态轮询）

## Agent E 交付物（mcp_server/ 目录，项目根下）

1. mcp_server/server.py：python mcp SDK（from mcp.server.fastmcp import FastMCP），stdio 传输。
   8 个工具（签名与行为严格按 CONTRACT.md 第三节，实现调 src/service/*）：
   get_asin_snapshot(asin, marketplace="US", date=None)、get_price_history(asin, days=30)、
   get_metrics_report(asin, days=30)、list_competitors(category=None, sort_by="review_velocity")、
   find_surging_products(min_review_velocity=5, window_days=7)、add_watchlist(asin, note="", variant_asin=None)、
   remove_watchlist(asin)、trigger_scrape(asins, channels=["pdp"])->{task_id}、get_task_status(task_id)
   （9 个工具，第 9 个 get_task_status 配套 trigger_scrape）
   - 只读工具直接调 service；trigger_scrape 调 ScrapeService.submit_scrape
   - ASIN 参数校验 ^B0[A-Z0-9]{8}$，非法返回 isError 内容而非抛栈
   - 所有工具返回结构化 JSON 文本（ensure_ascii=False）；异常捕获成 {"ok": false, "error": ...}
2. mcp_server/__init__.py、mcp_server/__main__.py（python -m mcp_server 启动 stdio）
3. tests/test_mcp_server.py：不启动真实 stdio——直接构造 FastMCP 的 tool 函数逐个调用断言（service 层 mock/注入 sqlite fixture）；至少覆盖：snapshot 查询、非法 ASIN 校验、add/remove watchlist（临时 yaml）、trigger→status 轮询闭环（mock scraper 立即完成）
4. mcp_server/README.md：Claude Code 接入方法（claude_desktop_config.json / .mcp.json 配置示例 + uvx/python -m 运行说明）

依赖注入方式：server.py 提供 configure_services(watchlist=None, scrape=None, metrics=None) 供测试注入；默认惰性构造真实 service。

## 测试基线要求（两位都要遵守）
- 完成后全量 /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 -m pytest tests/ -q 必须全绿（包括别人的测试；别人测试挂了要么是环境问题要说明，要么互相等）
- 无真实网络；DB 测试 sqlite 内存；Redis 测试 monkeypatch 隔离本机真实 Redis
- 不触碰其他 Agent 目录；src/common、src/database、src/scrapers、src/anti_detection、src/pools 只读
