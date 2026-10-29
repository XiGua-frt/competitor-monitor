"""一次性验证脚本: 池模式 + 已验证的单品 PDP 通道 (Phase 5a 闭环).

用池租约身份采集已知商品 1729480426738058662 (昨日 e2e verified 的 PDP),
验证 cookie 注入后的真实采集入库链路。用完即删, 不入库不进 git。
"""
import logging
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)-7s | %(name)s:%(lineno)d - %(message)s')

from src.pools.integration import pool_scoped_client, inject_cookie_to_client, inject_proxy_to_client
from src.scrapers.shop_scraper import ShopProductScraper
from src.database import SnapshotDiffEngine

PRODUCT_ID = '1729480426738058662'  # MY 站 Autozo Carpro, 昨日 e2e verified 基线


def make_client(lease_ctx):
    """复刻 main.py _client_for_task: 非影子且有租约时注入 cookie + IP 池代理。"""
    from src.anti_detection import AntiDetectionClient
    client = AntiDetectionClient()
    if lease_ctx.active_lease is not None:
        inject_cookie_to_client(client, lease_ctx.active_lease)
        inject_proxy_to_client(client, lease_ctx.active_lease)
    return client


def main() -> int:
    engine = SnapshotDiffEngine()

    with pool_scoped_client('shop') as ctx:
        if ctx.shadow:
            print('[i] 影子模式: 仅选号不注入')
        if ctx.lease is not None:
            acc = ctx.lease.account
            print(f'[*] 租约身份: {acc.username} ({acc.account_id}) '
                  f'cookies={len(acc.cookies or {})}')

        scraper = ShopProductScraper(client=make_client(ctx))
        data = scraper.scrape_product(PRODUCT_ID)

        if not data:
            print('[FAIL] PDP 采集失败 (池身份)')
            ctx.success = False
            return 2

        print('[OK] 采集字段:', {k: data.get(k) for k in
              ('title', 'price_current', 'rating', 'sales_total_global', 'shop_name')})

        from datetime import date
        data.setdefault('snapshot_date', date.today().isoformat())
        ok = engine.save_snapshot(data, 'product_snapshots')
        ctx.success = bool(ok)
        print('[OK] 快照写入:', ok)
        return 0 if ok else 3


if __name__ == '__main__':
    raise SystemExit(main())
