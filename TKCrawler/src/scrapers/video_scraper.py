"""VideoStreamScraper: product-related TikTok video stream collection.

All request paths go through BaseScraper.fetch (rate limiting, daily quota,
429/403 soft cooldown). No real network calls are made in tests — the
client is injected/mocked.

Endpoints and response shapes are ASSUMPTIONS pending on-device
verification (see docs/reverse/video-api.md and video_selectors.json).

Risk control (task card):
- top-N videos per product only (default limit=10, never full lists)
- 8-12s randomized request interval (env: VIDEO_REQUEST_MIN_INTERVAL /
  VIDEO_REQUEST_MAX_INTERVAL)
- daily quota DAILY_QUOTA_VIDEOS=500, counted per VIDEO (not per product),
  reusing the BaseScraper/RateLimiter quota mechanism
"""

import json
import os
from typing import List, Optional

from ..common.config import config
from ..common.exceptions import (
    QuotaExceededException,
    RateLimitException,
    WAFBlockedException,
)
from ..common.logger import get_logger
from .base_scraper import BaseScraper
from .parsers.video_parser import VIDEO_FIELDS, VideoParser
from .signer import TikTokSigner

logger = get_logger(__name__)


class VideoStreamScraper(BaseScraper):
    """Scrapes the product-related video stream (top-N per product)."""

    RESOURCE_TYPE = 'videos'  # daily quota bucket

    # ------------------------------------------------------------------
    # Env-configurable risk defaults (video-specific, overrides BaseScraper)
    # ------------------------------------------------------------------
    @staticmethod
    def _video_intervals() -> tuple:
        """(min_interval, max_interval) in seconds; env overridable.

        Defaults 8-12s per the Agent E task card.
        """
        min_i = float(os.getenv('VIDEO_REQUEST_MIN_INTERVAL', '8'))
        max_i = float(os.getenv('VIDEO_REQUEST_MAX_INTERVAL', '12'))
        # Degenerate env values must not invert the bounds.
        if max_i < min_i:
            min_i, max_i = max_i, min_i
        return min_i, max_i

    def __init__(self, client=None, signer: TikTokSigner = None,
                 parser: VideoParser = None, snapshot_engine=None,
                 **kwargs):
        """Args mirror BaseScraper; `client`, `signer`, `parser` and
        `snapshot_engine` (SnapshotDiffEngine-compatible, optional) are
        injectable for tests / alternate persistence.
        """
        # Video-specific intervals: explicit kwargs win, else env (8-12s)
        min_default, max_default = self._video_intervals()
        if 'min_interval' not in kwargs:
            kwargs['min_interval'] = min_default
        if 'max_interval' not in kwargs:
            kwargs['max_interval'] = max_default
        super().__init__(client=client, **kwargs)
        self.signer = signer if signer is not None else TikTokSigner()
        self.parser = parser if parser is not None else VideoParser()
        self.snapshot_engine = snapshot_engine

    # ------------------------------------------------------------------
    # URL construction
    # ------------------------------------------------------------------
    def _build_api_url(self, product_id: str, limit: int,
                       ms_token: Optional[str] = None) -> str:
        """Build the (unsigned) product-videos API URL.

        Query composition is an assumption pending capture (task-card
        endpoint; static baseline params from video_selectors.json).
        """
        urls = self.parser.selectors['urls']
        params = self.parser.selectors['api_params']
        base = urls['product_videos_api']['value']
        static_query = params['static_query']['value']
        count_key = params['count_key']['value']
        pid_key = params['product_id_key']['value']
        ms_key = params['ms_token_key']['value']

        parts = [static_query, f"{pid_key}={product_id}",
                 f"{count_key}={limit}"]
        if ms_token:
            parts.append(f"{ms_key}={ms_token}")
        return f"{base}?{'&'.join(parts)}"

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------
    def scrape_product_videos(self, product_id: str, limit: int = 10,
                              ms_token: Optional[str] = None) -> List[dict]:
        """Fetch and parse the top-N videos tied to one product.

        Quota is consumed per video row (daily bucket 'videos', limit
        DAILY_QUOTA_VIDEOS=500); when the quota runs out mid-list, the
        rows fetched so far are returned and the exception stops the loop
        (logged, not propagated) so partial data is preserved.

        Returns:
            List of VIDEO_FIELDS dicts (possibly empty on 404 / empty /
            unparseable responses — logged, never crashes).

        Raises:
            QuotaExceededException: daily video quota already exhausted
                before the first row.
            RateLimitException: still in soft cooldown, or 429 received.
            WAFBlockedException: 403/418/503 received (soft cooldown is
                armed before the exception propagates).
        """
        # Quota gate: refuse the whole call when nothing is left at all.
        self.check_quota(self.RESOURCE_TYPE, config.DAILY_QUOTA_VIDEOS)

        # Stamp product_id onto every parsed row (parser state, simple
        # sequential scraper — no concurrency within one instance).
        self.parser.product_id = str(product_id)

        url = self._build_api_url(product_id, limit, ms_token=ms_token)
        signed_url = self.signer.sign_url(url)
        headers = {'User-Agent': self.signer.user_agent,
                   'X-Bogus': signed_url.split('X-Bogus=')[-1]}
        logger.info(f"Scraping product videos {product_id} "
                    f"(limit={limit}) -> {signed_url}")

        try:
            response = self.fetch(signed_url, headers=headers)
        except (RateLimitException, WAFBlockedException):
            # fetch() already armed the soft cooldown; let it propagate
            raise
        except Exception as exc:
            logger.error(f"Network error scraping videos for "
                         f"{product_id}: {exc}")
            return []

        status = getattr(response, 'status_code', None)
        if status == 404:
            logger.warning(f"Product {product_id} video API returned 404")
            return []
        if status != 200:
            logger.warning(f"Product {product_id} video API unexpected "
                           f"status {status}")
            return []

        raw_items = self._extract_items(response.text, product_id)
        if not raw_items:
            logger.warning(f"No video items parsed for product {product_id}")
            return []

        # Cap to limit defensively and consume quota per row.
        videos: List[dict] = []
        for raw in raw_items[:limit]:
            item = self.parser.parse_video_item(raw)
            if item is None:
                continue
            try:
                self.check_quota(self.RESOURCE_TYPE,
                                 config.DAILY_QUOTA_VIDEOS)
            except QuotaExceededException:
                logger.warning(
                    f"Daily video quota exhausted after {len(videos)} rows; "
                    f"returning partial data for product {product_id}"
                )
                break
            videos.append(item)

        if videos and self.snapshot_engine is not None:
            for video in videos:
                self.snapshot_engine.save_snapshot(video, 'video_snapshots')

        logger.info(f"Product {product_id}: {len(videos)} videos collected")
        return videos

    def _extract_items(self, text: str, product_id: str) -> List[dict]:
        """Parse the response body into raw video item dicts.

        Accepts the assumed API envelope (any configured root candidate).
        Malformed JSON is logged and yields [].
        """
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning(f"Video API response not JSON for {product_id}: "
                           f"{exc}")
            return []
        if not isinstance(data, dict):
            return []
        return self.parser.find_video_roots(data)

    # ------------------------------------------------------------------
    # Page-embedded fallback (no signed API required)
    # ------------------------------------------------------------------
    def scrape_product_videos_from_page(self, product_id: str) -> List[dict]:
        """Fallback path: fetch the product page and pull the video list
        out of the embedded rehydration JSON (same assumption family as
        Agent D's selectors.json; verified: false)."""
        template = self.parser.selectors['urls']['product_page']['value']
        page_url = template.format(product_id=product_id)
        logger.info(f"Scraping product page for videos: {page_url}")

        self.parser.product_id = product_id
        try:
            response = self.fetch(page_url)
        except (RateLimitException, WAFBlockedException):
            raise
        except Exception as exc:
            logger.error(f"Network error fetching product page "
                         f"{product_id}: {exc}")
            return []

        status = getattr(response, 'status_code', None)
        if status != 200:
            logger.warning(f"Product page unexpected status {status}: "
                           f"{page_url}")
            return []
        return self.parser.parse_product_page(response.text)

    # ------------------------------------------------------------------
    # Stats re-parsing (public helper, pure)
    # ------------------------------------------------------------------
    def parse_video_stats(self, video_data: dict) -> dict:
        """Normalize a raw video payload into VIDEO_FIELDS.

        Contract mirrors parser.parse_video_item; product_id defaults to
        the scraper's parser state when absent from video_data.
        """
        pid = video_data.get('product_id') or self.parser.product_id
        original = self.parser.product_id
        self.parser.product_id = pid
        try:
            result = self.parser.parse_video_item(video_data)
        finally:
            self.parser.product_id = original
        return result if result is not None else \
            {field: None for field in VIDEO_FIELDS}
