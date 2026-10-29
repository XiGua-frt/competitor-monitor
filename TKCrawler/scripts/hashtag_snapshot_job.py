#!/usr/bin/env python
"""hashtag 日常快照 job — 调度链路入口 (Agent S)。

逐标签调用 HashtagTrendScraper.scrape_hashtag_api (实机验证通道,
docs/reverse/hashtag-api.md E-8) → SnapshotDiffEngine.save_snapshot
写入 hashtag_snapshots → 打印本次 views_total 与上次快照的差分
(views_growth, 语义对齐 calculate_growth / compute_growth_values 的
NULLIF 约定: 上期为 0 或缺失时增长率 None, 不猜测)。

用法:
    /path/to/python scripts/hashtag_snapshot_job.py                # 默认标签
    /path/to/python scripts/hashtag_snapshot_job.py --tags a,b     # 指定标签
    /path/to/python scripts/hashtag_snapshot_job.py --dry-run      # 只打印计划

调度接入:
    tasks.yaml 任务 scrape_hashtag_snapshots (interval 86400) →
    main.py scheduler --start 注册 handler → import 本模块 run_job
    (import 复用, 无 subprocess 循环依赖)。

退出码 (与 main.py 一致):
    0  成功 (至少一个标签快照写入成功)
    1  采集失败 (全部标签返回 None / 触发风控)
    2  配置/依赖错误 (数据库不可用、快照写入失败)
       数据库不可用时提示: docker-compose up -d postgres

纪律: 本脚本不用于临时手动试跑真实请求 — 真实链路由主控单独验证;
标签间随机间隔 8-12s, 模拟真实节奏, 避免 API 通道被限流。
"""

import argparse
import random
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select  # noqa: E402

from src.common.exceptions import CrawlerException  # noqa: E402
from src.common.logger import get_logger  # noqa: E402
from src.database import HashtagSnapshot, SnapshotDiffEngine  # noqa: E402
from src.database.snapshot_engine import compute_growth_values  # noqa: E402
from src.scrapers.hashtag_scraper import HashtagTrendScraper  # noqa: E402

logger = get_logger("scripts.hashtag_snapshot_job")

DEFAULT_TAGS = "tiktokmademebuyit,webdev"
TABLE = "hashtag_snapshots"
GAP_RANGE: Tuple[float, float] = (8.0, 12.0)  # 标签间随机间隔(秒)

EXIT_OK = 0
EXIT_SCRAPE_FAILED = 1
EXIT_CONFIG_ERROR = 2

DB_HINT = (
    "数据库不可用: 请先启动 PostgreSQL (docker-compose up -d postgres), "
    "并核对 .env 中 POSTGRES_HOST/PORT/DB/USER/PASSWORD 配置;"
    "首次使用先执行 python main.py init-db"
)


class ConfigError(Exception):
    """配置/依赖错误 → 退出码 2 (与 main.py 语义一致)。"""


def parse_tags(raw: Optional[str]) -> List[str]:
    """逗号分隔标签串 → 去重/去空/去 '#' 的有序标签列表。"""
    if not raw:
        return []
    seen, out = set(), []
    for part in str(raw).split(","):
        tag = part.strip().lstrip("#")
        if tag and tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out


def check_db() -> None:
    """探测数据库连通性, 不可用时抛 ConfigError (退出码 2)。"""
    from sqlalchemy import text

    from src.database import engine as db_engine

    try:
        with db_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        raise ConfigError(f"{DB_HINT}\n原始错误: {exc}") from exc


def get_previous_snapshot(session_factory, tag_name: str) -> dict:
    """返回指定标签最近一次 hashtag 快照 ({views_total, snapshot_date})。

    SnapshotDiffEngine.get_latest_snapshot 仅覆盖商品表, hashtag 表的
    差分按 tag_name 维度, 故在此按同一排序语义查询。无记录返回 {}。
    """
    with session_factory() as session:
        row = session.execute(
            select(HashtagSnapshot)
            .where(HashtagSnapshot.tag_name == tag_name)
            .order_by(HashtagSnapshot.snapshot_date.desc())
            .limit(1)
        ).scalar_one_or_none()
    if row is None:
        return {}
    return {"views_total": row.views_total, "snapshot_date": row.snapshot_date}


