#!/usr/bin/env python3
"""Agent O probe: first real-request calibration against TikTok public pages.

Hard discipline (see task card):
- Total request budget <= 6 (2 page types x max 3 each).
- Min 8s random interval between requests.
- On 403/429/418 or an obvious WAF block page -> STOP immediately, save
  evidence, no retry storm.
- Public pages only, no credentials, no login attempts.
- Full evidence per request: URL, status, headers, first 200KB of body,
  saved under docs/probe/ with timestamps.

Note on WAF markers (learned from the first real response): legit TikTok
pages contain "captcha" only as JS chunk filenames (captcha-ttp.*.js in the
asset manifest), so bare-body matching false-positives. The block-page
heuristic therefore checks: block-ish <title>, "access denied", "security
check", "verify to continue", "px-captcha" element, or a near-empty body
that is not a real page.
"""

import json
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from curl_cffi import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROBE_DIR = PROJECT_ROOT / 'docs' / 'probe'
PROBE_DIR.mkdir(parents=True, exist_ok=True)

PROXY = 'http://127.0.0.1:7892'
IMPERSONATE = 'chrome131'
MAX_ATTEMPTS_PER_TYPE = 3
MIN_INTERVAL = 8.0
MAX_INTERVAL = 15.0
BODY_SAVE_LIMIT = 200 * 1024  # 200KB (hard requirement for raw body evidence)

WAF_MARKERS = [
    'access denied',
    'security check',
    'verify to continue',
    '<div id="px-captcha"',
    'are you a robot',
]

EMBEDDED_JSON_NODES = [
    '__UNIVERSAL_DATA_FOR_REHYDRATION__',
    'SIGI_STATE',
]

TARGETS = [
    {'label': 'tag_webdev', 'url': 'https://www.tiktok.com/tag/webdev'},
    {'label': 'creator_khaby_lame', 'url': 'https://www.tiktok.com/@khaby.lame'},
]

STRUCTURE_MARKERS = [
    '__UNIVERSAL_DATA_FOR_REHYDRATION__',
    'SIGI_STATE',
    'webapp.challenge-detail',
    'webapp.user-detail',
    'challengeInfo',
    'userInfo',
    'ItemModule',
]


def looks_like_waf(body: str, title: str = '') -> bool:
    low_title = (title or '').lower()
    if any(w in low_title for w in ('captcha', 'verify', 'denied', 'blocked')):
        return True
    low = body[:100_000].lower()
    if any(m in low for m in WAF_MARKERS):
        return True
    return False


def extract_embedded_json_evidence(base: str, body_text: str) -> dict:
    """Save the full embedded rehydration JSON as separate evidence files.

    The raw body is truncated to 200KB per the evidence rule, which can cut
    the JSON node; extracting it from the in-memory full response keeps the
    evidence usable without any extra request.
    """
    saved = {}
    for node_id in EMBEDDED_JSON_NODES:
        m = re.search(
            rf'<script[^>]*id="{node_id}"[^>]*>(.*?)</script>',
            body_text, re.S)
        if not m:
            continue
        raw = m.group(1)
        raw_path = PROBE_DIR / f"{base}_embedded_{node_id}.json"
        raw_path.write_text(raw, encoding='utf-8')
        info = {'node_id': node_id, 'raw_length': len(raw), 'file': raw_path.name}
        try:
            data = json.loads(raw)
            info['valid_json'] = True
            info['top_level_keys'] = sorted(data.get('__DEFAULT_SCOPE__', data).keys())
            pretty = PROBE_DIR / f"{base}_embedded_{node_id}_pretty.json"
            pretty.write_text(json.dumps(data, indent=1, ensure_ascii=False),
                              encoding='utf-8')
            info['pretty_file'] = pretty.name
        except json.JSONDecodeError as exc:
            info['valid_json'] = False
            info['json_error'] = str(exc)
        saved[node_id] = info
    return saved


