#!/usr/bin/env python
"""TKCrawler 命令行入口 (Agent L)。

子命令:
    scrape shop    --category-url <url> [--limit N]   商品类目采集 → product_snapshots
    scrape video   --product-id <id> [--limit N]       商品关联视频采集 → video_snapshots
    scrape creator --creator-id <id>                   达人主页采集 → creator_snapshots
    scrape hashtag --tag <name>                        话题采集 → hashtag_snapshots
    scrape itemlist --sec-uid <secUid> [--handle <unique_id>]
                     [--count N] [--pages M]           达人视频列表采集 → video_snapshots
    scheduler      --start | --status | --pause <task_id>
                                                       加载 src/scheduler/config/tasks.yaml,
                                                       注册 handler, 阻塞运行 + process_next 消费
    init-db                                            执行 scripts/init_db.py 的建表逻辑
    report         --product-id <id>                   MetricsCalculator.calculate_all 4 指标 (JSON)

全局选项:
    --dry-run   只打印将要执行的动作, 不发起任何真实请求、不写库
                (便于无登录态冒烟测试)

退出码:
    0  成功
    1  采集失败 (scraper 返回空/None、解析失败等业务失败)
    2  配置/依赖错误 (数据库不可用、配置缺失、参数错误等)
       数据库不可用时会提示: docker-compose up -d postgres

用法示例:
    python main.py scrape shop --category-url "https://www.tiktok.com/shop/category/x" --limit 20
    python main.py --dry-run scrape hashtag --tag summer
    python main.py scheduler --start
    python main.py init-db
    python main.py report --product-id P123
"""

import argparse
import json
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.anti_detection import AntiDetectionClient  # noqa: E402
from src.common.exceptions import (  # noqa: E402
    CrawlerException,
    RateLimitException,
    SessionExpiredException,
    WAFBlockedException,
)
from src.common.logger import get_logger  # noqa: E402
from src.database import SnapshotDiffEngine  # noqa: E402
from src.metrics import MetricsCalculator  # noqa: E402
from src.pools.integration import (  # noqa: E402
    PoolUnavailableError,
    inject_cookie_to_client,
    inject_proxy_to_client,
    pool_scoped_client,
)
from src.scrapers.creator_scraper import CreatorProfileScraper  # noqa: E402
from src.scrapers.hashtag_scraper import HashtagTrendScraper  # noqa: E402
from src.scrapers.item_list_scraper import ItemListScraper  # noqa: E402
from src.scrapers.shop_scraper import ShopProductScraper  # noqa: E402
from src.scrapers.video_scraper import VideoStreamScraper  # noqa: E402
from src.scheduler import TaskScheduler  # noqa: E402

logger = get_logger("cli")

# 退出码约定 (见模块 docstring)
EXIT_OK = 0
EXIT_SCRAPE_FAILED = 1
EXIT_CONFIG_ERROR = 2

TASKS_YAML = PROJECT_ROOT / "src" / "scheduler" / "config" / "tasks.yaml"
STATE_FILE = PROJECT_ROOT / "data" / "scheduler_state.json"

DB_HINT = (
    "数据库不可用: 请先启动 PostgreSQL (docker-compose up -d postgres), "
    "并核对 .env 中 POSTGRES_HOST/PORT/DB/USER/PASSWORD 配置;"
    "首次使用先执行 python main.py init-db"
)


class ConfigError(Exception):
    """配置/依赖错误 → 退出码 2。"""


# ---------------------------------------------------------------------------
# 公共工具
# ---------------------------------------------------------------------------
def check_db() -> None:
    """探测数据库连通性, 不可用时抛 ConfigError (由 main 统一转退出码 2)。"""
    from sqlalchemy import text

    from src.database import engine as db_engine

    try:
        with db_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        raise ConfigError(f"{DB_HINT}\n原始错误: {exc}") from exc


def _make_client() -> AntiDetectionClient:
    """构造反检测客户端 (采集路径专用; --dry-run 下不会被调用)。"""
    return AntiDetectionClient()


def _client_for_task(lease_ctx) -> AntiDetectionClient:
    """按池边界结果构造客户端: 非影子 lease 注入账号 cookie, 否则原路径。

    影子模式/池关闭时 lease_ctx.active_lease 为 None, 行为与原 _make_client() 完全一致。
    cookie 注入后, 若 POOL_IP_ENABLED=true 再按账号绑定关系注入 IP 池代理
    (无可用代理时保留 config.PROXY_URL 原配置)。
    """
    client = _make_client()
    lease = lease_ctx.active_lease
    if lease is not None:
        inject_cookie_to_client(client, lease)
        inject_proxy_to_client(client, lease)
    return client


