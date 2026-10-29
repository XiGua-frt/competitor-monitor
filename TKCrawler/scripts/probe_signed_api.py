#!/usr/bin/env python3
"""Agent P probe: LIVE verification of TikTokSigner X-Bogus against the
public signed APIs (guest, no credentials).

Router context: reverse-skill-router R3 (js-reverse) -> this is the
"request replay" leg of the Rebuild workflow: the X-Bogus algorithm was
already rebuilt in src/scrapers/signer.py (docs/reverse/video-api.md);
this probe tests server acceptance end-to-end.

Hard discipline (task card, Agent P):
- Total request budget <= 10 (signed-API first live run, small iteration
  allowance for parameter drift).
- Min 8s (random 8-15s) interval between requests.
- On 403/429/418 -> HARD STOP, save evidence, no retry storm.
- Public APIs only, guest cookie mode, NO credentials / no .env contents
  in evidence.
- Auditable: prints full signed URL + signature headers before every
  request, status after; saves URL/status/headers/body per request to
  docs/probe/ with timestamps.

Hypotheses under test (marked as assumptions, sources):
- /api/challenge/detail/?challengeName=...      (public knowledge:
  davidteather/TikTok-Api, drawrowfly/tiktok-scraper)
- /api/challenge/item_list/?challengeID=...     (same sources)
- /api/post/item_list/?secUid=...&count&cursor  (same sources; secUid for
  khaby.lame taken FREE from prior Agent O evidence
  docs/probe/20261007T144901Z_creator_khaby_lame_try1_embedded_*.json --
  no extra request spent)
- msToken: guest mode uses a DUMMY token (random 116-char) embedded both
  in query (participates in signature) and as Cookie -- community
  practice; validity itself is part of what this probe tests.
- X-Bogus placement: appended to query (canonical) AND duplicated as a
  request header (belt-and-suspenders; signer.py flags this as an
  assumption).

Endpoints decision tree:
  1. challenge/detail (challengeName=webdev) -> expect challenge id +
     stats. If valid JSON, use its challengeID for step 2.
  2. challenge/item_list (challengeID from step 1) -> expect itemList.
  3. post/item_list (secUid=khaby) -> expect itemList with playCount.
"""

import json
import random
import re
import string
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from curl_cffi import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.scrapers.signer import TikTokSigner  # noqa: E402

PROBE_DIR = PROJECT_ROOT / 'docs' / 'probe'
PROBE_DIR.mkdir(parents=True, exist_ok=True)

PROXY = 'http://127.0.0.1:7892'
IMPERSONATE = 'chrome131'
MAX_REQUESTS = 10          # hard budget (task card)
MIN_INTERVAL = 8.0
MAX_INTERVAL = 15.0
BODY_SAVE_LIMIT = 200 * 1024

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')

# Guest-mode msToken: dummy value (community practice: value content does
# not need to be a real server-issued token for several guest endpoints;
# whether that holds here is PART of what this probe verifies).
MS_TOKEN_DUMMY = ''.join(random.choices(
    string.ascii_letters + string.digits + '-_', k=116))

KHABY_SECUID = (
    'MS4wLjABAAAAwAg0rSzO65WQfz4RzQgGv2Xdv108BgPXhRrrmNVIHQZ9PO8-flwwRtEppYTS0OjA'
)
# Source: docs/probe/20261007T144901Z_creator_khaby_lame_try1_embedded_*
# (Agent O evidence, public profile page embedded JSON -- no credential).

