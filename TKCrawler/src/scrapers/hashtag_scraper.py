"""HashtagTrendScraper: TikTok hashtag (topic) page scraper.

All request paths go through BaseScraper.fetch (rate limiting, daily quota,
429/403 soft cooldown). No real network calls are made in tests — the
client is injected/mocked.

Reverse-analysis basis: docs/reverse/hashtag-api.md (all endpoints/selectors
are assumptions marked verified: false pending live calibration).

Data contract:
- scrape_hashtag() returns {tag_name, views_total, snapshot_date} for
  SnapshotDiffEngine.save_snapshot(data, table='hashtag_snapshots').
- views_growth_7d is NOT computed here: it is derived by
  SnapshotDiffEngine.calculate_growth from two snapshots.
"""

from datetime import date
import random
import string
from typing import List, Optional
from urllib.parse import quote, urlencode

from ..common.config import config
from ..common.exceptions import RateLimitException, WAFBlockedException
from ..common.logger import get_logger
from .base_scraper import BaseScraper
from .parsers.hashtag_parser import HASHTAG_FIELDS, HashtagParser
from .signer import TikTokSigner

logger = get_logger(__name__)

# Baseline query params for the signed challenge/detail API, calibrated by
# Agent P's live probe (2026-10-07, scripts/probe_signed_api.py). The
# request was accepted with this exact shape; a stripped-down param set is
# UNVERIFIED.
API_BASE_PARAMS = {
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
    'region': 'US',
    'screen_height': '1080',
    'screen_width': '1920',
    'tz_name': 'America/New_York',
    'webcast_language': 'en',
}


def _guest_ms_token(length: int = 116) -> str:
    """Guest-mode dummy msToken (no credential).

    Agent P verified the challenge/detail endpoint accepts a random guest
    token in both query and Cookie (docs/reverse/hashtag-api.md E-8).
    """
    alphabet = string.ascii_letters + string.digits + '-_'
    return ''.join(random.choice(alphabet) for _ in range(length))


