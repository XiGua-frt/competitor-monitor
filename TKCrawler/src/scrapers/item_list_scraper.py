"""ItemListScraper: TikTok user post list via the signed
/api/post/item_list/ API.

Request shape is LIVE-VERIFIED (docs/probe/itemlist_retest_2026-10-08.md,
verified: true): login-state cookies (SessionManager, data/session.json)
+ TikTokSigner X-Bogus appended to the query with a same-value header
copy + the REAL msToken from the session cookies (both query and Cookie).
X-Gnarly / X-Dynosaur / device_id / web_id are NOT required in login
state (verified by omission in the probe). Stripping any verified param
from the baseline set is UNVERIFIED and must not be done silently.

Untested: pagination — the probe account had a single video, so only the
hasMore=false / cursor="-1" path was observed live. The cursor/hasMore
loop in scrape_user_videos implements the documented response contract
and is marked "untested: pagination" until a >15-video account is probed.

Output contract (snake_case, aligned with HashtagTrendScraper /
VIDEO_FIELDS style):
- fetch_page()        -> {videos: [...], has_more: bool, cursor: int}
- scrape_user_videos()-> List[ITEM_LIST_FIELDS dicts]
  where each video row carries: video_id, author_id, sec_uid,
  unique_id, desc, create_time, play_count, digg_count,
  collect_count, comment_count, share_count.

NOTE author_id (2026-10-08): video_snapshots.author_id is varchar(50)
and the 76-char sec_uid does not fit (E2E: docs/probe/
e2e_itemlist_2026-10-08.md), so the stored value is the NUMERIC
author.id (TikTok itemList[i].author.id, ~19 chars as a string) —
consistent with creator_snapshots.creator_id varchar(50).
"""

import json
from typing import List, Optional
from urllib.parse import urlencode

from ..anti_detection import SessionManager
from ..anti_detection.session_manager import session_manager as _default_session_manager
from ..common.config import config
from ..common.exceptions import (
    QuotaExceededException,
    RateLimitException,
    SessionExpiredException,
    WAFBlockedException,
)
from ..common.logger import get_logger
from .base_scraper import BaseScraper
from .signer import TikTokSigner

logger = get_logger(__name__)

# Live-verified endpoint (docs/probe/itemlist_retest_2026-10-08.md).
ITEM_LIST_API_URL = 'https://www.tiktok.com/api/post/item_list/'

# Baseline query params accepted by the live probe (2026-10-08). Identical
# to the signed challenge/detail baseline in hashtag_scraper.py except
# from_page=user; the probe signed_url carried exactly this shape plus
# secUid/count/cursor/sourceType/coverFormat/msToken/referer and the
# needPinnedItemIds/withQABis extras. Do not trim params without a probe.
ITEM_LIST_BASE_PARAMS = {
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
    'from_page': 'user',
    'os': 'windows',
    'priority_region': '',
    'region': 'US',
    'screen_height': '1080',
    'screen_width': '1920',
    'tz_name': 'America/New_York',
    'webcast_language': 'en',
}

# Output fields per video row (snake_case, matches VIDEO_FIELDS style).
ITEM_LIST_FIELDS = (
    'video_id',
    'author_id',
    'sec_uid',
    'unique_id',
    'desc',
    'create_time',
    'play_count',
    'digg_count',
    'collect_count',
    'comment_count',
    'share_count',
)