BASE_PARAMS = {
    'aid': '1988',
    'app_language': 'en',
    'app_name': 'tiktok_web',
    'browser_language': 'en-US',
    'browser_name': 'Mozilla',
    'browser_online': 'true',
    'browser_platform': 'Win32',
    'browser_version': '5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                       ' (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'channel': 'tiktok_web',
    'cookie_enabled': 'true',
    'device_platform': 'web_pc',
    'focus_state': 'true',
    'from_page': 'hashtag',
    'os': 'windows',
    'priority_region': '',
    'referer': '',
    'region': 'US',
    'screen_height': '1080',
    'screen_width': '1920',
    'tz_name': 'America/New_York',
    'webcast_language': 'en',
    'msToken': MS_TOKEN_DUMMY,
}


def build_signed_url(path: str, extra: dict, referer: str) -> tuple:
    """Build query -> sign with TikTokSigner -> return (signed_url, headers)."""
    params = dict(BASE_PARAMS)
    params['referer'] = referer
    params.update(extra)
    base_url = f'https://www.tiktok.com{path}?{urlencode(params)}'

    signer = TikTokSigner(user_agent=UA)
    signed_url = signer.sign_url(base_url)

    headers = {
        'User-Agent': UA,
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': referer,
        # Duplicate signature header (signer.py assumption; query param is
        # the canonical placement per public implementations).
        'X-Bogus': signed_url.rsplit('X-Bogus=', 1)[1],
        # Guest cookie mode: dummy msToken cookie, no session cookies.
        'Cookie': f'msToken={MS_TOKEN_DUMMY}',
    }
    return signed_url, headers


def save_evidence(label: str, signed_url: str, sent_headers: dict, status: int,
                  elapsed_ms: int, resp_headers, body: bytes,
                  error: str = None) -> Path:
    ts = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    base = f"{ts}_{label}"
    meta = {
        'label': label,
        'signed_url': signed_url,
        'sent_headers': sent_headers,
        'status_code': status,
        'elapsed_ms': elapsed_ms,
        'error': error,
        'captured_at_utc': ts,
        'proxy': PROXY,
        'impersonate': IMPERSONATE,
        'cookie_note': 'guest mode: dummy msToken cookie only, no credentials',
        'response_headers': dict(resp_headers) if resp_headers is not None else None,
        'body_bytes_captured': len(body),
    }
    meta_path = PROBE_DIR / f"{base}_meta.json"
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False),
                         encoding='utf-8')
    body_path = PROBE_DIR / f"{base}_body.json"
    body_path.write_bytes(body[:BODY_SAVE_LIMIT])
    print(f"  evidence: {meta_path.name} / {body_path.name}")
    return body_path


def summarize_json(body: bytes) -> dict:
    """Extract structural verdict from a response body."""
    text = body.decode('utf-8', errors='replace')
    summary = {'content_type_guess': 'json' if text.lstrip()[:1] in '{['
               else 'non-json'}
    if summary['content_type_guess'] != 'json':
        summary['body_head'] = text[:300]
        return summary
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        summary['content_type_guess'] = 'json-parse-error'
        summary['json_error'] = str(exc)
        summary['body_head'] = text[:300]
        return summary
    summary['top_keys'] = sorted(data.keys()) if isinstance(data, dict) else \
        f'array[{len(data)}]'
    if not isinstance(data, dict):
        return summary
    # Common TikTok API envelope fields
    for key in ('statusCode', 'status_code', 'statusMsg', 'error', 'message'):
        if key in data:
            summary[key] = data[key]
    # challenge/detail shape
    ci = data.get('challengeInfo') or data.get('challenge') or {}
    if isinstance(ci, dict):
        ch = ci.get('challenge') or {}
        if ch:
            summary['challenge.id'] = ch.get('id')
            summary['challenge.challengeName'] = ch.get('challengeName')
            summary['challenge.stats'] = ch.get('stats') or ch.get('statsV2')
    # item list shapes
    for key in ('itemList', 'item_list', 'aweme_list'):
        items = data.get(key)
        if isinstance(items, list):
            summary[f'{key}.count'] = len(items)
            if items and isinstance(items[0], dict):
                first = items[0]
                summary[f'{key}[0].keys'] = sorted(first.keys())[:30]
                stats = first.get('stats') or first.get('statistics') or {}
                if stats:
                    summary[f'{key}[0].stats'] = {
                        k: stats.get(k) for k in
                        ('playCount', 'play_count', 'diggCount', 'digg_count',
                         'commentCount', 'comment_count')
                    }
                summary[f'{key}[0].id'] = first.get('id') or first.get('aweme_id')
                summary[f'{key}[0].createTime'] = first.get('createTime') or \
                    first.get('create_time')
    if 'hasMore' in data:
        summary['hasMore'] = data['hasMore']
    if 'cursor' in data:
        summary['cursor'] = data['cursor']
    return summary


