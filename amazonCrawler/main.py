#!/usr/bin/env python
"""amazonCrawler 命令行入口。

子命令:
    scrape pdp     --asin B0XXXX [--all]     PDP 采集 → asin_snapshots
    scrape search  --keyword "..." [--page]  搜索排名 → keyword_rank_snapshots
    scrape offers  --asin B0XXXX             跟卖/BuyBox → offer_snapshots
    scheduler      --start | --status        调度主服务
    init-db                                  建表
    report         --asin B0XXXX [--days 30] 派生指标 JSON

退出码: 0 成功 / 1 采集失败 / 2 配置或依赖错误
"""

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

EXIT_OK = 0
EXIT_SCRAPE_FAILED = 1
EXIT_CONFIG_ERROR = 2


def cmd_scrape(args) -> int:
    from src.service.watchlist_service import WatchlistService
    from src.service.scrape_service import ScrapeService

    scrape = ScrapeService()
    if args.kind == "pdp":
        asins = _resolve_asins(args)
        if not asins:
            print("没有可采集的 ASIN (--asin 或 --all)", file=sys.stderr)
            return EXIT_CONFIG_ERROR
        results = scrape.scrape_pdp_now(asins)
        print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
        return EXIT_OK if any(r.get("ok") for r in results) else EXIT_SCRAPE_FAILED
    if args.kind == "search":
        ok, task = scrape.scrape_keyword_now(args.keyword, page=args.page)
        print(json.dumps({"ok": ok, "task": task}, ensure_ascii=False, default=str))
        return EXIT_OK if ok else EXIT_SCRAPE_FAILED
    if args.kind == "offers":
        results = scrape.scrape_offers_now(args.asin)
        print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
        return EXIT_OK if results.get("ok") else EXIT_SCRAPE_FAILED
    return EXIT_CONFIG_ERROR


def _resolve_asins(args) -> list:
    if args.asin:
        return [args.asin]
    if getattr(args, "all", False):
        wl = WatchlistService()
        return [item["asin"] for item in wl.list_items()]
    return []


def cmd_scheduler(args) -> int:
    from src.scheduler.task_scheduler import TaskScheduler

    sched = TaskScheduler(state_file=str(PROJECT_ROOT / "data" / "scheduler_state.json"))
    if args.status:
        print(json.dumps(sched.status(), ensure_ascii=False, indent=2, default=str))
        return EXIT_OK
    sched.start_with_handlers()
    print("scheduler 已启动, Ctrl+C 退出")
    try:
        sched.run_forever()
    except KeyboardInterrupt:
        pass
    return EXIT_OK


def cmd_init_db(args) -> int:
    from scripts.init_db import main as init_main

    return init_main()


def cmd_report(args) -> int:
    from src.service.metrics_service import MetricsService

    svc = MetricsService()
    report = svc.asin_report(args.asin, days=args.days)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="amazonCrawler", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_scrape = sub.add_parser("scrape", help="手动触发采集")
    scrape_sub = p_scrape.add_subparsers(dest="kind", required=True)
    p_pdp = scrape_sub.add_parser("pdp", help="PDP 快照")
    p_pdp.add_argument("--asin")
    p_pdp.add_argument("--all", action="store_true", help="采集 watchlist 全部")
    p_search = scrape_sub.add_parser("search", help="关键词排名快照")
    p_search.add_argument("--keyword", required=True)
    p_search.add_argument("--page", type=int, default=1)
    p_offers = scrape_sub.add_parser("offers", help="跟卖快照")
    p_offers.add_argument("--asin", required=True)
    p_scrape.set_defaults(func=cmd_scrape)

    p_sched = sub.add_parser("scheduler", help="调度服务")
    p_sched.add_argument("--start", action="store_true")
    p_sched.add_argument("--status", action="store_true")
    p_sched.set_defaults(func=cmd_scheduler)

    p_init = sub.add_parser("init-db", help="建表")
    p_init.set_defaults(func=cmd_init_db)

    p_report = sub.add_parser("report", help="派生指标报告")
    p_report.add_argument("--asin", required=True)
    p_report.add_argument("--days", type=int, default=30)
    p_report.set_defaults(func=cmd_report)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
