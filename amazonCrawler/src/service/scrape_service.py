"""ScrapeService — 采集业务入口（同步三件套 + 异步任务提交/状态轮询）。

同步：
    scrape_pdp_now / scrape_keyword_now / scrape_offers_now
    每次成功写库走 SnapshotDiffEngine.save_snapshot；
    BotBlockedException 记 error 后继续下一个（不中断整批）。

异步：
    submit_scrape(asins, channels) -> task_id
    后台线程消费；任务状态存 Redis hash，Redis 不可用降级进程内 dict。
    get_task_status(task_id) -> {state, progress, errors}。

冷却登记：
    BaseScraper 的软冷却是实例级，本模块维护全局冷却登记 dict
    （register_cooldown / get_cooldowns）供 HealthService 查询。
"""

import threading
import time
import uuid
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

try:
    from src.common.config import config
    from src.common.exceptions import BotBlockedException
    from src.common.logger import get_logger
    logger = get_logger(__name__)
except ImportError:  # pragma: no cover
    logger = None
    BotBlockedException = Exception

try:
    from src.database.snapshot_engine import SnapshotDiffEngine
except ImportError:  # pragma: no cover
    SnapshotDiffEngine = None

try:
    from src.scrapers import OfferScraper, PdpScraper, SearchScraper
except ImportError:  # pragma: no cover
    PdpScraper = SearchScraper = OfferScraper = None

if logger is None:
    import logging
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 模块级冷却登记表：BaseScraper 实例级冷却不可集中查询，这里登记一份全局状态
# ---------------------------------------------------------------------------
_COOLDOWN_REGISTRY: Dict[str, float] = {}   # scraper_name -> cooldown 截止 epoch
_COOLDOWN_LOCK = threading.Lock()


def register_cooldown(scraper_name: str, until_epoch: float) -> None:
    """登记某爬虫进入软冷却的截止时间（scrape_service 内部调用 / 测试注入）。"""
    with _COOLDOWN_LOCK:
        _COOLDOWN_REGISTRY[scraper_name] = float(until_epoch)


def get_cooldowns() -> Dict[str, float]:
    """返回仍处于冷却期的 {scraper_name: remaining_seconds}。"""
    now = time.time()
    with _COOLDOWN_LOCK:
        return {
            name: max(0.0, until - now)
            for name, until in _COOLDOWN_REGISTRY.items() if until > now
        }