def _save_or_fail(engine: SnapshotDiffEngine, data: dict, table: str) -> bool:
    """写入一条快照; 失败视为数据库依赖错误, 抛 ConfigError。"""
    if not engine.save_snapshot(data, table):
        raise ConfigError(f"快照写入失败 (table={table})。{DB_HINT}")
    return True


def _print_dry_run(action: str, **params) -> None:
    print(f"[dry-run] {action}")
    for key, value in params.items():
        if value is not None:
            print(f"    {key}: {value}")


# ---------------------------------------------------------------------------
# scrape 子命令 (dry_run=True 时只打印, 不构造 client、不发请求、不连库)
# ---------------------------------------------------------------------------
def run_shop_scrape(category_url: str, limit: Optional[int] = None,
                    dry_run: bool = False) -> int:
    """商品类目采集: scrape_category → 逐个 scrape_product → save_snapshot。"""
    if dry_run:
        _print_dry_run("scrape shop", category_url=category_url, limit=limit,
                       save_to="product_snapshots")
        return EXIT_OK

    check_db()
    engine = SnapshotDiffEngine()

    with pool_scoped_client('shop', dry_run=dry_run) as lease_ctx:
        scraper = ShopProductScraper(client=_client_for_task(lease_ctx))

        product_ids = scraper.scrape_category(category_url)
        if not product_ids:
            print("scrape shop: 类目页未解析到任何商品 ID (可能被风控或页面结构变化)")
            return EXIT_SCRAPE_FAILED
        if limit is not None:
            product_ids = product_ids[:limit]

        saved = 0
        for pid in product_ids:
            data = scraper.scrape_product(pid)
            if not data:
                print(f"scrape shop: 商品 {pid} 采集失败, 跳过")
                continue
            _save_or_fail(engine, data, "product_snapshots")
            saved += 1
            print(f"scrape shop: 商品 {pid} 快照已写入 ({saved}/{len(product_ids)})")
        lease_ctx.success = bool(saved)
        return EXIT_OK if saved else EXIT_SCRAPE_FAILED


def run_video_scrape(product_id: str, limit: Optional[int] = None,
                     dry_run: bool = False) -> int:
    """商品关联视频采集: scrape_product_videos → 逐条 save_snapshot。"""
    if dry_run:
        _print_dry_run("scrape video", product_id=product_id, limit=limit,
                       save_to="video_snapshots")
        return EXIT_OK

    check_db()
    engine = SnapshotDiffEngine()

    with pool_scoped_client('video', dry_run=dry_run) as lease_ctx:
        scraper = VideoStreamScraper(client=_client_for_task(lease_ctx))

        videos = scraper.scrape_product_videos(product_id, limit=limit or 10)
        if not videos:
            print(f"scrape video: 商品 {product_id} 未采集到视频")
            return EXIT_SCRAPE_FAILED
        for data in videos:
            _save_or_fail(engine, data, "video_snapshots")
        lease_ctx.success = True
        print(f"scrape video: 商品 {product_id} 共写入 {len(videos)} 条视频快照")
        return EXIT_OK


def run_creator_scrape(creator_id: str, dry_run: bool = False) -> int:
    """达人主页采集: scrape_profile → save_snapshot。"""
    if dry_run:
        _print_dry_run("scrape creator", creator_id=creator_id,
                       save_to="creator_snapshots")
        return EXIT_OK

    check_db()
    engine = SnapshotDiffEngine()

    with pool_scoped_client('creator', dry_run=dry_run) as lease_ctx:
        scraper = CreatorProfileScraper(client=_client_for_task(lease_ctx))

        data = scraper.scrape_profile(creator_id)
        if not data:
            print(f"scrape creator: 达人 {creator_id} 采集失败")
            return EXIT_SCRAPE_FAILED
        _save_or_fail(engine, data, "creator_snapshots")
        lease_ctx.success = True
        print(f"scrape creator: 达人 {creator_id} 快照已写入")
        return EXIT_OK


def run_hashtag_scrape(tag: str, dry_run: bool = False) -> int:
    """话题采集: scrape_hashtag → save_snapshot。"""
    if dry_run:
        _print_dry_run("scrape hashtag", tag=tag, save_to="hashtag_snapshots")
        return EXIT_OK

    check_db()
    engine = SnapshotDiffEngine()

    with pool_scoped_client('hashtag', dry_run=dry_run) as lease_ctx:
        scraper = HashtagTrendScraper(client=_client_for_task(lease_ctx))

        data = scraper.scrape_hashtag(tag)
        if not data:
            print(f"scrape hashtag: 话题 #{tag} 采集失败")
            return EXIT_SCRAPE_FAILED
        _save_or_fail(engine, data, "hashtag_snapshots")
        lease_ctx.success = True
        print(f"scrape hashtag: 话题 #{tag} 快照已写入")
        return EXIT_OK