def do_request(label: str, path: str, extra: dict, referer: str,
               counter: dict) -> str:
    """One request round-trip. Returns 'ok' | 'stop' | 'fail'."""
    if counter['n'] >= MAX_REQUESTS:
        print(f"\n[{label}] SKIPPED: request budget exhausted "
              f"({MAX_REQUESTS}).")
        return 'fail'

    counter['n'] += 1
    signed_url, headers = build_signed_url(path, extra, referer)
    print(f"\n=== request #{counter['n']}/{MAX_REQUESTS} [{label}] ===")
    print(f"  METHOD GET {path}")
    print(f"  signed URL ({len(signed_url)} chars):")
    print(f"    {signed_url}")
    print(f"  signature headers: X-Bogus={headers['X-Bogus']} "
          f"(UA-bound Chrome/131, msToken dummy 116ch)")
    print(f"  pre-request audit: budget={counter['n']}/{MAX_REQUESTS}, "
          f"proxy={PROXY}")

    session = requests.Session(impersonate=IMPERSONATE, proxies={
        'http': PROXY, 'https': PROXY})
    status, elapsed_ms, resp_headers, body, error = 0, 0, None, b'', None
    t0 = time.monotonic()
    try:
        resp = session.get(signed_url, headers=headers, timeout=30)
        status = resp.status_code
        resp_headers = resp.headers
        body = resp.content
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    save_evidence(label, signed_url, headers, status, elapsed_ms,
                  resp_headers, body, error)

    if error:
        print(f"  RESULT: NETWORK ERROR: {error} ({elapsed_ms}ms)")
        return 'fail'

    print(f"  RESULT: status={status} elapsed={elapsed_ms}ms body={len(body)}B")

    if status in (403, 429, 418):
        print("  !! HARD STOP: block/rate-limit status. No further requests "
              "per discipline. Evidence saved.")
        return 'stop'

    summary = summarize_json(body)
    print(f"  summary: {json.dumps(summary, ensure_ascii=False, default=str)}")
    return 'ok' if status == 200 else 'fail'


def _glob_files(prefix_pattern: str):
    return sorted(PROBE_DIR.glob(prefix_pattern),
                  key=lambda p: p.stat().st_mtime)


def extract_guest_cookie_jar() -> dict:
    """Zero-request: harvest guest cookies from prior probe evidence
    (Set-Cookie response headers saved in *_meta.json).

    These are guest session identifiers served to any anonymous visitor,
    not credentials. Source files are cited in the report.
    """
    jar = {}
    sources = []
    for meta_path in sorted(PROBE_DIR.glob('*_meta.json')):
        if '_api_' in meta_path.name:
            continue
        meta = json.loads(meta_path.read_text(encoding='utf-8'))
        sc = (meta.get('response_headers') or {}).get('set-cookie')
        if not sc:
            continue
        sources.append(meta_path.name)
        for part in sc.split(', '):
            name, _, value = part.partition('=')
            name = name.strip()
            value = value.split(';', 1)[0].strip()
            if name in ('ttwid', 'msToken', 'tt_csrf_token', 'tt_chain_token'):
                jar[name] = value
    print(f"  cookie jar keys: {sorted(jar.keys())}")
    print(f"  harvested from evidence: {sources}")
    return jar