class ScrapeService:
    """采集编排：PDP / 关键词 / Offer 同步采集 + 异步任务队列。"""

    def __init__(self, engine: Optional["SnapshotDiffEngine"] = None,
                 pdp_scraper=None, search_scraper=None, offer_scraper=None,
                 redis_client=None):
        """
        Args:
            engine: SnapshotDiffEngine（测试注入 SQLite 版；None 走全局默认）。
            pdp/search/offer_scraper: 注入 mock 供测试；None 惰性构造真实实例。
            redis_client: 任务状态存储的 Redis 客户端；None 时按需惰性连接，
                          连接失败自动降级进程内 dict。
        """
        if SnapshotDiffEngine is not None:
            self._engine = engine or SnapshotDiffEngine()
        else:  # pragma: no cover
            self._engine = None
        # 后台线程写库：SQLite 内存库默认每连接独立 DB，
        # 换 StaticPool 共享单连接，保证 worker 线程与测试线程看到同一份数据。
        self._ensure_shared_memory_pool()
        self._pdp = pdp_scraper
        self._search = search_scraper
        self._offer = offer_scraper
        self._redis = redis_client
        self._redis_checked = redis_client is not None

        # 降级内存任务存储：task_id -> {state, progress, total, errors, ...}
        self._tasks: Dict[str, dict] = {}
        self._tasks_lock = threading.Lock()
        self._workers: Dict[str, threading.Thread] = {}

    def _ensure_shared_memory_pool(self):
        """SQLite :memory: 跨线程共享：重建 engine 为 StaticPool 单连接。

        SQLite 内存库默认每个连接是独立数据库，后台 worker 线程会看不到
        主线程 create_all 建的表。重建 engine（StaticPool 共享同一连接）后
        重绑 session_factory，并在新连接上补建全部表。
        """
        try:
            factory = getattr(self._engine, "_session_factory", None)
            bind = getattr(factory, "kw", {}).get("bind")
            if bind is None or bind.url.get_backend_name() != "sqlite":
                return
            if str(bind.url) not in ("sqlite://", "sqlite:///:memory:"):
                return
            from sqlalchemy import create_engine
            from sqlalchemy.pool import StaticPool

            shared_engine = create_engine(
                "sqlite://",
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
            from src.database.models import Base
            Base.metadata.create_all(shared_engine)
            factory.configure(bind=shared_engine)
        except Exception:  # pragma: no cover - 非 SQLite/非内存场景静默跳过
            pass

    # ------------------------------------------------------------------
    # scraper 惰性构造
    # ------------------------------------------------------------------
    @property
    def pdp_scraper(self):
        if self._pdp is None:
            self._pdp = PdpScraper()
        return self._pdp

    @property
    def search_scraper(self):
        if self._search is None:
            self._search = SearchScraper()
        return self._search

    @property
    def offer_scraper(self):
        if self._offer is None:
            self._offer = OfferScraper()
        return self._offer

    # ------------------------------------------------------------------
    # 同步采集
    # ------------------------------------------------------------------
    def scrape_pdp_now(self, asins: List[str]) -> List[dict]:
        """逐个采集 PDP 并落库；失败（含 BotBlocked）记 error 继续下一个。

        Returns:
            [{asin, ok: True, snapshot: {...}} | {asin, ok: False, error: str}]
        """
        results = []
        for asin in asins:
            try:
                data = self.pdp_scraper.scrape_product(asin)
                if not data:
                    results.append(
                        {"asin": asin, "ok": False, "error": "empty parse result"})
                    continue
                saved = bool(self._engine) and self._engine.save_snapshot(
                    data, "asin_snapshots")
                results.append({"asin": asin, "ok": True, "snapshot": data,
                                "saved": saved})
            except BotBlockedException as exc:
                self._register_scraper_cooldown("pdp")
                results.append({"asin": asin, "ok": False,
                                "error": f"bot_blocked: {exc}"})
            except Exception as exc:
                results.append({"asin": asin, "ok": False,
                                "error": f"{type(exc).__name__}: {exc}"})
        return results

    def scrape_keyword_now(self, keyword: str, page: int = 1) -> Tuple[bool, list]:
        """采集关键词排名并落库；返回 (ok, results)。"""
        try:
            items = self.search_scraper.search(keyword, page=page)
            if not isinstance(items, list):
                return False, []
            saved = 0
            for item in items:
                if self._engine and self._engine.save_snapshot(
                        item, "keyword_rank_snapshots"):
                    saved += 1
            return True, items
        except BotBlockedException as exc:
            self._register_scraper_cooldown("search")
            return False, [f"bot_blocked: {exc}"]
        except Exception as exc:
            return False, [f"{type(exc).__name__}: {exc}"]

    def scrape_offers_now(self, asin: str) -> dict:
        """采集 offers 并落库；返回 {ok, offers} 或 {ok, error}。"""
        try:
            offers = self.offer_scraper.scrape_offers(asin)
            if not isinstance(offers, list):
                return {"ok": False, "error": "empty parse result"}
            for offer in offers:
                if self._engine:
                    self._engine.save_snapshot(offer, "offer_snapshots")
            return {"ok": True, "offers": offers}
        except BotBlockedException as exc:
            self._register_scraper_cooldown("offer")
            return {"ok": False, "error": f"bot_blocked: {exc}"}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def _register_scraper_cooldown(self, name: str) -> None:
        """BotBlocked 后按真实 scraper 的冷却窗口登记（可查健康状态）。"""
        try:
            scraper = {"pdp": self.pdp_scraper,
                       "search": self.search_scraper,
                       "offer": self.offer_scraper}[name]
            remaining = scraper.get_cooldown_remaining()
            if remaining > 0:
                register_cooldown(name, time.time() + remaining)
        except Exception:  # pragma: no cover
            pass

    # ------------------------------------------------------------------
    # 异步任务：提交 / 状态
    # ------------------------------------------------------------------
    def submit_scrape(self, asins: List[str],
                      channels: Optional[List[str]] = None) -> str:
        """提交异步采集任务，返回 task_id；后台线程立即开始执行。"""
        channels = channels or ["pdp"]
        task_id = uuid.uuid4().hex[:16]
        record = {
            "task_id": task_id,
            "state": "queued",
            "progress": 0,
            "total": len(asins),
            "channels": list(channels),
            "asins": list(asins),
            "errors": [],
            "submitted_at": datetime.now().isoformat(),
            "finished_at": None,
        }
        if not self._store_set(task_id, record):
            # Redis 不可用已在 _store_set 降级写内存
            pass
        worker = threading.Thread(
            target=self._run_task, args=(task_id, asins, channels),
            name=f"scrape-task-{task_id}", daemon=True)
        with self._tasks_lock:
            self._workers[task_id] = worker
        worker.start()
        return task_id

    def get_task_status(self, task_id: str) -> Optional[dict]:
        """轮询任务状态：{state, progress, total, errors, channels...}；未知 id → None。"""
        return self._store_get(task_id)

    # ------------------------------------------------------------------
    # 后台执行
    # ------------------------------------------------------------------
    def _run_task(self, task_id: str, asins: List[str],
                  channels: List[str]) -> None:
        def _update(**fields):
            record = self._store_get(task_id) or {}
            record.update(fields)
            self._store_set(task_id, record)

        _update(state="running")
        errors: List[str] = []
        done = 0
        total = max(1, len(asins))
        try:
            for asin in asins:
                for channel in channels:
                    if channel == "pdp":
                        result = self.scrape_pdp_now([asin])[0]
                        if not result.get("ok"):
                            errors.append(
                                f"{asin}/pdp: {result.get('error')}")
                    elif channel == "offers":
                        result = self.scrape_offers_now(asin)
                        if not result.get("ok"):
                            errors.append(
                                f"{asin}/offers: {result.get('error')}")
                    elif channel == "keyword":
                        # keyword 通道按 asin 语义不成立，记录跳过
                        errors.append(f"{asin}/keyword: 通道不适用于 asin 维度")
                    else:
                        errors.append(f"{asin}/{channel}: 未知通道")
                done += 1
                _update(progress=done, errors=list(errors))
            state = "failed" if errors and done == 0 else (
                "completed_with_errors" if errors else "completed")
        except Exception as exc:  # pragma: no cover - 兜底，保证状态终态
            errors.append(f"worker crash: {type(exc).__name__}: {exc}")
            state = "failed"
        _update(state=state, errors=errors, progress=done,
                finished_at=datetime.now().isoformat())

    # ------------------------------------------------------------------
    # 状态存储：Redis hash 优先，降级内存 dict
    # ------------------------------------------------------------------
    def _get_redis(self):
        """惰性取 Redis：显式注入优先，否则按 config 连一次并缓存结果。"""
        if self._redis_checked:
            return self._redis
        self._redis_checked = True
        if self._redis is None:
            try:
                import redis as redis_lib
                self._redis = redis_lib.Redis(
                    host=config.REDIS_HOST, port=config.REDIS_PORT,
                    password=config.REDIS_PASSWORD, db=config.REDIS_DB,
                    socket_connect_timeout=1, socket_timeout=1)
                self._redis.ping()
            except Exception as exc:
                logger.warning("Redis 不可用，任务状态降级内存存储: {}", exc)
                self._redis = None
        return self._redis

    _TASK_KEY_PREFIX = "amz:scrape_task:"

    def _store_set(self, task_id: str, record: dict) -> bool:
        """写任务记录；Redis 成功返回 True，降级内存返回 False。"""
        r = self._get_redis()
        if r is not None:
            try:
                import json as _json
                r.set(self._TASK_KEY_PREFIX + task_id,
                      _json.dumps(record, ensure_ascii=False), ex=86400)
                return True
            except Exception as exc:
                logger.warning("Redis 写任务状态失败，降级内存: {}", exc)
        with self._tasks_lock:
            self._tasks[task_id] = record
        return False

    def _store_get(self, task_id: str) -> Optional[dict]:
        r = self._get_redis()
        if r is not None:
            try:
                import json as _json
                raw = r.get(self._TASK_KEY_PREFIX + task_id)
                if raw is not None:
                    return _json.loads(raw)
            except Exception as exc:
                logger.warning("Redis 读任务状态失败，读内存: {}", exc)
        with self._tasks_lock:
            return self._tasks.get(task_id)