def run_itemlist_scrape(sec_uid: str, handle: Optional[str] = None,
                        count: int = 35, pages: int = 3,
                        dry_run: bool = False) -> int:
    """达人视频列表采集: scrape_user_videos → 逐行 save_snapshot。

    入库映射 (E2E 已验证, scripts/e2e_itemlist_20261008.py step3):
    video_snapshots 表写入 video_id/snapshot_date/author_id/
    publish_time(=create_time UTC naive)/views/likes/comments/shares;
    product_id/duration/is_paid 留 NULL; desc/collect_count 无列不入库。
    """
    if dry_run:
        _print_dry_run("scrape itemlist", sec_uid=sec_uid, handle=handle,
                       count=count, pages=pages,
                       save_to="video_snapshots")
        return EXIT_OK

    check_db()
    engine = SnapshotDiffEngine()

    with pool_scoped_client('itemlist', dry_run=dry_run) as lease_ctx:
        scraper = ItemListScraper(client=_client_for_task(lease_ctx))

        videos = scraper.scrape_user_videos(sec_uid, unique_id=handle,
                                            count=count, max_pages=pages)
        if not videos:
            print(f"scrape itemlist: 达人 {handle or sec_uid} 未采集到视频")
            return EXIT_SCRAPE_FAILED
        snapshot_date = date.today()
        saved = 0
        for row in videos:
            data = {
                'video_id': str(row['video_id']),
                'snapshot_date': snapshot_date,
                'author_id': row['author_id'],
                'publish_time': datetime.fromtimestamp(
                    row['create_time'], tz=timezone.utc).replace(tzinfo=None),
                'views': row['play_count'],
                'likes': row['digg_count'],
                'comments': row['comment_count'],
                'shares': row['share_count'],
            }
            _save_or_fail(engine, data, "video_snapshots")
            saved += 1
        lease_ctx.success = bool(saved)
        print(f"scrape itemlist: 共写入 {saved} 条视频快照 (含 upsert)")
        return EXIT_OK if saved else EXIT_SCRAPE_FAILED


# ---------------------------------------------------------------------------
# scheduler 子命令
# ---------------------------------------------------------------------------
def _task_handler_shop(**payload) -> None:
    """调度 handler: scrape_shop_products。payload 可带 category_url/limit。"""
    category_url = payload.get("category_url")
    if not category_url:
        raise ValueError(
            "scrape_shop_products 缺少 category_url: 请在 tasks.yaml 任务 payload "
            "或环境变量 TK_SHOP_CATEGORY_URL 中配置类目页地址"
        )
    code = run_shop_scrape(category_url, limit=payload.get("limit"))
    if code != EXIT_OK:
        raise RuntimeError(f"shop 采集失败 (exit={code})")


def _task_handler_video(**payload) -> None:
    """调度 handler: scrape_videos。payload 可带 product_id。"""
    product_id = payload.get("product_id")
    if not product_id:
        raise ValueError("scrape_videos 缺少 product_id: 请在任务 payload 中配置")
    code = run_video_scrape(product_id, limit=payload.get("limit"))
    if code != EXIT_OK:
        raise RuntimeError(f"video 采集失败 (exit={code})")


def _task_handler_hashtag(**payload) -> None:
    """调度 handler: scrape_hashtag_snapshots (import 复用 job 脚本入口)。

    payload 可带 tags (逗号分隔字符串或列表); 未配置时用 job 默认标签。
    """
    from scripts.hashtag_snapshot_job import run_job  # 延迟 import 避免循环依赖

    tags = payload.get("tags")
    if isinstance(tags, str):
        from scripts.hashtag_snapshot_job import parse_tags
        tags = parse_tags(tags)
    code = run_job(tags=tags)
    if code != EXIT_OK:
        raise RuntimeError(f"hashtag 快照 job 失败 (exit={code})")


def _task_handler_itemlist(**payload) -> None:
    """调度 handler: scrape_itemlist_snapshots。payload 需含 sec_uid。

    payload 可带 handle/count/pages (可选, 缺省 35 条/1 页)。
    """
    sec_uid = payload.get("sec_uid")
    if not sec_uid:
        raise ValueError(
            "scrape_itemlist_snapshots 缺少 sec_uid: 请在 tasks.yaml 任务 "
            "payload 中配置目标达人的 secUid"
        )
    code = run_itemlist_scrape(
        sec_uid,
        handle=payload.get("handle"),
        count=payload.get("count", 35),
        pages=payload.get("pages", 1),
    )
    if code != EXIT_OK:
        raise RuntimeError(f"itemlist 采集失败 (exit={code})")


