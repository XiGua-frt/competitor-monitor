"""Hashtag page parser: extract topic views and top video IDs.

IMPORTANT - assumption vs verified:
All selectors/JSON paths come from src/scrapers/hashtag_selectors.json where
every entry carries "verified": false. They are based on public knowledge of
TikTok Web pages (__UNIVERSAL_DATA_FOR_REHYDRATION__ / SIGI_STATE embedded
JSON, topic detail historically named "challenge") and have NOT been
validated against a live TikTok hashtag page. Full reverse notes:
docs/reverse/hashtag-api.md (Evidence E-1 ~ E-6).

Strategy:
1. Find the embedded JSON node (UNIVERSAL_DATA first, SIGI_STATE fallback).
2. Walk "root_candidates" to locate the challenge-detail subtree.
3. Extract tag_name / views_total via json_path, CSS fallback via
   BeautifulSoup.
4. Top video IDs: embedded itemList candidates first, CSS anchor fallback
   with a video-id regex; de-duplicated, order kept, limited.
"""

import json
import re
from pathlib import Path
from typing import Any, List, Optional
from urllib.parse import quote

from bs4 import BeautifulSoup

from ...common.logger import get_logger
from .product_parser import _MISSING, _walk_json_path

logger = get_logger(__name__)

SELECTORS_PATH = Path(__file__).resolve().parent.parent / 'hashtag_selectors.json'

# Fields produced by parse_hashtag (Agent G task card). snapshot_date is
# added by the scraper, not the parser.
HASHTAG_FIELDS = ['tag_name', 'views_total', 'snapshot_date']

_VIDEO_ID_FALLBACK_KEYS = ('video.id', 'video.itemStruct.id', 'id')


def parse_view_count(text: Any) -> int:
    """Normalize a view-count value to int.

    Handles plain numbers, comma-grouped numbers and human suffixes:
        617000000000 -> 617000000000
        '61.7B'      -> 61700000000
        '1.2m'/'1.2M'-> 1200000
        '3,4k'? no — '3.4k' -> 3400
    Returns 0 when nothing numeric can be extracted (never raises).
    """
    if text is None:
        return 0
    if isinstance(text, bool):
        return 0
    if isinstance(text, (int, float)):
        return int(text)

    s = str(text).strip()
    if not s:
        return 0

    m = re.search(r'([\d.,]+)\s*([kKmMbB])\b', s)
    if m:
        try:
            base = float(m.group(1).replace(',', ''))
        except ValueError:
            return 0
        mult = {'k': 1_000, 'm': 1_000_000, 'b': 1_000_000_000}[m.group(2).lower()]
        return int(base * mult)

    cleaned = s.replace(',', '')
    m = re.match(r'^\s*(\d+(?:\.\d+)?)', cleaned)
    if m:
        try:
            return int(float(m.group(1)))
        except ValueError:
            return 0
    return 0


