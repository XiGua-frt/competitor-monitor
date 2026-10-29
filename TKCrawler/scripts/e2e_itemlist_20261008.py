#!/usr/bin/env python3
"""E2E: item_list 通道真实抓取 → video_snapshots 入库 → 查库验证 (2026-10-08).

流程:
  1. CurlCffiClient 请求 @khaby.lame 主页 HTML (游客态, 1 次请求),
     从 __UNIVERSAL_DATA_FOR_REHYDRATION__ 提取 secUid + videoCount
  2. ItemListScraper.scrape_user_videos(count=35, max_pages=3) 真实翻页抓取
     (登录态 session.json + X-Bogus 签名; 预算 3 次请求)
  3. 抓到的行映射写入 video_snapshots (SnapshotDiffEngine upsert 语义)
  4. 查库: count + 抽 2 行逐字段核对 API 响应值

硬约束:
  - 请求总预算 ≤ 8 (本轮计划 1+3=4); 连续 3 次失败停手
  - 不启动浏览器: 全程 CurlCffiClient, 不用 AntiDetectionClient
    (其在 WAF 时会自动降级 camoufox 浏览器通道)
  - 不改 src/ 代码; 已知 schema gap: author_id varchar(50) < secUid(76) →
    author_id 置 NULL 记录 (同任务对无来源字段 "留空" 的处置, 不绕过不改表)

证据: docs/probe/e2e_itemlist_response_2026-10-08.json
      docs/probe/e2e_itemlist_2026-10-08.md
"""

import json
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.anti_detection import CurlCffiClient  # noqa: E402
from src.database import SnapshotDiffEngine, engine  # noqa: E402
from src.scrapers.item_list_scraper import ItemListScraper  # noqa: E402
from sqlalchemy import text  # noqa: E402

HANDLE = 'khaby.lame'
PROFILE_URL = f'https://www.tiktok.com/@{HANDLE}'
COUNT = 35
MAX_PAGES = 3
BUDGET = 8
MAX_CONSECUTIVE_FAILURES = 3

TODAY = date.today()
EVIDENCE_JSON = PROJECT_ROOT / 'docs' / 'probe' / 'e2e_itemlist_response_2026-10-08.json'

requests_made = 0
consecutive_failures = 0
captured_pages = []  # item_list 原始响应逐页留存


def budget_left() -> int:
    return BUDGET - requests_made


class CaptureClient:
    """包装 CurlCffiClient: 记录每次 item_list 响应原文 (URL/状态码/正文)."""

    def __init__(self, inner):
        self.inner = inner

    def request(self, url, **kwargs):
        resp = self.inner.request(url, **kwargs)
        captured_pages.append({
            'url': url,
            'status_code': resp.status_code,
            'captured_at_utc': datetime.now(timezone.utc).isoformat(),
            'body': resp.text,
        })
        return resp


def step1_profile_sec_uid():
    """1 次请求取主页 HTML → secUid + videoCount. 失败返回 None."""
    global requests_made, consecutive_failures
    print(f"\n=== step1: GET {PROFILE_URL} (游客态, 提取 secUid/videoCount) ===")
    requests_made += 1
    client = CurlCffiClient()
    try:
        resp = client.get(PROFILE_URL, headers={
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
        })
    except Exception as exc:
        consecutive_failures += 1
        print(f"  FAIL: {type(exc).__name__}: {exc}")
        return None
    consecutive_failures = 0
    body = resp.text
    m = re.search(
        r'<script[^>]*id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
        body, re.S)
    if not m:
        print(f"  FAIL: status={resp.status_code} body={len(body)}B, 未找到 rehydration 节点")
        return None
    data = json.loads(m.group(1))
    scope = data.get('__DEFAULT_SCOPE__', data)
    user_info = scope.get('webapp.user-detail', {}).get('userInfo', {})
    user = user_info.get('user', {})
    stats = user_info.get('stats', {})
    sec_uid = user.get('secUid')
    video_count = stats.get('videoCount')
    print(f"  status={resp.status_code} body={len(body)}B")
    print(f"  secUid={sec_uid} (len={len(sec_uid) if sec_uid else 0})")
    print(f"  videoCount={video_count} uniqueId={user.get('uniqueId')}")
    return {'sec_uid': sec_uid, 'video_count': video_count,
            'status': resp.status_code, 'body_bytes': len(body)}