class ItemListScraper(BaseScraper):
    """Scrapes a user's post list through the signed item_list API
    (login-state channel, verified 2026-10-08)."""

    RESOURCE_TYPE = 'videos'  # daily quota bucket (DAILY_QUOTA_VIDEOS)

    def __init__(self, client=None, signer: TikTokSigner = None,
                 session_manager: SessionManager = None, **kwargs):
        """Args mirror BaseScraper; `client`, `signer` and
        `session_manager` are injectable for tests. The session manager
        provides the login-state cookies (the verified gate for this
        API)."""
        super().__init__(client=client, **kwargs)
        self.signer = signer if signer is not None else TikTokSigner()
        self.session = (session_manager if session_manager is not None
                        else _default_session_manager)

    # ------------------------------------------------------------------
    # URL building (verified request shape)
    # ------------------------------------------------------------------
    def _build_url(self, sec_uid: str, count: int = 15, cursor: int = 0,
                   unique_id: str = None, ms_token: str = '') -> str:
        """Build the (unsigned) item_list URL.

        Args:
            sec_uid: target user's secUid (the verified identity key).
            count: page size (probe used 15).
            cursor: page offset (first page 0; response cursor drives
                pagination — untested: pagination).
            unique_id: optional handle, used only for the Referer header
                value embedded in the query (probe sent @<handle>).
            ms_token: REAL msToken from the session cookies (the probe
                sent the cookie value, not a dummy).
        """
        referer = (f'https://www.tiktok.com/@{unique_id}' if unique_id
                   else 'https://www.tiktok.com/')
        params = dict(ITEM_LIST_BASE_PARAMS)
        params.update({
            'referer': referer,
            'msToken': ms_token,
            'secUid': sec_uid,
            'count': str(count),
            'cursor': str(cursor),
            'sourceType': '8',
            'coverFormat': '2',
            'needPinnedItemIds': 'false',
            'withQABis': 'true',
        })
        return f"{ITEM_LIST_API_URL}?{urlencode(params)}"

    @staticmethod
    def _cookie_header(cookies: dict) -> str:
        """Serialize the full login-state cookie jar (probe sent all 27)."""
        return '; '.join(f'{k}={v}' for k, v in cookies.items())

    # ------------------------------------------------------------------
    # Response parsing (structure verified against the probe response)
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_item(item: dict) -> dict:
        """Map one itemList entry to the flat ITEM_LIST_FIELDS row."""
        stats = item.get('stats') or {}
        author = item.get('author') or {}
        return {
            'video_id': item.get('id'),
            # Numeric author.id as a string (~19 chars, fits varchar(50));
            # NOT sec_uid (76 chars). None when author is missing.
            'author_id': (str(author['id'])
                          if author.get('id') is not None else None),
            'sec_uid': author.get('secUid'),
            'unique_id': author.get('uniqueId'),
            'desc': item.get('desc'),
            'create_time': item.get('createTime'),
            'play_count': stats.get('playCount', 0),
            'digg_count': stats.get('diggCount', 0),
            'collect_count': stats.get('collectCount', 0),
            'comment_count': stats.get('commentCount', 0),
            'share_count': stats.get('shareCount', 0),
        }

    @staticmethod
    def _normalize_cursor(raw, fallback: int) -> int:
        """Coerce the response cursor (string in the probe) to int; fall
        back to offset arithmetic when missing/garbled."""
        try:
            return int(raw)
        except (TypeError, ValueError):
            return fallback

    # ------------------------------------------------------------------
    # Single page
    # ------------------------------------------------------------------
    def fetch_page(self, sec_uid: str, cursor: int = 0, count: int = 15,
                   unique_id: str = None) -> Optional[dict]:
        """Fetch one item_list page for a user.

        Returns:
            {videos: List[ITEM_LIST_FIELDS dict], has_more: bool,
             cursor: int} where cursor is the server-returned next-page
            cursor (int-coerced; "−1" stays -1). None on 404 / unexpected
            HTTP status / unparseable JSON / non-zero statusCode /
            network error (logged, never crashes).

        Raises:
            SessionExpiredException: no session cookies, session expired
                locally, or msToken missing from the cookie jar.
            QuotaExceededException: daily videos quota exhausted.
            RateLimitException: still in soft cooldown, or 429 received.
            WAFBlockedException: 403/418/503 received (soft cooldown is
                armed before the exception propagates).
        """
        # Daily quota gate (one unit per page request)
        self.check_quota(self.RESOURCE_TYPE, config.DAILY_QUOTA_VIDEOS)

        # Login-state cookies are the verified gate for this endpoint.
        # get_cookies() raises SessionExpiredException when empty/expired.
        cookies = self.session.get_cookies()
        ms_token = cookies.get('msToken')
        if not ms_token:
            raise SessionExpiredException(
                "msToken missing from session cookies; item_list requires "
                "the real login msToken (probe-verified shape)"
            )

        url = self._build_url(sec_uid, count=count, cursor=cursor,
                              unique_id=unique_id, ms_token=ms_token)
        referer = (f'https://www.tiktok.com/@{unique_id}' if unique_id
                   else 'https://www.tiktok.com/')

        # X-Bogus: query-embedded (canonical) + same-value header copy,
        # exactly like the probe / hashtag signed-API channel.
        signed_url = self.signer.sign_url(url)
        headers = self.signer.headers_for(url)
        headers.update({
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': referer,
            'Cookie': self._cookie_header(cookies),
        })
        logger.info(
            f"Fetching item_list page sec_uid={sec_uid[:16]}... "
            f"cursor={cursor} count={count}"
        )
        logger.debug(f"signed item_list url: {signed_url}")

        try:
            response = self.fetch(signed_url, headers=headers)
        except (RateLimitException, WAFBlockedException):
            # fetch() already armed the soft cooldown; let it propagate
            raise
        except Exception as exc:
            # Network-level errors are logged and surfaced as None so
            # batch jobs keep running.
            logger.error(f"Network error on item_list sec_uid={sec_uid}: "
                         f"{exc}")
            return None

        status = getattr(response, 'status_code', None)
        if status == 404:
            logger.warning(f"item_list returned 404 for {sec_uid}, skipping")
            return None
        if status != 200:
            logger.warning(
                f"item_list returned unexpected status {status} for "
                f"{sec_uid}"
            )
            return None

        try:
            data = json.loads(response.text)
        except (ValueError, TypeError) as exc:
            logger.warning(f"item_list returned non-JSON body for "
                           f"{sec_uid}: {exc}")
            return None

        # Response is camelCase (statusCode) with a snake_case mirror
        # (status_code); both were 0 in the verified probe.
        status_code = data.get('statusCode', data.get('status_code', 0))
        if status_code != 0:
            logger.warning(
                f"item_list non-zero statusCode={status_code} for "
                f"{sec_uid} (gated / blocked)"
            )
            return None

        items = data.get('itemList') or []
        videos = [self._parse_item(item) for item in items]
        has_more = bool(data.get('hasMore', False))
        next_cursor = self._normalize_cursor(
            data.get('cursor'), fallback=cursor + count
        )
        logger.info(
            f"item_list {sec_uid[:16]}... cursor={cursor}: "
            f"{len(videos)} videos, has_more={has_more}, "
            f"next_cursor={next_cursor}"
        )
        return {'videos': videos, 'has_more': has_more,
                'cursor': next_cursor}

    # ------------------------------------------------------------------
    # Paginated entry (untested: pagination — no >15-video account probed)
    # ------------------------------------------------------------------
    def scrape_user_videos(self, sec_uid: str, unique_id: str = None,
                           count: int = 15, max_pages: int = 1
                           ) -> Optional[List[dict]]:
        """Fetch up to `max_pages` item_list pages for one user.

        Pagination follows the response contract: keep requesting with
        the server cursor while hasMore is true. Because the loop was
        never exercised live (probe account had 1 video), guards stop it
        on a non-advancing cursor to avoid spinning.

        Returns:
            Flat list of ITEM_LIST_FIELDS rows across pages. None only
            when the FIRST page fails (404 / non-zero statusCode /
            network error / unparseable body); a later-page failure
            keeps the rows collected so far (partial data preserved,
            same policy as scrape_product_videos).

        Raises:
            SessionExpiredException / RateLimitException /
            WAFBlockedException: same contract as fetch_page.
            QuotaExceededException: exhausted before the first page
                (mid-loop exhaustion keeps partial data, logged).
        """
        videos: List[dict] = []
        cursor = 0
        for page in range(max_pages):
            try:
                result = self.fetch_page(sec_uid, cursor=cursor,
                                         count=count, unique_id=unique_id)
            except QuotaExceededException:
                if videos:
                    logger.warning(
                        f"item_list quota exhausted mid-list for "
                        f"{sec_uid}; keeping {len(videos)} rows"
                    )
                    return videos
                raise
            if result is None:
                if videos:
                    logger.warning(
                        f"item_list page failed after {len(videos)} rows "
                        f"for {sec_uid}; keeping partial data"
                    )
                    return videos
                return None

            videos.extend(result['videos'])
            if not result['has_more']:
                break
            next_cursor = result['cursor']
            if next_cursor == cursor:
                # Guard against a stale/echoed cursor spinning forever.
                # NOTE verified 2026-10-08 (E2E @khaby.lame): item_list cursor is
                # createTime-based and DECREASES across pages (newest first), so
                # the old `next_cursor <= cursor` guard wrongly stopped page 2.
                # Only an identical (echoed) cursor means no progress.
                logger.warning(
                    f"item_list cursor echoed unchanged "
                    f"({cursor}); stopping pagination"
                )
                break
            cursor = next_cursor
        return videos