class HashtagParser:
    """Parses TikTok hashtag page HTML into tag fields + top video IDs."""

    def __init__(self, selectors_path: Path = None):
        self.selectors_path = (
            Path(selectors_path) if selectors_path else SELECTORS_PATH
        )
        with open(self.selectors_path, 'r', encoding='utf-8') as f:
            self.selectors = json.load(f)

    # ------------------------------------------------------------------
    # Embedded JSON extraction (same mechanism as ProductParser)
    # ------------------------------------------------------------------
    def extract_embedded_json(self, html: str) -> Optional[dict]:
        """Extract the embedded rehydration JSON.

        Tries __UNIVERSAL_DATA_FOR_REHYDRATION__ then SIGI_STATE.
        Returns None (logs a warning) when neither node exists.
        """
        if not html or not html.strip():
            logger.warning("Empty HTML passed to HashtagParser")
            return None

        soup = BeautifulSoup(html, 'lxml')
        candidates = [
            self.selectors['embedded_json']['universal_data_script_id']['value'],
            self.selectors['embedded_json']['sigi_state_script_id']['value'],
        ]
        for css in candidates:
            tag = soup.select_one(css)
            if tag is None:
                continue
            try:
                data = json.loads(tag.string or '')
            except (json.JSONDecodeError, TypeError) as exc:
                logger.warning(f"Embedded JSON node {css} failed to parse: {exc}")
                continue
            logger.debug(f"Embedded JSON extracted from {css}")
            return data

        logger.warning("No embedded JSON node found (UNIVERSAL_DATA / SIGI_STATE)")
        return None

    def _find_hashtag_root(self, data: dict) -> Optional[dict]:
        """Try each root_candidate JSON path, return the first dict hit."""
        for candidate in self.selectors['embedded_json']['root_candidates']['value']:
            node = _walk_json_path(data, candidate)
            if isinstance(node, dict):
                logger.debug(f"Hashtag root found at candidate: {candidate}")
                return node
        logger.warning("No root candidate matched in embedded JSON")
        return None

    # ------------------------------------------------------------------
    # Field extraction
    # ------------------------------------------------------------------
    def _extract_field(self, root: Optional[dict], soup: Optional[BeautifulSoup],
                       field: str, alt_root: Optional[dict] = None) -> Any:
        """Extract one field: json_path first, CSS fallback second.

        The json_path is tried against `root` first, then against
        `alt_root` (the full embedded JSON) — depending on the page
        variant, root_candidates may resolve to the challenge container
        itself or to a wrapper around it.
        """
        spec = self.selectors['fields'].get(field)
        if spec is None:
            logger.warning(f"No selector configured for field '{field}'")
            return None

        value = _MISSING
        if root is not None:
            value = _walk_json_path(root, spec['value'])
        if value is _MISSING and alt_root is not None:
            value = _walk_json_path(alt_root, spec['value'])
        if value is _MISSING:
            value = None
        if value is None and soup is not None and spec.get('css_fallback'):
            tag = soup.select_one(spec['css_fallback'])
            if tag is not None:
                value = tag.get_text(strip=True)
        return value

    # ------------------------------------------------------------------
    # Top video IDs
    # ------------------------------------------------------------------
    @staticmethod
    def _video_id_from_item(item: Any) -> Optional[str]:
        """Pull a video ID string out of one embedded-JSON video object."""
        if isinstance(item, str) and item.isdigit():
            return item
        if not isinstance(item, dict):
            return None
        for path in _VIDEO_ID_FALLBACK_KEYS:
            value = _walk_json_path(item, path)
            if value is not None and value is not _MISSING:
                return str(value)
        return None

    def _video_ids_from_json(self, root: dict) -> List[str]:
        """Extract video IDs from embedded JSON candidate list paths."""
        ids: List[str] = []
        for candidate in self.selectors['top_videos']['video_list_json_path']['value']:
            node = _walk_json_path(root, candidate)
            if isinstance(node, list):
                for item in node:
                    vid = self._video_id_from_item(item)
                    if vid:
                        ids.append(vid)
            elif isinstance(node, dict):
                # itemModule-style mapping {video_id: {...}}
                for key, item in node.items():
                    vid = str(key) if str(key).isdigit() \
                        else self._video_id_from_item(item)
                    if vid:
                        ids.append(vid)
            if ids:
                logger.debug(f"Video IDs found at candidate: {candidate}")
                break
        return ids

    def _video_ids_from_html(self, html: str) -> List[str]:
        """CSS/regex fallback: any anchor pointing at a /video/ detail page."""
        pattern = re.compile(self.selectors['top_videos']['video_id_regex']['value'])
        css = self.selectors['top_videos']['video_link']['value']
        attr = self.selectors['top_videos']['video_link'].get('attribute', 'href')

        soup = BeautifulSoup(html, 'lxml')
        ids: List[str] = []
        seen = set()
        for tag in soup.select(css):
            href = tag.get(attr, '') or ''
            m = pattern.search(href)
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                ids.append(m.group(1))
        return ids

    def get_top_video_ids(self, html: str, limit: int = 30) -> List[str]:
        """Extract top video IDs from a hashtag page.

        Order: embedded-JSON list candidates first, CSS anchor fallback
        second. De-duplicated, page order kept, capped at `limit`.
        Returns an empty list (logs a warning) when nothing is found;
        never raises for structural issues.
        """
        if not html or not html.strip():
            logger.warning("Empty HTML passed to get_top_video_ids")
            return []

        ids: List[str] = []
        data = self.extract_embedded_json(html)
        if data is not None:
            root = self._find_hashtag_root(data)
            if root is not None:
                ids = self._video_ids_from_json(root)

        if not ids:
            ids = self._video_ids_from_html(html)

        if not ids:
            logger.warning("No video IDs found on hashtag page")
            return []

        # De-duplicate preserving order, then cap at limit
        deduped = list(dict.fromkeys(ids))
        return deduped[:limit]

    # ------------------------------------------------------------------
    # Signed-API channel (Agent P, live-verified 2026-10-07)
    # ------------------------------------------------------------------
    def parse_hashtag_api(self, payload: Any) -> Optional[dict]:
        """Parse a challenge/detail signed-API response into tag fields.

        Verified channel (docs/reverse/hashtag-api.md E-8, evidence
        docs/probe/20261007T154904Z_api_challenge_detail_body.json):
        response is a bare JSON object (not HTML) whose
        `challengeInfo.challenge` subtree carries `title` (tag name) and
        `stats.viewCount` (int, rounded) / `statsV2.viewCount` (str,
        precise). Fields are resolved via hashtag_selectors.json full
        paths (verified: true entries).

        Args:
            payload: response JSON as dict, or a JSON string.

        Returns:
            {tag_name, views_total} (same contract as parse_hashtag), or
            None when the payload is not a usable challenge/detail body
            (missing challengeInfo / non-zero statusCode). Never raises.
        """
        if isinstance(payload, (str, bytes)):
            try:
                payload = json.loads(payload)
            except (json.JSONDecodeError, TypeError) as exc:
                logger.warning(f"API payload is not valid JSON: {exc}")
                return None
        if not isinstance(payload, dict):
            logger.warning("API payload is not a JSON object")
            return None

        status_code = payload.get('statusCode', payload.get('status_code', 0))
        if status_code not in (0, None):
            logger.warning(f"API returned statusCode={status_code}")
            return None
        if not isinstance(payload.get('challengeInfo'), dict):
            logger.warning("API payload has no challengeInfo object")
            return None

        # Full-path walk against the payload (selectors use paths rooted
        # at the response top level).
        tag_name = self._extract_field(None, None, 'tag_name', alt_root=payload)
        views_raw = self._extract_field(
            None, None, 'views_total', alt_root=payload)

        if tag_name is None and views_raw is None:
            logger.warning("API response produced no tag_name and no views")
            return None

        tag_name = str(tag_name).strip().lstrip('#') if tag_name else None
        return {
            'tag_name': tag_name,
            'views_total': parse_view_count(views_raw),
        }

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------
    def parse_hashtag(self, html: str) -> Optional[dict]:
        """Parse hashtag page HTML into {tag_name, views_total}.

        Returns:
            dict with tag_name (str, no leading '#') and views_total (int,
            0 when absent), or None when the page carries no usable
            embedded JSON and no CSS fallback targets (empty / blocked).
            Never raises for structural issues; logs warnings instead.
        """
        data = self.extract_embedded_json(html)
        root = None
        soup = None

        if data is not None:
            root = self._find_hashtag_root(data)
        if root is None:
            # JSON path unavailable/missed: still allow CSS-only extraction
            soup = BeautifulSoup(html, 'lxml')

        if root is None and data is None and soup is None:
            return None

        # Field paths are tried against root first, then the full
        # embedded JSON (root variant dependent), then CSS fallback.
        tag_name = self._extract_field(root, soup, 'tag_name', alt_root=data)
        views_raw = self._extract_field(root, soup, 'views_total', alt_root=data)

        if tag_name is None and views_raw is None:
            logger.warning("Hashtag page produced no tag_name and no views")
            return None

        tag_name = str(tag_name).strip().lstrip('#') if tag_name else None
        if not tag_name:
            logger.warning("tag_name missing after parse; using None")
        return {
            'tag_name': tag_name,
            'views_total': parse_view_count(views_raw),
        }