def _task_handler_watchlist(**payload) -> None:
    """调度 handler: scrape_watchlist（种子商品清单每日快照）。

    清单文件 src/scheduler/config/watchlist.yaml；payload 可带 watchlist 路径。
    批次单租约语义见 scripts/watchlist_daily_job.py 模块头。
    """
    from scripts.watchlist_daily_job import run_job  # 延迟 import 避免循环依赖

    code = run_job(
        watchlist_path=payload.get("watchlist") or None,
    )
    if code != EXIT_OK:
        raise RuntimeError(f"watchlist 批次失败 (exit={code})")


def cmd_scheduler_start() -> int:
    """加载 tasks.yaml → 注册 handler → 阻塞运行 + process_next 消费循环。"""
    check_db()
    scheduler = TaskScheduler(config_path=str(TASKS_YAML), auto_start=True)
    scheduler.register_handler("scrape_shop_products", _task_handler_shop)
    scheduler.register_handler("scrape_videos", _task_handler_video)
    scheduler.register_handler("scrape_hashtag_snapshots", _task_handler_hashtag)
    scheduler.register_handler("scrape_itemlist_snapshots", _task_handler_itemlist)
    scheduler.register_handler("scrape_watchlist", _task_handler_watchlist)
    task_ids = ", ".join(scheduler.load_config(str(TASKS_YAML)).keys())
    print(f"调度器已启动, 周期任务: {task_ids}; Ctrl+C 退出")

    try:
        while True:
            scheduler.process_next()
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n收到退出信号, 关闭调度器...")
        scheduler.shutdown(wait=False)
    return EXIT_OK


def cmd_scheduler_status() -> int:
    """展示周期任务与状态文件 (不启动后台线程)。"""
    scheduler = TaskScheduler(auto_start=False)
    scheduler.load_config(str(TASKS_YAML))
    print("已注册周期任务:")
    for job in scheduler.jobs:
        print(f"    {job.name} (id={job.id})")
    if not scheduler.jobs:
        print("    (无)")

    if STATE_FILE.exists():
        try:
            states = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            states = {}
        print(f"任务状态 ({STATE_FILE}):")
        for task_id, state in states.items():
            status = state.get("status", "?") if isinstance(state, dict) else state
            print(f"    {task_id}: {status}")
    else:
        print(f"任务状态: 尚无状态文件 ({STATE_FILE})")
    return EXIT_OK


def cmd_scheduler_pause(task_id: str) -> int:
    """暂停指定周期任务 (写状态; 调度器未运行时仅记录)。"""
    scheduler = TaskScheduler(auto_start=False)
    scheduler.load_config(str(TASKS_YAML))
    scheduler.pause_task(task_id)
    print(f"任务 {task_id} 已暂停 (状态写入 {STATE_FILE})")
    return EXIT_OK


# ---------------------------------------------------------------------------
# init-db / report 子命令
# ---------------------------------------------------------------------------
def cmd_init_db() -> int:
    """执行 scripts/init_db.py 的建表逻辑 (import 复用, 保持其错误处理语义)。"""
    try:
        from scripts.init_db import run_migrations, verify_tables
        from src.database.connection import create_db_engine
    except ImportError as exc:
        raise ConfigError(f"init-db 依赖加载失败: {exc}") from exc

    engine = create_db_engine()
    try:
        run_migrations()
        ok = verify_tables(engine)
        return EXIT_OK if ok else EXIT_SCRAPE_FAILED
    except Exception as exc:
        logger.exception("init-db 失败")
        print(f"init-db 失败: {exc}")
        print(f"提示: {DB_HINT}")
        return EXIT_CONFIG_ERROR
    finally:
        engine.dispose()