class HashtagTrendScraper(BaseScraper):
    """Scrapes TikTok hashtag pages: total views snapshot + top videos."""

    RESOURCE_TYPE = 'hashtags'  # daily quota bucket

    def __init__(self, client=None, **kwargs):
        """Args mirror BaseScraper; `client` is injectable for tests."""
        super().__init__(client=client, **kwargs)
        self.parser = HashtagParser()

    # ------------------------------------------------------------------
    # URL building
    # ------------------------------------------------------------------
    def _tag_url(self, tag_name: str) -> str:
        """Build the hashtag page URL from the (assumed) URL template."""
        template = self.parser.selectors['urls']['tag_page']['value']
        clean = tag_name.strip().lstrip('#')
        return template.format(tag_name=quote(clean, safe=''))

    # ------------------------------------------------------------------
    # Views snapshot
    # ------------------------------------------------------------------
    def scrape_hashtag(self, tag_name: str) -> Optional[dict]:
        """Fetch and parse one hashtag page into the snapshot dict.

        Returns:
            dict {tag_name, views_total, snapshot_date} where tag_name is
            the normalized input (no '#'), views_total is an int (0 when
            the page shows no count) and snapshot_date is today's ISO
            date string. None when the page is empty / 404 / no embedded
            data (logged, never crashes).

        Raises:
            QuotaExceededException: daily hashtag quota exhausted.
            RateLimitException: still in soft cooldown, or 429 received.
            WAFBlockedException: 403/418/503 received (soft cooldown is
                armed before the exception propagates).
        """
        # Daily quota gate (defaults to DAILY_QUOTA_PRODUCTS via BaseScraper)
        self.check_quota(self.RESOURCE_TYPE, config.DAILY_QUOTA_PRODUCTS)

        url = self._tag_url(tag_name)
        logger.info(f"Scraping hashtag {tag_name} -> {url}")

        try:
            response = self.fetch(url)
        except (RateLimitException, WAFBlockedException):
            # fetch() already armed the soft cooldown; let it propagate
            raise
        except Exception as exc:
            # Network-level errors are logged and surfaced as None so batch
            # jobs keep running.
            logger.error(f"Network error scraping hashtag {tag_name}: {exc}")
            return None

        status = getattr(response, 'status_code', None)
        if status == 404:
            logger.warning(f"Hashtag {tag_name} returned 404, skipping")
            return None
        if status != 200:
            logger.warning(
                f"Hashtag {tag_name} returned unexpected status {status}"
            )
            return None

        data = self.parser.parse_hashtag(response.text)
        if data is None:
            logger.warning(
                f"Hashtag {tag_name}: no parseable embedded data "
                f"(empty/blocked page)"
            )
            return None

        # Input tag_name is the snapshot identity key and always wins
        # (same policy as product_id for snapshots)
        data['tag_name'] = str(tag_name).strip().lstrip('#')

        result = {field: data.get(field) for field in HASHTAG_FIELDS}
        result['snapshot_date'] = date.today().isoformat()
        logger.info(
            f"Hashtag {result['tag_name']}: views_total={result['views_total']}"
        )
        return result

    # ------------------------------------------------------------------
    # Signed-API channel (live-verified 2026-10-07, see E-8)
    # ------------------------------------------------------------------
    def scrape_hashtag_api(self, tag_name: str) -> Optional[dict]:
        """Fetch hashtag totals via the signed challenge/detail API.

        This is the ONLY live-verified hashtag data channel (guest SSR
        /tag/{tag} is an empty shell -- docs/probe/probe-findings-2026-10-07.md
        §3.3). Request shape matches the verified probe exactly:
        TikTokSigner X-Bogus appended to the query (UA-bound), guest
        dummy msToken in both query and Cookie, chrome131 UA.

        Returns:
            Same contract as scrape_hashtag: {tag_name, views_total,
            snapshot_date}. None on 404/unexpected status/unparseable
            payload (logged, never crashes).

        Raises:
            QuotaExceededException / RateLimitException /
            WAFBlockedException: same contract as scrape_hashtag.
        """
        self.check_quota(self.RESOURCE_TYPE, config.DAILY_QUOTA_PRODUCTS)

        clean = str(tag_name).strip().lstrip('#')
        template = self.parser.selectors['urls']['api_challenge_detail']['value']
        url = template.format(tag_name=quote(clean, safe=''))

        ms_token = _guest_ms_token()
        params = dict(API_BASE_PARAMS)
        params['referer'] = f'https://www.tiktok.com/tag/{clean}'
        params['msToken'] = ms_token
        url = f"{url}&{urlencode(params)}"

        signer = TikTokSigner()
        signed_url = signer.sign_url(url)
        headers = signer.headers_for(url)
        headers.update({
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': f'https://www.tiktok.com/tag/{clean}',
            'Cookie': f'msToken={ms_token}',
        })
        logger.info(f"Scraping hashtag via signed API {clean} -> "
                    f"{template.split('?')[0]}")
        logger.debug(f"signed API url: {signed_url}")

        try:
            response = self.fetch(signed_url, headers=headers)
        except (RateLimitException, WAFBlockedException):
            raise
        except Exception as exc:
            logger.error(f"Network error on signed API for {clean}: {exc}")
            return None

        status = getattr(response, 'status_code', None)
        if status == 404:
            logger.warning(f"Signed API for {clean} returned 404, skipping")
            return None
        if status != 200:
            logger.warning(
                f"Signed API for {clean} returned unexpected status {status}"
            )
            return None

        data = self.parser.parse_hashtag_api(response.text)
        if data is None:
            logger.warning(
                f"Signed API for {clean}: no parseable challenge data "
                f"(empty body / gated / non-zero statusCode)"
            )
            return None

        data['tag_name'] = clean
        result = {field: data.get(field) for field in HASHTAG_FIELDS}
        result['snapshot_date'] = date.today().isoformat()
        logger.info(
            f"Hashtag {result['tag_name']} (API): "
            f"views_total={result['views_total']}"
        )
        return result

    # ------------------------------------------------------------------
    # Top videos
    # ------------------------------------------------------------------
    def get_top_videos(self, tag_name: str, limit: int = 30) -> List[str]:
        """Return the top video ID list for a hashtag (first page only).

        Note:
            Initial version extracts IDs from the hashtag page's embedded
            JSON (first screen). The paginated item_list API requires an
            unverified signature chain (msToken/X-Bogus) and is NOT used —
            see docs/reverse/hashtag-api.md E-5. If the first screen
            carries fewer than `limit` videos, fewer are returned.

        Returns:
            De-duplicated video ID strings (page order kept, capped at
            `limit`). Empty list when the page is empty / 404 / no video
            links (logged, never crashes).

        Raises:
            RateLimitException / WAFBlockedException: same contract as
                scrape_hashtag.
        """
        # Daily quota gate
        self.check_quota(self.RESOURCE_TYPE, config.DAILY_QUOTA_PRODUCTS)

        url = self._tag_url(tag_name)
        logger.info(f"Scraping top videos for hashtag {tag_name} -> {url}")

        try:
            response = self.fetch(url)
        except (RateLimitException, WAFBlockedException):
            raise
        except Exception as exc:
            logger.error(
                f"Network error scraping hashtag videos {tag_name}: {exc}"
            )
            return []

        status = getattr(response, 'status_code', None)
        if status == 404:
            logger.warning(f"Hashtag {tag_name} returned 404, skipping")
            return []
        if status != 200:
            logger.warning(
                f"Hashtag {tag_name} returned unexpected status {status}"
            )
            return []

        video_ids = self.parser.get_top_video_ids(response.text, limit=limit)
        if not video_ids:
            logger.warning(f"No top videos found for hashtag {tag_name}")
        else:
            logger.info(
                f"Hashtag {tag_name}: {len(video_ids)} top videos "
                f"(limit={limit})"
            )
        return video_ids
