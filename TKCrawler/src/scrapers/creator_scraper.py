"""CreatorProfileScraper: TikTok creator (user) profile + showcase scraper.

All request paths go through BaseScraper.fetch (rate limiting, daily quota,
429/403 soft cooldown). No real network calls are made in tests — the
AntiDetectionClient is injected/mocked.

Tier definitions (classify_tier, boundary semantics: lower bound inclusive):
    nano:  follower_count <  10_000          (< 1万)
    micro: 10_000 <= follower_count < 100_000   (1万-10万, i.e. 腰部)
    mid:   100_000 <= follower_count < 500_000  (10万-50万)
    macro: follower_count >= 500_000         (>= 50万)
Negative / zero counts classify as nano (treated as "no audience").
"""

import re
from typing import Iterable, List, Optional

from ..common.config import config
from ..common.exceptions import RateLimitException, WAFBlockedException
from ..common.logger import get_logger
from .base_scraper import BaseScraper
from .parsers.creator_parser import CREATOR_FIELDS, CreatorParser

logger = get_logger(__name__)

# Tier boundaries (followers); documented in classify_tier docstring and
# docs/reverse/creator-api.md
TIER_BOUNDARIES = {
    'nano': (0, 10_000),
    'micro': (10_000, 100_000),
    'mid': (100_000, 500_000),
    'macro': (500_000, None),
}