def save_evidence(label: str, url: str, status: int, elapsed_ms: int,
                  headers, body_bytes: bytes, error: str = None) -> Path:
    ts = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    base = f"{ts}_{label}"
    meta = {
        'label': label,
        'url': url,
        'status_code': status,
        'elapsed_ms': elapsed_ms,
        'error': error,
        'captured_at_utc': ts,
        'proxy': PROXY,
        'impersonate': IMPERSONATE,
        'headers_sent_note': 'curl_cffi chrome131 default headers, no cookies, no credentials',
        'response_headers': dict(headers) if headers is not None else None,
        'body_bytes_captured': len(body_bytes),
    }
    meta_path = PROBE_DIR / f"{base}_meta.json"
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding='utf-8')
    body_path = PROBE_DIR / f"{base}_body.html"
    body_path.write_bytes(body_bytes[:BODY_SAVE_LIMIT])
    return body_path


def structural_triage(body_text: str) -> dict:
    return {m: body_text.count(m) for m in STRUCTURE_MARKERS}


def probe_target(target: dict, attempt: int) -> str:
    """One request. Returns 'done' | 'retry' | 'stop'.

    done  = got a usable 200 response (evidence saved; no need to re-hit)
    retry = transient failure (network error / 5xx), may retry in budget
    stop  = block/rate-limit/WAF signal; no further requests at all
    """
    label = f"{target['label']}_try{attempt}"
    url = target['url']
    print(f"\n=== [{label}] GET {url} (proxy={PROXY}) ===")
    session = requests.Session(impersonate=IMPERSONATE, proxies={
        'http': PROXY, 'https': PROXY})
    status, elapsed_ms, headers, body, error = 0, 0, None, b'', None
    t0 = time.monotonic()
    try:
        resp = session.get(
            url,
            headers={
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9',
                'Upgrade-Insecure-Requests': '1',
            },
            timeout=30,
        )
        status = resp.status_code
        headers = resp.headers
        body = resp.content
    except Exception as exc:  # network-level failure
        error = f"{type(exc).__name__}: {exc}"
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    body_path = save_evidence(label, url, status, elapsed_ms, headers, body, error)
    text = body.decode('utf-8', errors='replace')

    if error:
        print(f"  NETWORK ERROR: {error}  ({elapsed_ms}ms)")
        print(f"  evidence: {body_path}")
        # network errors are not WAF blocks; allow retry within budget
        return 'retry'

    print(f"  status={status}  elapsed={elapsed_ms}ms  body={len(body)}B")
    print(f"  evidence: {body_path}")

    if status in (403, 429, 418):
        print(f"  !! HARD STOP: status {status} is a block/rate-limit signal. "
              "No further requests per discipline.")
        return 'stop'

    if 500 <= status < 600:
        print("  5xx server error; treated as transient, may retry within budget.")
        return 'retry'

    if looks_like_waf(text):
        print("  !! HARD STOP: response body matches WAF block-page markers. "
              "No further requests per discipline.")
        return 'stop'

    if status == 200:
        title = re.search(r'<title[^>]*>(.*?)</title>', text, re.S)
        if title:
            print(f"  <title>: {title.group(1).strip()[:120]}")
        embedded = extract_embedded_json_evidence(
            body_path.stem.replace('_body', ''), text)
        print(f"  embedded json nodes: {json.dumps(embedded, ensure_ascii=False)}")
        triage = structural_triage(text)
        print(f"  structure markers (first 200KB): {json.dumps(triage)}")
        return 'done'

    # other 4xx (404 etc.): evidence saved, no point retrying same URL
    print(f"  unexpected status {status}; evidence saved, not retrying.")
    return 'done'


def main():
    request_count = 0
    stopped = False
    for target in TARGETS:
        for attempt in range(1, MAX_ATTEMPTS_PER_TYPE + 1):
            if request_count >= 6:
                print("\nBudget exhausted (6 requests). Stopping.")
                stopped = True
                break
            if request_count > 0:
                wait = random.uniform(MIN_INTERVAL, MAX_INTERVAL)
                print(f"\n-- sleeping {wait:.1f}s before next request --")
                time.sleep(wait)
            request_count += 1
            outcome = probe_target(target, attempt)
            if outcome == 'stop':
                stopped = True
                break
            if outcome == 'done':
                break  # evidence sufficient for this page type
        if stopped:
            break
    print(f"\nDone. total_requests={request_count} stopped_early={stopped}")


if __name__ == '__main__':
    sys.exit(main())