def run_single(counter: dict, endpoint: str, args: list, use_cookie_jar: bool):
    """Targeted one-shot mode (iteration within budget, auditable)."""
    cookie_str = ''
    if use_cookie_jar:
        jar = extract_guest_cookie_jar()
        cookie_str = '; '.join(f'{k}={v}' for k, v in jar.items())
        # keep query msToken consistent with the cookie msToken
        if 'msToken' in jar:
            BASE_PARAMS['msToken'] = jar['msToken']

    if endpoint == 'challenge_item_list':
        challenge_id, count, cursor = args[0], args[1], args[2]
        extra = {'challengeID': challenge_id, 'count': count, 'cursor': cursor}
        referer = 'https://www.tiktok.com/tag/webdev'
        path = '/api/challenge/item_list/'
    elif endpoint == 'post_item_list':
        sec_uid, count, cursor, from_page = \
            args[0], args[1], args[2], args[3]
        extra = {'secUid': sec_uid, 'count': count, 'cursor': cursor,
                 'sourceType': '8', 'coverFormat': '2',
                 'needPinnedItemIds': 'false', 'withQABis': 'true'}
        referer = 'https://www.tiktok.com/@khaby.lame'
        path = '/api/post/item_list/'
        BASE_PARAMS['from_page'] = from_page
    else:
        raise ValueError(f'unknown endpoint: {endpoint}')

    query = urlencode({**BASE_PARAMS, **extra})
    base_url = f'https://www.tiktok.com{path}?{query}'
    signer = TikTokSigner(user_agent=UA)
    signed_url = signer.sign_url(base_url)
    headers = {
        'User-Agent': UA,
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': referer,
        'X-Bogus': signed_url.rsplit('X-Bogus=', 1)[1],
    }
    if cookie_str:
        headers['Cookie'] = cookie_str
        print("  cookie mode: EVIDENCE-HARVESTED guest cookies "
              "(ttwid/msToken/tt_csrf_token/tt_chain_token)")
    else:
        headers['Cookie'] = f'msToken={MS_TOKEN_DUMMY}'
        print("  cookie mode: dummy msToken only")

    counter['n'] += 1
    print(f"\n=== request #{counter['n']}/{MAX_REQUESTS} [{path} "
          f"from_page={BASE_PARAMS['from_page']}] ===")
    print(f"  signed URL ({len(signed_url)} chars):")
    print(f"    {signed_url}")
    session = requests.Session(impersonate=IMPERSONATE, proxies={
        'http': PROXY, 'https': PROXY})
    status, elapsed_ms, resp_headers, body, error = 0, 0, None, b'', None
    t0 = time.monotonic()
    try:
        resp = session.get(signed_url, headers=headers, timeout=30)
        status = resp.status_code
        resp_headers = resp.headers
        body = resp.content
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    save_evidence(f'api_{endpoint}', signed_url, headers, status,
                  elapsed_ms, resp_headers, body, error)
    if error:
        print(f"  RESULT: NETWORK ERROR: {error}")
        return 'fail'
    print(f"  RESULT: status={status} elapsed={elapsed_ms}ms body={len(body)}B")
    if status in (403, 429, 418):
        print("  !! HARD STOP per discipline.")
        return 'stop'
    print(f"  summary: "
          f"{json.dumps(summarize_json(body), ensure_ascii=False, default=str)}")
    return 'ok' if status == 200 else 'fail'


