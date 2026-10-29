#!/usr/bin/env python3
"""E2E: shop 通道真实抓取 → product_snapshots 入库 → 查库核对.

任务卡: e2e_shop_2026-10-08 (只允许写 docs/probe/e2e_shop* 与本脚本).
请求预算: 总 ≤ 5 次 (本脚本实际只发 1 次 scraper 请求, 重定向在客户端内完成).

用法: .venv/bin/python scripts/e2e_shop_20261008.py [--no-db]
"""
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text as sa_text  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.anti_detection.curl_client import CurlCffiClient  # noqa: E402
from src.common.exceptions import (  # noqa: E402
    RateLimitException,
    SessionExpiredException,
    WAFBlockedException,
)
from src.database import connection as dbconn  # noqa: E402
from src.database.snapshot_engine import SnapshotDiffEngine  # noqa: E402
from src.scrapers.parsers.shop_parser import SHOP_FIELDS, ShopPageParser  # noqa: E402
from src.scrapers.shop_scraper import ShopProductScraper  # noqa: E402

PRODUCT_ID = '1729480426738058662'  # MY 站 Autozo Carpro (探针 R8 证据商品)
TODAY = date.today()
EVIDENCE_DIR = ROOT / 'docs' / 'probe'

# 探针基线 (docs/probe/shop_retest_2026-10-08.md §2, 实测时间 2026-10-08)
BASELINE = {
    'product_id': '1729480426738058662',
    'title_contains': 'HIGH PRESSURE NOZZLE',
    'price_current': 19.90,
    'price_original': 69.90,
    'sales_total_global': 1584,
    'rating': 4.7,
    'review_count': 419,
    'shop_name': 'Autozo Carpro',
    'shop_id': '7494754844264401318',
    'category_l1': 'Automotive & Motorcycle',
    'category_l2': 'Car Washing & Maintenance',
    'category_l3': 'Water Guns & Snow Foam Lances',
    'sku_count': 1,
    'stock_status': 'out_of_stock',
}


class RecordingClient:
    """包装 CurlCffiClient, 记录每次请求账目 (不改 src/)."""

    def __init__(self, inner):
        self.inner = inner
        self.log = []
        self.last_response = None

    def request(self, url, **kwargs):
        resp = self.inner.request(url, **kwargs)
        self.last_response = resp
        try:
            body_len = len(resp.content)
        except Exception:
            body_len = -1
        self.log.append({
            'url': url,
            'final_url': str(getattr(resp, 'url', '')),
            'status': getattr(resp, 'status_code', None),
            'body_bytes': body_len,
        })
        return resp

    def __getattr__(self, name):
        return getattr(self.inner, name)


def compare_with_baseline(data: dict) -> list:
    """逐字段与探针基线核对, 返回 (field, baseline, actual, verdict) 列表."""
    rows = []
    exact_keys = [k for k in BASELINE if not k.endswith('_contains')]
    for key in exact_keys:
        expected = BASELINE[key]
        actual = data.get(key)
        if key == 'rating':
            ok = actual is not None and abs(float(actual) - expected) <= 0.05
        elif key in ('sales_total_global', 'review_count'):
            ok = actual is not None and abs(int(actual) - int(expected)) <= max(
                5, int(expected) * 0.01)  # 商品可能有真实变动, 允许 ~1% 漂移
        elif key in ('price_current', 'price_original'):
            ok = actual is not None and abs(float(actual) - expected) < 0.005
        else:
            ok = str(actual) == str(expected)
        rows.append((key, expected, actual,
                     'OK' if ok else ('MISSING' if actual is None else 'DRIFT')))
    expected = BASELINE['title_contains']
    actual = data.get('title') or ''
    rows.append(('title', f'contains {expected!r}', actual,
                 'OK' if expected in actual.upper() else 'DRIFT'))
    return rows


def map_to_db_row(data: dict) -> dict:
    """15 字段 → product_snapshots 列映射 (口径 gap 显式置 NULL)."""
    return {
        'product_id': data.get('product_id'),
        'snapshot_date': TODAY.isoformat(),   # RANGE 分区键
        'title': data.get('title'),
        'category_l1': data.get('category_l1'),
        'category_l2': data.get('category_l2'),
        'category_l3': data.get('category_l3'),
        'price_current': data.get('price_current'),
        'price_original': data.get('price_original'),
        # 口径 gap: sales_total_global(上架以来全球销量) ≠ sales_30d, 严禁混写 → NULL
        'sales_30d': None,
        'gmv_30d_est': None,
        'listed_date': None,
        'shop_id': data.get('shop_id'),
        'shop_name': data.get('shop_name'),
        'creator_count': None,   # shop 通道无此数据
        'video_count_7d': None,  # shop 通道无此数据
        'review_count': data.get('review_count'),
        'rating': data.get('rating'),
    }