def process_tag(scraper, diff_engine: SnapshotDiffEngine,
                session_factory, tag_name: str) -> bool:
    """单标签: 采集 → 差分打印 → 写快照。返回是否写入成功。

    顺序说明: 先取上次快照再写入, 同日重跑时 diff 基准为写入前的
    最新记录; 快照写入本身按 (tag_name, snapshot_date) upsert 幂等。
    """
    data = scraper.scrape_hashtag_api(tag_name)
    if not data:
        print(f"hashtag snapshot: 话题 #{tag_name} 采集失败 (返回空), 跳过")
        return False

    prev = get_previous_snapshot(session_factory, tag_name)
    views_total = data.get("views_total")

    if prev and prev["snapshot_date"] < data["snapshot_date"] \
            and prev["views_total"] is not None:
        growth, rate = compute_growth_values(views_total, prev["views_total"])
        rate_text = "N/A (上期为 0)" if rate is None else f"{rate:+.2%}"
        print(
            f"hashtag snapshot: #{tag_name} views_total={views_total} "
            f"(上次 {prev['snapshot_date']}: {prev['views_total']}), "
            f"增量 views_growth={growth} ({rate_text})"
        )
    else:
        reason = "无历史快照" if not prev else "上次快照为同日/缺 views_total"
        print(
            f"hashtag snapshot: #{tag_name} views_total={views_total} "
            f"(本次差分不可得: {reason})"
        )

    if not diff_engine.save_snapshot(data, TABLE):
        raise ConfigError(f"快照写入失败 (table={TABLE}, tag={tag_name})。{DB_HINT}")
    return True


def run_job(tags: Optional[List[str]] = None, dry_run: bool = False,
            scraper=None, session_factory=None,
            sleep_fn: Optional[Callable[[float], None]] = None,
            gap_range: Tuple[float, float] = GAP_RANGE) -> int:
    """job 主逻辑 (调度 handler 与 CLI 共用入口)。

    Args:
        tags: 标签列表; None 时用 DEFAULT_TAGS 拆分。
        dry_run: 只打印执行计划, 不构造 scraper、不请求、不连库。
        scraper / session_factory / sleep_fn / gap_range: 注入点, 供测试
            全 mock (不真实请求、不依赖真实 PG、不真 sleep)。

    Returns:
        退出码 0/1/2 (见模块 docstring)。
    """
    tag_list = parse_tags(DEFAULT_TAGS) if tags is None else list(tags)
    if not tag_list:
        print("hashtag snapshot: 标签列表为空, 无事可做")
        return EXIT_CONFIG_ERROR

    if dry_run:
        print("[dry-run] hashtag snapshot job")
        print(f"    tags: {', '.join(tag_list)}")
        print(f"    save_to: {TABLE} (写入 + 打印 views_growth 差分)")
        print(f"    标签间随机间隔: {gap_range[0]}-{gap_range[1]}s")
        return EXIT_OK

    check_db()

    if session_factory is None:
        from src.database.connection import SessionLocal
        session_factory = SessionLocal
    diff_engine = SnapshotDiffEngine(session_factory=session_factory)
    if scraper is None:
        from src.anti_detection import AntiDetectionClient
        scraper = HashtagTrendScraper(client=AntiDetectionClient())
    sleep_fn = sleep_fn or time.sleep

    saved = 0
    for i, tag in enumerate(tag_list):
        if i > 0:
            gap = random.uniform(*gap_range)
            logger.debug(f"标签间随机间隔 {gap:.1f}s")
            sleep_fn(gap)
        try:
            if process_tag(scraper, diff_engine, session_factory, tag):
                saved += 1
        except ConfigError:
            raise
        except CrawlerException as exc:
            # 触发风控/配额耗尽: 单标签失败, 继续剩余标签
            print(f"hashtag snapshot: 话题 #{tag} 采集异常: {exc}")
    if not saved:
        print("hashtag snapshot: 所有标签均采集失败")
        return EXIT_SCRAPE_FAILED
    print(f"hashtag snapshot: 完成, 共写入 {saved}/{len(tag_list)} 个标签快照")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hashtag_snapshot_job",
        description="hashtag 日常快照 job: 实机验证的签名 API 通道 → "
                    "hashtag_snapshots 快照 + views_total 差分打印",
        epilog="退出码: 0 成功 / 1 采集失败 / 2 配置或依赖错误",
    )
    parser.add_argument(
        "--tags", default=DEFAULT_TAGS,
        help=f"逗号分隔的标签列表 (不含 #, 默认: {DEFAULT_TAGS})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只打印执行计划, 不发起请求、不连库",
    )
    return parser


def main(argv=None) -> int:
    """CLI 入口。退出码: 0 成功 / 1 采集失败 / 2 配置或依赖错误。"""
    args = build_parser().parse_args(argv)
    try:
        return run_job(tags=parse_tags(args.tags), dry_run=args.dry_run)
    except ConfigError as exc:
        print(f"[配置/依赖错误] {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except CrawlerException as exc:
        print(f"[采集失败] 触发风控/会话失效: {exc}", file=sys.stderr)
        return EXIT_SCRAPE_FAILED


if __name__ == "__main__":
    sys.exit(main())
