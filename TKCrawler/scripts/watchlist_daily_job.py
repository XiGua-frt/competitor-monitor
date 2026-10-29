#!/usr/bin/env python
"""种子商品清单每日快照 job —— 选品激增系数的数据源。

逐商品调用 ShopProductScraper.scrape_product (实机验证的 PDP 通道)
→ SnapshotDiffEngine.save_snapshot 写入 product_snapshots。
快照 diff 由数据库/指标层完成 (sales_velocity / 激增系数需要连续 3-4 天数据)。

用法:
    /path/to/python scripts/watchlist_daily_job.py            # 采集清单全部商品
    /path/to/python scripts/watchlist_daily_job.py --dry-run  # 只打印计划

调度接入:
    tasks.yaml 任务 scrape_watchlist (interval 1d) →
    main.py scheduler --start 注册 handler → import 本模块 run_job。

池语义 (决策记录 2026-10-11):
    一个批次 = 一个任务 = 一次租约 (任务内身份固定, 不中途换号);
    单商品失败不换号不重试整批, 记失败继续下一个;
    批次级成功判定 = 至少 1 个商品快照写入。

退出码 (与 main.py 一致):
    0  成功 (至少一个商品快照写入)
    1  采集失败 (全部商品失败 / 触发风控)
    2  配置/依赖错误 (数据库不可用、快照写入失败、清单为空)
"""

import argparse
import random
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.common.exceptions import CrawlerException  # noqa: E402
from src.common.logger import get_logger  # noqa: E402
from src.pools.integration import (  # noqa: E402
    inject_cookie_to_client,
    inject_proxy_to_client,
    pool_scoped_client,
)
from src.database import SnapshotDiffEngine  # noqa: E402
from src.scrapers.shop_scraper import ShopProductScraper  # noqa: E402

logger = get_logger("scripts.watchlist_daily_job")

DEFAULT_WATCHLIST = PROJECT_ROOT / "src" / "scheduler" / "config" / "watchlist.yaml"
TABLE = "product_snapshots"
GAP_RANGE: Tuple[float, float] = (5.0, 9.0)  # 商品间随机间隔(秒), 模拟真实节奏

EXIT_OK = 0
EXIT_SCRAPE_FAILED = 1
EXIT_CONFIG_ERROR = 2

DB_HINT = (
    "数据库不可用: 请先启动 PostgreSQL (docker-compose up -d postgres), "
    "并核对 .env 配置; 首次使用先执行 python main.py init-db"
)


class ConfigError(Exception):
    """配置/依赖错误 → 退出码 2 (与 main.py 语义一致)。"""


def load_watchlist(path: Optional[Path] = None) -> List[str]:
    """读取 watchlist.yaml, 返回去重后的 product_id 列表 (保持声明顺序)。"""
    path = Path(path or DEFAULT_WATCHLIST)
    if not path.exists():
        raise ConfigError(f"种子清单不存在: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    products = data.get("products") or []
    ids: List[str] = []
    for item in products:
        pid = str((item or {}).get("product_id") or "").strip()
        if pid and pid.isdigit() and pid not in ids:
            ids.append(pid)
    return ids


def build_client(lease_ctx):
    """按池边界构造客户端 (与 main.py _client_for_task 语义一致)。"""
    from src.anti_detection import AntiDetectionClient

    client = AntiDetectionClient()
    lease = lease_ctx.active_lease
    if lease is not None:
        inject_cookie_to_client(client, lease)
        inject_proxy_to_client(client, lease)
    return client


def run_job(products: Optional[List[str]] = None,
            watchlist_path: Optional[Path] = None,
            dry_run: bool = False,
            gap_range: Tuple[float, float] = GAP_RANGE,
            scraper_factory: Optional[Callable] = None,
            engine_factory: Optional[Callable] = None) -> int:
    """采集清单内全部商品并写快照。scraper/engine 工厂参数供测试注入。"""
    try:
        ids = list(products) if products is not None else load_watchlist(watchlist_path)
    except ConfigError as e:
        print(f"[watchlist] {e}")
        return EXIT_CONFIG_ERROR
    if not ids:
        print(f"[watchlist] 种子清单为空, 请编辑 {watchlist_path or DEFAULT_WATCHLIST} "
              "添加商品 ID (格式见文件内注释)")
        return EXIT_CONFIG_ERROR

    if dry_run:
        print(f"[watchlist][dry-run] 将采集 {len(ids)} 个商品: {ids}")
        print(f"[watchlist][dry-run] 写入表: {TABLE}; 池语义: 批次单租约")
        return EXIT_OK

    try:
        engine = (engine_factory or SnapshotDiffEngine)()
    except Exception as e:
        print(f"[watchlist] {DB_HINT}; 原始错误: {e}")
        return EXIT_CONFIG_ERROR

    saved, failed = 0, []
    with pool_scoped_client('shop', task_id=f"watchlist_{time.strftime('%Y%m%d')}") as ctx:
        lease = ctx.lease
        if lease is not None:
            print(f"[watchlist] 批次身份: {lease.account.username} "
                  f"({lease.account_id}), 本批 {len(ids)} 商品")
        scraper = (scraper_factory or (lambda client: ShopProductScraper(client=client)))(
            build_client(ctx))

        for i, pid in enumerate(ids, 1):
            try:
                data = scraper.scrape_product(pid)
            except CrawlerException as e:
                # WAF/429/限流: 纪律性停止本批次 (不硬怼), 已抓到的保留
                print(f"[watchlist] 商品 {pid} 触发风控 ({type(e).__name__}), "
                      f"停止本批次")
                failed.append(pid)
                break
            except Exception as e:
                logger.warning(f"scrape_product({pid}) 失败: {type(e).__name__}: {e}")
                failed.append(pid)
                data = None

            if not data:
                if pid not in failed:
                    failed.append(pid)
                print(f"[watchlist] ({i}/{len(ids)}) 商品 {pid} 采集失败, 跳过")
            else:
                data.setdefault('snapshot_date', time.strftime('%Y-%m-%d'))
                if engine.save_snapshot(data, TABLE):
                    saved += 1
                    print(f"[watchlist] ({i}/{len(ids)}) 商品 {pid} 快照已写入 "
                          f"({saved}/{len(ids)})")
                else:
                    print(f"[watchlist] ({i}/{len(ids)}) 商品 {pid} 快照写入失败, "
                          f"视为依赖错误")
                    ctx.success = bool(saved)
                    return EXIT_CONFIG_ERROR

            if i < len(ids):
                time.sleep(random.uniform(*gap_range))

        ctx.success = saved > 0

    print(f"[watchlist] 批次完成: 成功 {saved}/{len(ids)}"
          + (f", 失败 {len(failed)}: {failed}" if failed else ""))
    return EXIT_OK if saved else EXIT_SCRAPE_FAILED


def main() -> int:
    parser = argparse.ArgumentParser(description="种子商品清单每日快照")
    parser.add_argument('--watchlist', type=Path, default=None,
                        help=f"清单路径 (默认 {DEFAULT_WATCHLIST})")
    parser.add_argument('--dry-run', action='store_true', help="只打印计划不采集")
    args = parser.parse_args()
    return run_job(watchlist_path=args.watchlist, dry_run=args.dry_run)


if __name__ == '__main__':
    raise SystemExit(main())