def main():
    no_db = '--no-db' in sys.argv
    db_only = '--db-only' in sys.argv  # 从证据 JSON 重放解析, 零新请求

    if db_only:
        evidence = EVIDENCE_DIR / 'e2e_shop_response_2026-10-08.json'
        router = json.loads(evidence.read_text(encoding='utf-8'))
        html = (
            '<html><body><script type="application/json" '
            'id="__MODERN_ROUTER_DATA__">'
            + json.dumps(router) + '</script></body></html>'
        )
        data = ShopPageParser().parse(html)
        client = None
        if not data:
            print('[e2e] FAIL 证据 JSON 重放解析为空')
            return 2
        print('[e2e] (--db-only) 从证据 JSON 重放解析完成, 零新请求')
    else:
        # ---- 1. 真实抓取 (1 次 scraper 请求) ----
        client = RecordingClient(CurlCffiClient())
        scraper = ShopProductScraper(client=client, region='MY')
        print(f"[e2e] scraping product {PRODUCT_ID} region=MY ...")
        try:
            data = scraper.scrape_product(PRODUCT_ID)
        except SessionExpiredException as exc:
            print(f"[e2e] FAIL SessionExpiredException: {exc}")
            return 2
        except (RateLimitException, WAFBlockedException) as exc:
            print(f"[e2e] FAIL 风控异常: {type(exc).__name__}: {exc}")
            return 2

    if not data:
        print("[e2e] FAIL scrape_product 返回 None (壳页/区域错误/解析失败)")
        if client is not None:
            _dump_account(client)
        return 2

    print(f"[e2e] 抓到字段 {sum(1 for v in data.values() if v is not None)}"
          f"/{len(SHOP_FIELDS)} (None 字段: "
          f"{[f for f, v in data.items() if v is None]})")
    for f in SHOP_FIELDS:
        val = data.get(f)
        if f == 'image_urls' and val:
            print(f"  {f}: {len(val)} 张, 首张 {val[0][:80]}...")
        else:
            print(f"  {f}: {val!r}")

    # ---- 2. 原始响应落档 (router data JSON; 仅真实抓取分支) ----
    router = None
    last_resp = getattr(client, 'last_response', None)
    if last_resp is not None:
        router = ShopPageParser().extract_router_data(last_resp.text)
    if router is not None:
        out = EVIDENCE_DIR / 'e2e_shop_response_2026-10-08.json'
        out.write_text(json.dumps(router, ensure_ascii=False, indent=1),
                       encoding='utf-8')
        print(f"[e2e] 原始 router data 已存 {out}")
    elif client is not None:
        print("[e2e] WARN 未能提取 router data (证据 JSON 缺失)")
    if client is not None:
        _dump_account(client)

    # ---- 3. 与探针基线核对 ----
    rows = compare_with_baseline(data)
    print("[e2e] 与探针基线核对:")
    for key, expected, actual, verdict in rows:
        print(f"  [{verdict:7s}] {key}: baseline={expected!r} actual={actual!r}")
    drift = [r for r in rows if r[3] != 'OK']

    if no_db:
        print("[e2e] --no-db: 跳过入库与查库")
        return 0

    # ---- 4. 入库 ----
    # NOTE: connection.ensure_month_partition() 在省略 engine_ 参数时触发
    # NameError (connection.py:108 引用未定义的 `engine`, 应为 _get_engine());
    # 本脚本显式传参绕过 (src bug 只记录不修)。
    dbconn.ensure_month_partition(TODAY, engine_=dbconn.engine)
    engine = SnapshotDiffEngine()
    row = map_to_db_row(data)
    ok = engine.save_snapshot(row, 'product_snapshots')
    print(f"[e2e] save_snapshot(product_snapshots) -> {ok}")
    if not ok:
        return 3

    # ---- 5. 查库核对 ----
    with dbconn.engine.connect() as conn:
        result = conn.execute(
            sa_text("SELECT * FROM product_snapshots "
                    "WHERE product_id = :pid AND snapshot_date = :d"),
            {'pid': PRODUCT_ID, 'd': TODAY.isoformat()},
        )
        db_row = result.mappings().first()
    if db_row is None:
        print("[e2e] FAIL 查库未找到今天快照")
        return 3
    print(f"[e2e] DB 行 (product_id={db_row['product_id']}, "
          f"snapshot_date={db_row['snapshot_date']}):")
    db_diff = []
    for col in row:
        actual = db_row[col]
        expected = row[col]
        # Decimal(19.90) vs float 19.9 数值等价, 统一走数值比较
        if isinstance(actual, Decimal):
            match = expected is not None and float(actual) == float(expected)
        elif expected is not None:
            match = str(actual) == str(expected)
        else:
            match = actual is None
        if not match:
            db_diff.append(col)
        print(f"  {'OK ' if match else 'DIFF'} {col} = {actual!r}")
    print(f"[e2e] created_at = {db_row['created_at']}")
    print(f"[e2e] DB 核对: {len(row) - len(db_diff)}/{len(row)} 列一致"
          + (f", 不一致: {db_diff}" if db_diff else ""))

    # ---- 汇总 ----
    summary = {
        'product_id': PRODUCT_ID,
        'fields_total': len(SHOP_FIELDS),
        'fields_filled': sum(1 for v in data.values() if v is not None),
        'request_log': client.log if client is not None else [],
        'baseline_drift': [(r[0], r[1], _safe(r[2])) for r in drift],
        'db_diff_cols': db_diff,
        'db_saved': ok,
    }
    print("[e2e] SUMMARY_JSON: " + json.dumps(summary, ensure_ascii=False))
    return 0


def _safe(v):
    return v if isinstance(v, (int, float, str, type(None))) else str(v)


def _dump_account(client):
    print("[e2e] 请求账目:")
    for i, e in enumerate(client.log, 1):
        print(f"  R{i}: {e['status']} {e['url']} -> {e['final_url']} "
              f"({e['body_bytes']}B)")


if __name__ == '__main__':
    sys.exit(main())