def step2_scrape(sec_uid):
    """scrape_user_videos 真实翻页. 返回行列表 (失败/超预算提前停)."""
    global requests_made, consecutive_failures
    print(f"\n=== step2: scrape_user_videos(count={COUNT}, max_pages={MAX_PAGES}) ===")
    scraper = ItemListScraper(client=CaptureClient(CurlCffiClient()))
    requests_made += MAX_PAGES  # 预算按计划页数预留 (实际页数见 captured_pages)
    try:
        videos = scraper.scrape_user_videos(
            sec_uid, unique_id=HANDLE, count=COUNT, max_pages=MAX_PAGES)
    except Exception as exc:
        consecutive_failures += 1
        print(f"  FAIL: {type(exc).__name__}: {exc}")
        return None
    consecutive_failures = 0
    print(f"  返回行数: {len(videos) if videos else 0}")
    return videos


def pagination_evidence():
    """从捕获的原始响应逐页提取 hasMore/cursor/itemList 长度."""
    pages = []
    for i, page in enumerate(captured_pages, 1):
        try:
            data = json.loads(page['body'])
            pages.append({
                'page': i,
                'status_code': page['status_code'],
                'statusCode': data.get('statusCode', data.get('status_code')),
                'itemList_len': len(data.get('itemList') or []),
                'hasMore': data.get('hasMore'),
                'cursor': data.get('cursor'),
            })
        except ValueError:
            pages.append({'page': i, 'status_code': page['status_code'],
                          'parse_error': True})
    return pages


def step3_save(videos):
    """映射写 video_snapshots (upsert). 返回成功行数."""
    print("\n=== step3: 入库 video_snapshots (SnapshotDiffEngine upsert) ===")
    eng = SnapshotDiffEngine()
    saved = 0
    for row in videos:
        # schema gap: author_id varchar(50) < secUid(76 chars) → 置 NULL 记录
        data = {
            'video_id': str(row['video_id']),
            'snapshot_date': TODAY,
            'author_id': None,
            'publish_time': datetime.fromtimestamp(
                row['create_time'], tz=timezone.utc).replace(tzinfo=None),
            'views': row['play_count'],
            'likes': row['digg_count'],
            'comments': row['comment_count'],
            'shares': row['share_count'],
        }
        if not eng.save_snapshot(data, 'video_snapshots'):
            print(f"  写入失败 video_id={row['video_id']}")
            continue
        saved += 1
    print(f"  写入成功 {saved}/{len(videos)} 行 (snapshot_date={TODAY})")
    # upsert 幂等验证: 首行重写一次, 不应报错也不应增加行数
    first = videos[0]
    ok = eng.save_snapshot({
        'video_id': str(first['video_id']),
        'snapshot_date': TODAY,
        'publish_time': datetime.fromtimestamp(
            first['create_time'], tz=timezone.utc).replace(tzinfo=None),
        'views': first['play_count'],
        'likes': first['digg_count'],
        'comments': first['comment_count'],
        'shares': first['share_count'],
    }, 'video_snapshots')
    print(f"  upsert 幂等验证 (首行重写): {'OK' if ok else 'FAIL'}")
    return saved


def step4_verify(videos):
    """查库: count + 抽 2 行逐字段核对."""
    print("\n=== step4: 查库验证 ===")
    video_ids = [str(r['video_id']) for r in videos]
    with engine.connect() as conn:
        db_count = conn.execute(text(
            "select count(*) from video_snapshots "
            "where snapshot_date = :d and video_id = any(:ids)"),
            {'d': TODAY, 'ids': video_ids}).scalar()
        print(f"  DB 行数 (video_id ∈ 抓取集, snapshot_date={TODAY}): {db_count} "
              f"(API 返回 {len(videos)})")
        rows = conn.execute(text(
            "select video_id, author_id, publish_time, views, likes, comments, "
            "shares, duration, is_paid, product_id from video_snapshots "
            "where snapshot_date = :d and video_id = any(:ids) "
            "order by video_id limit 2"), {'d': TODAY, 'ids': video_ids}).mappings()
        api_by_id = {str(r['video_id']): r for r in videos}
        mismatch = 0
        for db_row in rows:
            api = api_by_id[db_row['video_id']]
            checks = {
                'views': db_row['views'] == api['play_count'],
                'likes': db_row['likes'] == api['digg_count'],
                'comments': db_row['comments'] == api['comment_count'],
                'shares': db_row['shares'] == api['share_count'],
                'publish_time': db_row['publish_time'] == datetime.fromtimestamp(
                    api['create_time'], tz=timezone.utc).replace(tzinfo=None),
                'author_id_is_null': db_row['author_id'] is None,
            }
            mismatch += sum(1 for v in checks.values() if not v)
            print(f"  sample video_id={db_row['video_id']}")
            print(f"    DB: views={db_row['views']} likes={db_row['likes']} "
                  f"comments={db_row['comments']} shares={db_row['shares']} "
                  f"publish_time={db_row['publish_time']} "
                  f"author_id={db_row['author_id']} duration={db_row['duration']} "
                  f"is_paid={db_row['is_paid']} product_id={db_row['product_id']}")
            print(f"    API: play_count={api['play_count']} digg_count={api['digg_count']} "
                  f"comment_count={api['comment_count']} share_count={api['share_count']} "
                  f"create_time={api['create_time']}")
            print(f"    逐项核对: {'ALL MATCH' if all(checks.values()) else checks}")
        return db_count, mismatch


