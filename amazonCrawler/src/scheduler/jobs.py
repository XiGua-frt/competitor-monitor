"""jobs.py — 三个调度任务实现 + start_with_handlers 注册入口。

    job_scrape_watchlist  读 watchlist.yaml → ScrapeService.scrape_pdp_now
    job_scrape_keywords   读 keywords.yaml  → scrape_keyword_now 逐词
    job_scrape_offers     读 watchlist.yaml → scrape_offers_now 逐 ASIN

全部走 WatchlistService/ScrapeService（service 层），不直接触碰 scrapers；
测试可 monkeypatch 这两个 service 或注入 mock。
"""

from pathlib import Path
from typing import List, Optional

import yaml

from src.common.logger import get_logger
from src.scheduler.task_scheduler import TaskScheduler
from src.service.scrape_service import ScrapeService
from src.service.watchlist_service import WatchlistService

logger = get_logger("scheduler.jobs")

CONFIG_DIR = Path(__file__).resolve().parent / "config"
DEFAULT_WATCHLIST_YAML = CONFIG_DIR / "watchlist.yaml"
DEFAULT_KEYWORDS_YAML = CONFIG_DIR / "keywords.yaml"

# 模块级 service 单例（惰性；测试可替换）
_services: Optional[dict] = None


def _load_yaml_list(path: Path, key: str) -> List[str]:
    """读 yaml 顶层 key 的列表项；缺文件/空 → []。"""
    if not path.exists():
        logger.warning("配置文件不存在: {}", path)
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        values = data.get(key) or []
        return [v for v in values if v]
    except (yaml.YAMLError, OSError) as exc:
        logger.error("配置文件解析失败 {}: {}", path, exc)
        return []


def get_services(watchlist_path: Optional[Path] = None) -> dict:
    """惰性构造共享 service（watchlist/scrape），测试可 monkeypatch 替换。"""
    global _services
    if _services is None:
        _services = {
            "watchlist": WatchlistService(path=watchlist_path),
            "scrape": ScrapeService(),
        }
    return _services


def set_services(watchlist=None, scrape=None) -> None:
    """测试注入点：替换模块级 service。"""
    global _services
    if _services is None:
        _services = {}
    if watchlist is not None:
        _services["watchlist"] = watchlist
    if scrape is not None:
        _services["scrape"] = scrape


def reset_services() -> None:
    """清空模块级 service（测试隔离用）。"""
    global _services
    _services = None


# ---------------------------------------------------------------------------
# 三个 job（签名 func(**payload)，与 TaskScheduler.register_handler 对齐）
# ---------------------------------------------------------------------------

def job_scrape_watchlist(watchlist_path=None, **_ignored) -> dict:
    """watchlist 全部 ASIN 的 PDP 快照。"""
    services = get_services(watchlist_path=watchlist_path)
    items = services["watchlist"].list_items()
    asins = [it["asin"] for it in items]
    if not asins:
        logger.info("job_scrape_watchlist: 清单为空，跳过")
        return {"scraped": 0, "failed": 0}
    results = services["scrape"].scrape_pdp_now(asins)
    failed = [r for r in results if not r.get("ok")]
    logger.info("job_scrape_watchlist: {} 成功 / {} 失败",
                len(results) - len(failed), len(failed))
    return {"scraped": len(results) - len(failed), "failed": len(failed)}


def job_scrape_keywords(keywords_path=None, **_ignored) -> dict:
    """keywords.yaml 全部关键词的排名快照（逐词，失败继续）。"""
    services = get_services()
    keywords = _load_yaml_list(
        Path(keywords_path) if keywords_path else DEFAULT_KEYWORDS_YAML,
        "keywords")
    if not keywords:
        logger.info("job_scrape_keywords: 关键词清单为空，跳过")
        return {"scraped": 0, "failed": 0}
    ok_count = 0
    failed_keywords: List[str] = []
    for keyword in keywords:
        ok, _results = services["scrape"].scrape_keyword_now(keyword)
        if ok:
            ok_count += 1
        else:
            failed_keywords.append(keyword)
            logger.warning("关键词采集失败 {}: {}", keyword, _results)
    logger.info("job_scrape_keywords: {} 成功 / {} 失败",
                ok_count, len(failed_keywords))
    return {"scraped": ok_count, "failed": len(failed_keywords)}


def job_scrape_offers(watchlist_path=None, **_ignored) -> dict:
    """watchlist 全部 ASIN 的 offer/BuyBox 快照。"""
    services = get_services(watchlist_path=watchlist_path)
    items = services["watchlist"].list_items()
    asins = [it["asin"] for it in items]
    ok_count = 0
    failed: List[str] = []
    for asin in asins:
        result = services["scrape"].scrape_offers_now(asin)
        if result.get("ok"):
            ok_count += 1
        else:
            failed.append(asin)
            logger.warning("offer 采集失败 {}: {}", asin, result.get("error"))
    logger.info("job_scrape_offers: {} 成功 / {} 失败", ok_count, len(failed))
    return {"scraped": ok_count, "failed": len(failed)}


# ---------------------------------------------------------------------------
# 注册入口
# ---------------------------------------------------------------------------

def start_with_handlers(scheduler: Optional[TaskScheduler] = None,
                        config_path=None,
                        state_file: str = "data/scheduler_state.json",
                        auto_start: bool = False) -> TaskScheduler:
    """构造（或复用）TaskScheduler，加载 tasks.yaml 并注册三个 job handler。

    附带：AMZ_HEALTH_CHECK_ENABLED=true 时启动 IP 池后台探活线程
    （daemon，不阻塞调度；探活失败代理自动进入冷却）。
    """
    if scheduler is None:
        scheduler = TaskScheduler(state_file=state_file, auto_start=False)
    scheduler.load_config(
        Path(config_path) if config_path
        else CONFIG_DIR / "tasks.yaml")
    scheduler.register_handler("scrape_watchlist", job_scrape_watchlist)
    scheduler.register_handler("scrape_keyword_ranks", job_scrape_keywords)
    scheduler.register_handler("scrape_offers", job_scrape_offers)
    if auto_start:
        scheduler.start()
    _maybe_start_health_checker()
    return scheduler


_health_checker = None


def _maybe_start_health_checker():
    """按 AMZ_HEALTH_CHECK_ENABLED 启动代理探活 daemon 线程（只启动一次）。"""
    global _health_checker
    if _health_checker is not None:
        return
    try:
        from src.common.config import config
        if not config.HEALTH_CHECK_ENABLED:
            return
        from src.pools import get_ip_pool
        from src.pools.health_checker import ProxyHealthChecker

        interval = getattr(config, "HEALTH_CHECK_INTERVAL", 600)
        _health_checker = ProxyHealthChecker(
            get_ip_pool(), interval=interval)
        _health_checker.start()
        logger.info("IP 池健康检查已启动 (interval={}s)", interval)
    except Exception:
        logger.exception("健康检查启动失败（不影响调度主流程）")
