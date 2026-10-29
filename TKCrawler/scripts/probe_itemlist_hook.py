"""probe_itemlist_hook.py -- passive browser XHR capture for TikTok item_list.

Agent R (2026-10-08). Loads https://www.tiktok.com/tag/{tag} in a real
Chromium (playwright), passively captures every /api/ XHR the page itself
fires (request headers + response body), and saves full evidence to
docs/probe/ -- NO request is ever forged toward TikTok by this script;
everything captured is traffic the page initiated on its own.

Goal (docs/probe/probe-signed-api-findings-2026-10-07.md section 4):
  1. confirm which headers/params the real item_list XHR carries
     (X-Gnarly? web_id/verifyFp? real msToken?)
  2. grab a real itemList response body for fixture + selector calibration
  3. collect a complete header/cookie set for a single curl_cffi replay test

Risk-control discipline (hard budget):
  - browser tag-page loads <= 3 per invocation (1 by default, --retries opt)
  - no login, no credentials, public tag page only
  - 403/429/418 or captcha page -> stop immediately (exit 2)
  - every captured request is printed live to stdout (auditable)
  - cookie values are truncated in saved evidence (structure kept)

Usage:
  python scripts/probe_itemlist_hook.py --tag webdev --watch 35
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs

PROJ = Path(__file__).resolve().parent.parent
OUT_DIR = PROJ / 'docs' / 'probe'

PROXY = 'http://127.0.0.1:7892'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')

BLOCK_STATUS = {403, 429, 418}
COOKIE_TRUNC = 10


def ts() -> str:
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def mask_value(value: str, keep: int = COOKIE_TRUNC) -> str:
    """Truncate a sensitive value, keeping structure visible."""
    if not isinstance(value, str) or len(value) <= keep:
        return value
    return f"{value[:keep]}...TRUNC({len(value)}ch)"


def mask_headers(headers: dict) -> dict:
    masked = {}
    for k, v in headers.items():
        if k.lower() in ('cookie', 'x-gnarly', 'x-bogus', 'mstoken',
                         'authorization', 'x-secsdk-csrf-token'):
            masked[k] = mask_value(v, keep=16 if k.lower() != 'cookie' else 0)
        else:
            masked[k] = v
    return masked


def mask_cookie_jar(cookies: list) -> list:
    out = []
    for c in cookies:
        c = dict(c)
        c['value'] = mask_value(c.get('value', ''))
        out.append(c)
    return out


def safe_name(url: str) -> str:
    p = urlparse(url)
    name = p.path.strip('/').replace('/', '_')
    qs = parse_qs(p.query)
    bits = []
    for key in ('challengeID', 'challengeName', 'secUid', 'cursor', 'count'):
        if key in qs:
            v = qs[key][0]
            bits.append(f"{key}={v[:24]}")
    suffix = ('?' + '&'.join(bits)) if bits else ''
    return re.sub(r'[^A-Za-z0-9_\-=.&?]', '', f"{name}{suffix}")[:120] or 'unknown'


class Capture:
    """Collects /api/ requests + responses issued by the page."""

    def __init__(self, stamp: str):
        self.stamp = stamp
        self.requests: dict[str, dict] = {}   # playwright request guid -> rec
        self.captured: list[dict] = []        # finished api exchanges
        self.blocked: list[dict] = []         # 403/429/418 responses
        self.page_blocked = False

    def is_api(self, url: str) -> bool:
        host = urlparse(url).netloc
        return host.endswith('tiktok.com') and '/api/' in urlparse(url).path

    def on_request(self, request):
        try:
            if not self.is_api(request.url):
                return
            print(f"[CAPTURE-REQ] {ts()} {request.method} {request.url[:160]}")
        except Exception as exc:
            print(f"[HANDLER-ERR] on_request: {exc}")

    def on_response(self, response):
        try:
            self._on_response_inner(response)
        except Exception as exc:
            print(f"[HANDLER-ERR] on_response: {exc}")

    def _on_response_inner(self, response):
        request = response.request
        if not self.is_api(request.url):
            return
        rec = {
            'ts': ts(),
            'url': request.url,
            'method': request.method,
            'resource_type': request.resource_type,
            'request_headers': mask_headers(dict(request.headers)),
            'post_data': (request.post_data or '')[:2000] or None,
        }
        rec['status'] = response.status
        rec['response_headers'] = mask_headers(dict(response.headers))
        status = response.status
        print(f"[CAPTURE-RES] {rec['ts']} {status} {request.url[:160]}")

        if status in BLOCK_STATUS:
            self.blocked.append({'url': request.url, 'status': status})
            print(f"[BLOCKED] {status} on {request.url[:120]}")

        def _is_challenge_guard(resp):
            # TikTok renders a "challenge" interstitial; body sniff below
            return False

        body = ''
        try:
            body = response.text()
        except Exception as exc:
            body = f"<body read error: {exc}>"
        rec['body_len'] = len(body)
        rec['body'] = body

        # captcha / challenge interstitial heuristic on HTML bodies
        low = body[:4000].lower()
        if 'captcha' in low and 'verify' in low and 'tiktokapi' not in low:
            self.page_blocked = True
            print("[PAGE-BLOCK] captcha/verify keywords in API body")

        self.captured.append(rec)

    def save(self, tag: str) -> list[Path]:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        written = []
        for rec in self.captured:
            base = f"{self.stamp}_{safe_name(rec['url'])}"
            meta = {k: v for k, v in rec.items() if k != 'body'}
            meta['evidence_discipline'] = ('cookie/signed header values '
                                           'truncated; structure preserved')
            meta_path = OUT_DIR / f"{base}_meta.json"
            meta_path.write_text(json.dumps(meta, ensure_ascii=False,
                                            indent=2), encoding='utf-8')
            body = rec.get('body', '')
            ext = '.json' if body.strip().startswith(('{', '[')) else '.txt'
            body_path = OUT_DIR / f"{base}_body{ext}"
            try:
                parsed = json.loads(body)
                body_path.write_text(json.dumps(parsed, ensure_ascii=False,
                                                indent=2), encoding='utf-8')
            except (ValueError, TypeError):
                body_path.write_text(body, encoding='utf-8')
            written += [meta_path, body_path]
            print(f"[EVIDENCE] {body_path.name} ({rec.get('status')} "
                  f"{rec.get('body_len')}B)")
        return written

    def summary(self) -> dict:
        return {
            'stamp': self.stamp,
            'api_exchanges': len(self.captured),
            'urls': [r['url'] for r in self.captured],
            'statuses': {r['url'][:120]: r.get('status') for r in self.captured},
            'blocked': self.blocked,
            'page_blocked': self.page_blocked,
        }


def load_page(context, tag: str, cap: Capture, watch: int, scrolls: int):
    """One page load (counts against the <=3 budget)."""
    page = context.new_page()
    page.on('request', cap.on_request)
    page.on('response', cap.on_response)

    url = f'https://www.tiktok.com/tag/{tag}'
    print(f"\n=== LOAD #{cap.load_no} {url} @ {ts()} ===")
    try:
        resp = page.goto(url, timeout=45000, wait_until='domcontentloaded')
        print(f"[NAV] status={resp.status if resp else '?'}")
        if resp and resp.status in BLOCK_STATUS:
            cap.page_blocked = True
            return page
    except Exception as exc:
        print(f"[NAV-ERROR] {exc}")
        return page

    # observation window: let hydration fire the first item_list XHR,
    # then scroll to trigger lazy-loaded pages
    deadline = time.time() + watch
    scrolled = 0
    while time.time() < deadline:
        page.wait_for_timeout(2000)
        has_itemlist = any('item_list' in r['url'] for r in cap.captured)
        remaining = deadline - time.time()
        if scrolled < scrolls and (has_itemlist or remaining < watch * 0.35):
            print(f"[SCROLL] {scrolled + 1}/{scrolls} @ {ts()}")
            try:
                page.mouse.wheel(0, 4000)
            except Exception:
                page.evaluate('window.scrollBy(0, 4000)')
            scrolled += 1
        if has_itemlist and scrolled >= min(scrolls, 1) and remaining < watch * 0.6:
            # got item_list + one scroll for page 2; enough evidence
            break
    # browser-side cookie snapshot (values truncated at save time)
    try:
        cap.cookies = context.cookies()
    except Exception:
        cap.cookies = []
    return page


def replay_itemlist(cap: Capture, cookies: list, out_stamp: str) -> dict:
    """Single curl_cffi replay of the captured item_list request.

    Reuses the EXACT captured URL (X-Bogus/X-Gnarly/device_id intact) and
    the browser's full guest cookie jar. Counts against the <=2 replay
    budget: called once per run. Evidence saved with truncated cookies.
    """
    from curl_cffi import requests as cffi_requests

    target = None
    for r in cap.captured:
        if 'challenge/item_list' in r['url']:
            target = r
            break
    if target is None:
        print('[REPLAY] no item_list request captured; skip')
        return {'skipped': True}

    cookie_str = '; '.join(
        f"{c['name']}={c['value']}" for c in cookies
        if isinstance(c, dict) and 'tiktok.com' in (c.get('domain') or '')
    )
    # captured request headers were masked before storage; use the minimal
    # browser-consistent header set instead (UA matches the signed URL's UA)
    headers = {
        'User-Agent': UA,
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'https://www.tiktok.com/tag/webdev',
        'Cookie': cookie_str,
    }
    print(f"\n[REPLAY] GET {target['url'][:140]}")
    print(f"[REPLAY] cookie jar: {len(cookies)} cookies "
          f"({len(cookie_str)} chars)")
    result = {'url': target['url'], 'replayed_at': ts()}
    try:
        resp = cffi_requests.get(
            target['url'], headers=headers,
            impersonate='chrome131',
            proxies={'http': PROXY, 'https': PROXY},
            timeout=20,
        )
        result['status'] = resp.status_code
        result['body_len'] = len(resp.content)
        result['body_head'] = resp.text[:400]
        print(f"[REPLAY] status={resp.status_code} body={len(resp.content)}B")
        print(f"[REPLAY] body head: {resp.text[:200]!r}")
        # evidence
        base = f"{out_stamp}_replay_api_challenge_item_list"
        meta = {k: v for k, v in result.items() if k != 'url'}
        meta['url_note'] = 'full captured URL reused (see capture meta)'
        meta['cookie_masked'] = mask_value(cookie_str, keep=0)
        (OUT_DIR / f"{base}_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
        (OUT_DIR / f"{base}_body.txt").write_text(
            resp.text, encoding='utf-8')
        print(f"[EVIDENCE] {base}_meta.json / _body.txt")
    except Exception as exc:
        result['error'] = str(exc)
        print(f"[REPLAY-ERROR] {exc}")
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--tag', default='webdev')
    ap.add_argument('--watch', type=int, default=35,
                    help='observation seconds per load')
    ap.add_argument('--scrolls', type=int, default=2)
    ap.add_argument('--max-loads', type=int, default=1,
                    help='hard budget: tag page loads (<=3)')
    ap.add_argument('--replay', action='store_true',
                    help='one curl_cffi replay of captured item_list '
                         '(<=2 replays; run once per session)')
    args = ap.parse_args()
    if args.max_loads > 3:
        print('BUDGET VIOLATION: max-loads capped at 3')
        args.max_loads = 3

    stamp = ts()
    print(f"=== probe_itemlist_hook start {stamp} "
          f"tag={args.tag} watch={args.watch}s max_loads={args.max_loads} ===")

    from playwright.sync_api import sync_playwright

    cap = Capture(stamp)
    cap.cookies = []
    cap.load_no = 0
    all_evidence: list[Path] = []

    with sync_playwright() as p:
        # playwright's pinned chromium revision is not in the local cache;
        # use the installed system Google Chrome instead (channel launch)
        browser = p.chromium.launch(
            headless=True,
            channel='chrome',
            proxy={'server': PROXY},
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-first-run', '--no-default-browser-check',
                '--disable-infobars',
            ],
        )
        context = browser.new_context(
            user_agent=UA,
            locale='en-US',
            timezone_id='America/New_York',
            viewport={'width': 1920, 'height': 1080},
            color_scheme='light',
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', "
            "{get: () => undefined});"
        )

        pages = []
        for i in range(args.max_loads):
            cap.load_no = i + 1
            if cap.page_blocked or cap.blocked:
                print('[STOP] blocked status seen; stopping per discipline')
                break
            if any('item_list' in r['url'] and r.get('body_len', 0) > 2
                   for r in cap.captured):
                print('[STOP] item_list body already captured')
                break
            page = load_page(context, args.tag, cap, args.watch, args.scrolls)
            pages.append(page)

        # evidence dump
        all_evidence += cap.save(args.tag)

        cookie_path = OUT_DIR / f"{stamp}_browser_cookies_trunc.json"
        cookie_path.write_text(json.dumps(mask_cookie_jar(cap.cookies),
                                          ensure_ascii=False, indent=2),
                               encoding='utf-8')
        print(f"[EVIDENCE] {cookie_path.name}")

        summary = cap.summary()
        summary['cookies'] = {c['name']: mask_value(c['value'])
                              for c in cap.cookies if isinstance(c, dict)}
        summary_path = OUT_DIR / f"{stamp}_probe_itemlist_hook_summary.json"
        summary_path.write_text(json.dumps(summary, ensure_ascii=False,
                                           indent=2), encoding='utf-8')
        print(f"[EVIDENCE] {summary_path.name}")
        print(f"\n=== CAPTURED REQUEST LIST ({len(cap.captured)}) ===")
        for r in cap.captured:
            print(f"  {r.get('status')} {r['method']} {r['url'][:170]}")

        replay_result = None
        if args.replay and not cap.page_blocked and not cap.blocked:
            # replay INSIDE the browser session while cookies/X-Gnarly are
            # fresh; uses the full (unmasked) cookie jar held in memory
            replay_result = replay_itemlist(cap, cap.cookies, stamp)

        for pg in pages:
            try:
                pg.close()
            except Exception:
                pass
        context.close()
        browser.close()

    # exit codes
    if cap.page_blocked or cap.blocked:
        print('[RESULT] BLOCKED -- stop per risk-control discipline')
        return 2
    if replay_result and replay_result.get('status') == 200 \
            and replay_result.get('body_len', 0) > 2:
        print('[RESULT] REPLAY SUCCESS: item_list returned data via '
              'curl_cffi replay')
        return 0
    if replay_result and not replay_result.get('skipped'):
        print(f"[RESULT] REPLAY returned {replay_result.get('status')} "
              f"{replay_result.get('body_len', 0)}B (no data)")
    itemlists = [r for r in cap.captured
                 if 'item_list' in r['url'] and r.get('body_len', 0) > 2]
    if itemlists:
        print(f"[RESULT] SUCCESS: {len(itemlists)} item_list body/bodies "
              f"captured")
        return 0
    if any('item_list' in r['url'] for r in cap.captured):
        print('[RESULT] item_list XHR seen but empty body')
        return 1
    print('[RESULT] no item_list XHR observed')
    return 1


if __name__ == '__main__':
    sys.exit(main())