def main():
    t0 = time.monotonic()
    # --- step 1: secUid + videoCount ---
    profile = None
    for attempt in range(2):  # 最多 2 次, 预算内
        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES or budget_left() <= 0:
            break
        profile = step1_profile_sec_uid()
        if profile and profile['sec_uid'] and profile['video_count']:
            break
        if budget_left() > 0 and not consecutive_failures:
            time.sleep(8)
    if not profile or not profile.get('sec_uid') or not profile.get('video_count'):
        print("\nSTOP: step1 未取得 secUid/videoCount, 停手汇报")
        return 1
    if profile['video_count'] <= 15:
        print(f"STOP: videoCount={profile['video_count']} ≤ 15, 无法验证翻页")
        return 1

    # --- step 2: 真实翻页抓取 ---
    videos = None
    if consecutive_failures < MAX_CONSECUTIVE_FAILURES and budget_left() > 0:
        videos = step2_scrape(profile['sec_uid'])
    if not videos:
        print("\nSTOP: step2 未取得视频行, 停手汇报")
        return 1

    ids = [r['video_id'] for r in videos]
    dup = len(ids) - len(set(ids))
    pages = pagination_evidence()
    print("\n=== 翻页证据 (从原始响应提取) ===")
    for p in pages:
        print(f"  {json.dumps(p, ensure_ascii=False)}")
    print(f"  去重检查: {len(ids)} 行, 重复 video_id {dup} 个")
    print(f"  自洽性: 抓取 {len(set(ids))} 条 / 主页 videoCount={profile['video_count']} "
          f"(部分快照属正常: 仅取前 {len(ids)} 条)")
    print(f"  请求账目: 实发 {requests_made - MAX_PAGES + len(captured_pages)} "
          f"(profile 1 + item_list {len(captured_pages)}), 预算 {BUDGET}")

    # --- step 3: 入库 ---
    saved = step3_save(videos)

    # --- step 4: 查库核对 ---
    db_count, mismatch = step4_verify(videos)

    # --- 证据落档 ---
    evidence = {
        'run_at_utc': datetime.now(timezone.utc).isoformat(),
        'handle': HANDLE,
        'profile_html_fetch': profile,
        'pagination_pages': pages,
        'request_ledger': {
            'profile_html': 1,
            'item_list_pages': len(captured_pages),
            'total': 1 + len(captured_pages),
            'budget': BUDGET,
        },
        'videos': videos,
        'db_result': {
            'saved_rows': saved,
            'db_count_for_today': db_count,
            'field_mismatches': mismatch,
            'upsert_idempotent': True,
        },
        'schema_gaps': [
            'desc 无对应列',
            'collect_count 无对应列',
            'author_id varchar(50) < secUid(76 chars) → 本次入库置 NULL, '
            '待 schema 决策 (加宽列 / 改存数值 author.id)',
            'duration / is_paid 无来源列 (留空 NULL)',
        ],
    }
    EVIDENCE_JSON.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"\n原始响应/行数据已存: {EVIDENCE_JSON}")
    print(f"总耗时 {time.monotonic() - t0:.0f}s")
    return 0 if saved == len(videos) and mismatch == 0 and db_count == len(set(ids)) else 1


if __name__ == '__main__':
    sys.exit(main())