def main():
    counter = {'n': 0}
    stopped = False
    challenge_id = None

    # --- 1. challenge/detail -------------------------------------------
    outcome = do_request(
        'api_challenge_detail', '/api/challenge/detail/',
        {'challengeName': 'webdev'},
        'https://www.tiktok.com/tag/webdev',
        counter,
    )
    if outcome == 'stop':
        stopped = True

    # Try to harvest challengeID from the latest detail evidence for step 2
    if outcome == 'ok':
        detail_files = _glob_files('*_api_challenge_detail_body.json')
        if detail_files:
            try:
                data = json.loads(
                    detail_files[-1].read_text(encoding='utf-8',
                                               errors='replace'))
                ch = (data.get('challengeInfo') or {}).get('challenge') or {}
                challenge_id = ch.get('id')
            except Exception as exc:
                print(f"  (could not parse detail evidence: {exc})")
    print(f"\n--> challengeID harvested: {challenge_id}")

    # --- 2. challenge/item_list ----------------------------------------
    if not stopped and challenge_id:
        wait = random.uniform(MIN_INTERVAL, MAX_INTERVAL)
        print(f"\n-- sleeping {wait:.1f}s before next request --")
        time.sleep(wait)
        outcome = do_request(
            'api_challenge_item_list', '/api/challenge/item_list/',
            {'challengeID': str(challenge_id), 'count': '20', 'cursor': '0'},
            'https://www.tiktok.com/tag/webdev',
            counter,
        )
        if outcome == 'stop':
            stopped = True
    elif not stopped:
        print("\n[api_challenge_item_list] SKIPPED: no challengeID "
              "(detail step failed) -- budget preserved for post/item_list.")

    # --- 3. post/item_list ----------------------------------------------
    if not stopped and counter['n'] < MAX_REQUESTS:
        wait = random.uniform(MIN_INTERVAL, MAX_INTERVAL)
        print(f"\n-- sleeping {wait:.1f}s before next request --")
        time.sleep(wait)
        params = {'secUid': KHABY_SECUID, 'count': '20', 'cursor': '0',
                  'sourceType': '8', 'coverFormat': '2',
                  'needPinnedItemIds': 'false', 'withQABis': 'true'}
        outcome = do_request(
            'api_post_item_list', '/api/post/item_list/',
            params,
            'https://www.tiktok.com/@khaby.lame',
            counter,
        )
        if outcome == 'stop':
            stopped = True

    print(f"\nDone. total_requests={counter['n']}/{MAX_REQUESTS} "
          f"stopped_early={stopped}")
    return 0


def scan_evidence():
    """Local (zero-request) scan of prior probe evidence: look for real
    msToken cookies in Set-Cookie response headers and embedded JSON."""
    print("Scanning docs/probe/*_meta.json for Set-Cookie / msToken ...")
    for meta_path in sorted(PROBE_DIR.glob('*_meta.json')):
        if '_api_' in meta_path.name:
            continue
        meta = json.loads(meta_path.read_text(encoding='utf-8'))
        rh = meta.get('response_headers') or {}
        set_cookie = {k: v for k, v in rh.items() if 'cookie' in k.lower()}
        line = f"  {meta_path.name}: "
        if set_cookie:
            line += json.dumps(set_cookie, ensure_ascii=False)[:600]
        else:
            line += '(no set-cookie headers recorded)'
        print(line)
        for emb in PROBE_DIR.glob(
                meta_path.name.replace('_meta.json', '_embedded_*.json')):
            raw = emb.read_text(encoding='utf-8', errors='replace')
            print(f"    {emb.name}: 'msToken' x{raw.count('msToken')}")
    return 0


if __name__ == '__main__':
    # Usage:
    #   probe_signed_api.py                                -> full 3-step plan
    #   probe_signed_api.py single challenge_item_list <id> <count> <cursor>
    #   probe_signed_api.py single post_item_list <secUid> <count> <cursor> <from_page>
    #   probe_signed_api.py evidence-scan                   -> local scan, 0 requests
    if len(sys.argv) >= 2 and sys.argv[1] == 'evidence-scan':
        sys.exit(scan_evidence())
    if len(sys.argv) >= 3 and sys.argv[1] == 'single':
        _counter = {'n': 0}
        _with_jar = '--cookies' in sys.argv
        _args = [a for a in sys.argv[3:] if not a.startswith('--')]
        sys.exit(run_single(_counter, sys.argv[2], _args, _with_jar))
    sys.exit(main())