def cmd_report(product_id: str) -> int:
    """计算并 JSON 打印 4 个派生指标。"""
    check_db()
    calculator = MetricsCalculator()
    try:
        metrics = calculator.calculate_all(product_id)
    except Exception as exc:
        raise ConfigError(f"指标计算失败: {exc}\n{DB_HINT}") from exc
    print(json.dumps(
        {"product_id": product_id, **metrics},
        ensure_ascii=False, indent=2, default=str,
    ))
    return EXIT_OK


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="TikTok 跨境选品数据采集系统 CLI",
        epilog="全局 --dry-run 只打印动作不发起请求; 退出码: 0 成功 / 1 采集失败 / 2 配置或依赖错误",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只打印将要执行的动作, 不真正请求/写库",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # scrape
    p_scrape = sub.add_parser("scrape", help="数据采集 (shop/video/creator/hashtag/itemlist)")
    scrape_sub = p_scrape.add_subparsers(dest="scrape_target", required=True)

    p_shop = scrape_sub.add_parser("shop", help="商品类目采集")
    p_shop.add_argument("--category-url", required=True, help="TikTok Shop 类目页 URL")
    p_shop.add_argument("--limit", type=int, default=None, help="最多采集 N 个商品")

    p_video = scrape_sub.add_parser("video", help="商品关联视频采集")
    p_video.add_argument("--product-id", required=True, help="商品 ID")
    p_video.add_argument("--limit", type=int, default=None, help="最多采集 N 条视频 (默认 10)")

    p_creator = scrape_sub.add_parser("creator", help="达人主页采集")
    p_creator.add_argument("--creator-id", required=True, help="达人 ID")

    p_hashtag = scrape_sub.add_parser("hashtag", help="话题采集")
    p_hashtag.add_argument("--tag", required=True, help="话题名 (不含 #)")

    p_itemlist = scrape_sub.add_parser("itemlist", help="达人视频列表采集")
    p_itemlist.add_argument("--sec-uid", required=True, help="达人 secUid")
    p_itemlist.add_argument("--handle", default=None,
                            help="达人 unique_id (可选, 用于 Referer)")
    p_itemlist.add_argument("--count", type=int, default=35,
                            help="每页条数 (默认 35)")
    p_itemlist.add_argument("--pages", type=int, default=3,
                            help="最多翻 N 页 (默认 3)")

    # scheduler
    p_sched = sub.add_parser("scheduler", help="周期调度器")
    group = p_sched.add_mutually_exclusive_group(required=True)
    group.add_argument("--start", action="store_true", help="阻塞启动调度器")
    group.add_argument("--status", action="store_true", help="查看任务与状态")
    group.add_argument("--pause", metavar="TASK_ID", help="暂停指定任务")

    # init-db
    sub.add_parser("init-db", help="初始化数据库 (执行 Alembic 迁移建表)")

    # report
    p_report = sub.add_parser("report", help="商品 4 指标报告 (JSON)")
    p_report.add_argument("--product-id", required=True, help="商品 ID")

    return parser


def _dispatch(args: argparse.Namespace) -> int:
    """按解析结果分发到子命令处理函数。"""
    if args.command == "scrape":
        if args.scrape_target == "shop":
            return run_shop_scrape(args.category_url, limit=args.limit,
                                   dry_run=args.dry_run)
        if args.scrape_target == "video":
            return run_video_scrape(args.product_id, limit=args.limit,
                                    dry_run=args.dry_run)
        if args.scrape_target == "creator":
            return run_creator_scrape(args.creator_id, dry_run=args.dry_run)
        if args.scrape_target == "hashtag":
            return run_hashtag_scrape(args.tag, dry_run=args.dry_run)
        if args.scrape_target == "itemlist":
            return run_itemlist_scrape(args.sec_uid, handle=args.handle,
                                       count=args.count, pages=args.pages,
                                       dry_run=args.dry_run)
        raise ConfigError(f"未知的 scrape 目标: {args.scrape_target}")

    if args.command == "scheduler":
        if args.start:
            return cmd_scheduler_start()
        if args.status:
            return cmd_scheduler_status()
        if args.pause:
            return cmd_scheduler_pause(args.pause)
        raise ConfigError("scheduler 需要 --start / --status / --pause 之一")

    if args.command == "init-db":
        return cmd_init_db()

    if args.command == "report":
        return cmd_report(args.product_id)

    raise ConfigError(f"未知子命令: {args.command}")


def main(argv=None) -> int:
    """CLI 入口。退出码: 0 成功 / 1 采集失败 / 2 配置或依赖错误。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except ConfigError as exc:
        print(f"[配置/依赖错误] {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except PoolUnavailableError as exc:
        # 池开启但不可用 (POOL_FAIL_POLICY=fail_closed): 依赖(Redis/池)故障,
        # 与数据库不可用同类 → 退出码 2, 不静默降级到单 session
        print(f"[池不可用] {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except (WAFBlockedException, RateLimitException, SessionExpiredException) as exc:
        print(f"[采集失败] 触发风控/会话失效: {exc}", file=sys.stderr)
        return EXIT_SCRAPE_FAILED
    except CrawlerException as exc:
        print(f"[采集失败] {exc}", file=sys.stderr)
        return EXIT_SCRAPE_FAILED


if __name__ == "__main__":
    sys.exit(main())
