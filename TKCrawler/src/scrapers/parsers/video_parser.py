"""VideoParser: extract video fields from TikTok API JSON / product-page HTML.

All JSON paths and selectors come from src/scrapers/video_selectors.json
where every entry carries "verified": false (see docs/reverse/video-api.md).
The parsing LOGIC is unit-tested against fixtures built to match these
assumed structures; real response/page structure still requires on-device
calibration.

Strategy:
1. API path: parse_video_item(raw_dict) -> VIDEO_FIELDS dict.
   For each field, try each configured json_path in order.
2. Page path: extract_embedded_json(html) walks the rehydration nodes
   (UNIVERSAL_DATA first, SIGI_STATE fallback), then video_root_candidates
   locate the video list subtree; items go through parse_video_item.
3. is_paid: no single authoritative public field — truthy_candidates list,
   any truthy hit => True, all missing => False (assumption, pending
   calibration).
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

from bs4 import BeautifulSoup

from ...common.logger import get_logger
from .product_parser import _MISSING, _walk_json_path

logger = get_logger(__name__)

SELECTORS_PATH = Path(__file__).resolve().parent.parent / 'video_selectors.json'

# Core fields (Agent E task card / video_snapshots schema)
VIDEO_FIELDS = [
    'video_id', 'product_id', 'author_id', 'publish_time',
    'views', 'likes', 'comments', 'shares',
    'duration', 'is_paid',
]


class VideoParser:
    """Parses TikTok video payloads into the VIDEO_FIELDS dict shape."""

    def __init__(self, selectors_path: Path = None,
                 product_id: str = None):
        """Args:
            selectors_path: override the selectors file (tests / hot patch).
            product_id: stamped onto every parsed item's product_id field.
        """
        self.selectors_path = Path(selectors_path) if selectors_path else SELECTORS_PATH
        self.selectors = self._load_selectors(self.selectors_path)
        self.product_id = product_id

    @staticmethod
    def _load_selectors(path: Path) -> dict:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)

    # ------------------------------------------------------------------
    # API response item
    # ------------------------------------------------------------------
    def _pick(self, item: dict, spec: dict) -> Any:
        """Try each json_path in order, return the first present hit
        (0/False/'' are valid values; _MISSING/None are not)."""
        for path in spec.get('json_paths', []):
            value = _walk_json_path(item, path)
            if value is not _MISSING and value is not None:
                return value
        return None

    def _pick_truthy(self, item: dict, spec: dict) -> Optional[bool]:
        """truthy_candidates: any truthy hit => True; a present-but-falsy
        candidate is inconclusive; all missing => None (caller decides)."""
        seen_any = False
        for path in spec.get('truthy_candidates', []):
            value = _walk_json_path(item, path)
            if value is _MISSING or value is None:
                continue
            seen_any = True
            if value:
                return True
        return False if seen_any else None

    def _normalize_publish_time(self, raw: Any) -> Optional[str]:
        """Unix epoch seconds -> ISO8601 UTC string (None passthrough)."""
        if raw is None or raw == '' or raw == 0:
            return None
        try:
            return datetime.fromtimestamp(int(raw), tz=timezone.utc) \
                .strftime('%Y-%m-%dT%H:%M:%SZ')
        except (ValueError, TypeError, OSError, OverflowError):
            logger.warning(f"Unparseable publish_time: {raw!r}")
            return None

    def parse_video_item(self, raw: dict) -> Optional[dict]:
        """Map one raw video payload onto VIDEO_FIELDS.

        Returns None when video_id cannot be resolved (item unusable),
        otherwise a dict with all 10 VIDEO_FIELDS (missing values None /
        0 / False as appropriate).
        """
        if not isinstance(raw, dict):
            return None
        fields = self.selectors['fields']

        video_id = self._pick(raw, fields['video_id'])
        if video_id is None:
            logger.warning("Video item without resolvable video_id, skipped")
            return None

        result = {field: None for field in VIDEO_FIELDS}
        result['video_id'] = str(video_id)
        result['product_id'] = str(self.product_id) if self.product_id else None

        author_id = self._pick(raw, fields['author_id'])
        result['author_id'] = str(author_id) if author_id is not None else None

        result['publish_time'] = self._normalize_publish_time(
            self._pick(raw, fields['publish_time'])
        )

        for field in ('views', 'likes', 'comments', 'shares', 'duration'):
            value = self._pick(raw, fields[field])
            try:
                result[field] = int(value) if value is not None else 0
            except (ValueError, TypeError):
                result[field] = 0

        is_paid = self._pick_truthy(raw, fields['is_paid'])
        result['is_paid'] = bool(is_paid) if is_paid is not None else False

        return result

    # ------------------------------------------------------------------
    # Root discovery (API envelope or page rehydration JSON)
    # ------------------------------------------------------------------
    def find_video_roots(self, data: dict) -> List[dict]:
        """Walk video_root_candidates, return the video-list items from the
        first hit (dict-of-items or list)."""
        candidates = self.selectors['embedded_json']['video_root_candidates']['value']
        for candidate in candidates:
            node = _walk_json_path(data, candidate)
            if isinstance(node, list) and node:
                return [n for n in node if isinstance(n, dict)]
            if isinstance(node, dict):
                # dict form: {"<id>": {...}} or {"videos": [...]}
                values = list(node.values())
                if values and all(isinstance(v, dict) for v in values):
                    return values
        logger.warning("No video root candidate matched in payload")
        return []

    # ------------------------------------------------------------------
    # Page HTML path (fallback strategy)
    # ------------------------------------------------------------------
    def extract_embedded_json(self, html: str) -> Optional[dict]:
        """Extract rehydration JSON from product-page HTML.

        Tries __UNIVERSAL_DATA_FOR_REHYDRATION__ then SIGI_STATE.
        Returns None (logs, never raises) when neither node parses.
        """
        if not html or not html.strip():
            logger.warning("Empty HTML passed to VideoParser")
            return None

        soup = BeautifulSoup(html, 'lxml')
        embedded = self.selectors['embedded_json']
        candidates = [
            embedded['universal_data_script_id']['value'],
            embedded['sigi_state_script_id']['value'],
        ]
        for css in candidates:
            tag = soup.select_one(css)
            if tag is None:
                continue
            try:
                return json.loads(tag.string or '')
            except (json.JSONDecodeError, TypeError) as exc:
                logger.warning(f"Embedded JSON node {css} failed to parse: {exc}")
        logger.warning("No embedded JSON node found (UNIVERSAL_DATA / SIGI_STATE)")
        return None

    def parse_product_page(self, html: str) -> List[dict]:
        """Parse a product page's embedded JSON into video dicts."""
        data = self.extract_embedded_json(html)
        if data is None:
            return []
        items = self.find_video_roots(data)
        parsed = []
        for raw in items:
            item = self.parse_video_item(raw)
            if item is not None:
                parsed.append(item)
        logger.info(f"parse_product_page: {len(parsed)} videos extracted")
        return parsed