class CreatorProfileScraper(BaseScraper):
    """Scrapes TikTok creator profiles and showcase product lists."""

    RESOURCE_TYPE = 'creators'  # daily quota bucket (DAILY_QUOTA_CREATORS)

    def __init__(self, client=None, **kwargs):
        """Args mirror BaseScraper; `client` is injectable for tests."""
        super().__init__(client=client, **kwargs)
        self.parser = CreatorParser()

    # ------------------------------------------------------------------
    # URL helpers (templates are ASSUMPTIONS in creator_selectors.json)
    # ------------------------------------------------------------------
    def _profile_url(self, creator_id: str) -> str:
        template = self.parser.selectors['urls']['profile_page']['value']
        return template.format(unique_id=creator_id)

    def _showcase_url(self, creator_id: str) -> str:
        template = self.parser.selectors['urls']['showcase_page']['value']
        return template.format(unique_id=creator_id)

    # ------------------------------------------------------------------
    # Tier classification & mid-tier (腰部) filtering — pure logic
    # ------------------------------------------------------------------
    @staticmethod
    def classify_tier(follower_count: int) -> str:
        """Classify a creator by follower count.

        Boundary semantics (lower bound inclusive, upper exclusive):
            'nano'  : follower_count <  10_000    (< 1万粉丝)
            'micro' : 10_000   <= follower_count < 100_000   (1万-10万 = 腰部)
            'mid'   : 100_000  <= follower_count < 500_000   (10万-50万)
            'macro' : follower_count >= 500_000  (>= 50万)

        follower_count <= 0 (or None) -> 'nano'. Negative input never raises.
        Note: the task card calls the 1-10万 band "腰部" and the tier enum
        calls it 'micro' — same band, two names (see docs/reverse/creator-api.md).
        """
        if follower_count is None or follower_count < TIER_BOUNDARIES['micro'][0]:
            return 'nano'
        if follower_count < TIER_BOUNDARIES['mid'][0]:
            return 'micro'
        if follower_count < TIER_BOUNDARIES['macro'][0]:
            return 'mid'
        return 'macro'

    @classmethod
    def filter_mid_tier(cls, profiles: Iterable[dict]) -> List[dict]:
        """Keep only 腰部 creators (1-10万粉丝, tier == 'micro').

        Args:
            profiles: iterable of profile dicts carrying 'follower_count'.

        Returns:
            New list, page order preserved. Entries without a valid
            follower_count (None / non-numeric) are dropped.
        """
        result: List[dict] = []
        for profile in profiles:
            count = (profile or {}).get('follower_count')
            if not isinstance(count, (int, float)):
                logger.debug("filter_mid_tier: dropped profile without "
                             f"numeric follower_count: {profile!r}")
                continue
            if cls.classify_tier(int(count)) == 'micro':
                result.append(profile)
        return result

    # ------------------------------------------------------------------
    # Profile page
    # ------------------------------------------------------------------
    def scrape_profile(self, creator_id: str) -> Optional[dict]:
        """Fetch and parse one creator profile into the CREATOR_FIELDS dict.

        Returns:
            dict with the 6 CREATOR_FIELDS, or None when the page is
            empty / 404 / missing embedded data (logged, never crashes).

        Raises:
            QuotaExceededException: daily creator quota exhausted.
            RateLimitException: still in soft cooldown, or 429 received.
            WAFBlockedException: 403/418/503 received (soft cooldown is
                armed before the exception propagates).
        """
        # Daily quota gate (DAILY_QUOTA_CREATORS=100 via common config)
        self.check_quota(self.RESOURCE_TYPE, config.DAILY_QUOTA_CREATORS)

        url = self._profile_url(creator_id)
        logger.info(f"Scraping creator {creator_id} -> {url}")

        try:
            response = self.fetch(url)
        except (RateLimitException, WAFBlockedException):
            # fetch() already armed the soft cooldown; let it propagate
            raise
        except Exception as exc:
            # Network-level errors are logged and surfaced as None so batch
            # jobs keep running.
            logger.error(f"Network error scraping creator {creator_id}: {exc}")
            return None

        status = getattr(response, 'status_code', None)
        if status == 404:
            logger.warning(f"Creator {creator_id} returned 404, skipping")
            return None
        if status != 200:
            logger.warning(
                f"Creator {creator_id} returned unexpected status {status}"
            )
            return None

        data = self.parser.parse(response.text, creator_id)
        if data is None:
            logger.warning(
                f"Creator {creator_id}: no parseable embedded data "
                f"(empty/blocked page)"
            )
            return None

        missing = [f for f in CREATOR_FIELDS if data.get(f) is None]
        if missing:
            logger.info(f"Creator {creator_id} incomplete fields: {missing}")
        return data

    # ------------------------------------------------------------------
    # Showcase page
    # ------------------------------------------------------------------
    def get_showcase_products(self, creator_id: str) -> List[str]:
        """Fetch the creator's showcase page and return product IDs.

        Returns:
            List of product ID strings (de-duplicated, page order kept).
            Empty list when the page is empty / 404 / no product links
            (logged, never crashes).

        Raises:
            RateLimitException / WAFBlockedException: same contract as
                scrape_profile.
        """
        self.check_quota(self.RESOURCE_TYPE, config.DAILY_QUOTA_CREATORS)

        url = self._showcase_url(creator_id)
        logger.info(f"Scraping showcase of creator {creator_id} -> {url}")

        try:
            response = self.fetch(url)
        except (RateLimitException, WAFBlockedException):
            raise
        except Exception as exc:
            logger.error(
                f"Network error scraping showcase {creator_id}: {exc}"
            )
            return []

        status = getattr(response, 'status_code', None)
        if status == 404:
            logger.warning(f"Showcase page 404 for creator {creator_id}")
            return []
        if status != 200:
            logger.warning(
                f"Showcase page unexpected status {status} for creator {creator_id}"
            )
            return []

        regex_spec = self.parser.selectors['showcase_page']['product_id_regex']
        pattern = re.compile(regex_spec['value'])

        ids: List[str] = []
        seen = set()
        # scan the raw HTML so both href attributes and inline JSON blobs
        # containing product links are covered
        for match in pattern.finditer(response.text):
            pid = match.group(1)
            if pid not in seen:
                seen.add(pid)
                ids.append(pid)

        if not ids:
            logger.warning(f"No product links found on showcase page of {creator_id}")
        else:
            logger.info(f"Showcase of {creator_id}: {len(ids)} products")
        return ids
