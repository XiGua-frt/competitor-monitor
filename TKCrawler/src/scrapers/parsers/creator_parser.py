"""Creator profile parser: extract CREATOR_FIELDS from TikTok user page HTML.

IMPORTANT - assumption vs verified:
All selectors/JSON paths come from src/scrapers/creator_selectors.json where
every entry carries "verified": false. They are based on public knowledge of
TikTok web pages (__UNIVERSAL_DATA_FOR_REHYDRATION__ / SIGI_STATE embedded
JSON) and have NOT been validated against a live TikTok user profile. The
parsing LOGIC is unit-tested against fixture HTML built to match these
assumed structures. Reverse-analysis writeup: docs/reverse/creator-api.md.

Strategy:
1. Find the embedded JSON node (UNIVERSAL_DATA first, SIGI_STATE fallback).
2. Locate the user-info root via root_candidates (the UserModule.users dict
   variant is special-cased: match by unique_id, else first entry).
3. Extract fields via json_path; on miss, try the CSS fallback.
4. avg_views_30d: NOT provided directly by the profile interface — estimated
   as the median playCount of up to the latest 30 embedded videos (see
   docs/reverse/creator-api.md §3).
5. category_tags: hashtags from bio + sampled video descriptions (dedup,
   max 10, comma-joined).
"""

import json
import re
import statistics
from pathlib import Path
from typing import Any, Optional

from bs4 import BeautifulSoup

from ...common.logger import get_logger
from .product_parser import parse_number_with_suffix

logger = get_logger(__name__)

SELECTORS_PATH = Path(__file__).resolve().parent.parent / 'creator_selectors.json'

# 6 core fields (Agent F task card / creator_snapshots contract)
CREATOR_FIELDS = [
    'creator_id',
    'nickname',
    'follower_count',
    'avg_views_30d',
    'category_tags',
    'product_count',
]

# internal key: list of raw video dicts kept between _extract_field and the
# derived-field pass (not part of the public dict contract)
_VIDEO_LIST_KEY = '_videos'

_HASHTAG_RE_TPL = r'#([\w\u4e00-\u9fff]+)'


def _walk_json_path(data: Any, path: str) -> Any:
    """Walk a dotted JSON path with bracket segments (e.g. a.b[0].c).

    Returns None when any segment fails (creator-local copy so this parser
    stays decoupled from ProductParser internals).
    """
    tokens: list = []
    for raw_dot in re.split(r'\.(?![^\[]*\])', path):
        m = re.match(r"^([^\[\]]+)((?:\[[^\]]*\])*)$", raw_dot.strip())
        if not m:
            return None
        base = m.group(1).strip()
        if base:
            tokens.append(base)
        for bracket in re.findall(r"\[([^\]]*)\]", m.group(2) or ''):
            key = bracket.strip().strip("'\"")
            tokens.append(int(key) if re.fullmatch(r'\d+', key) else key)

    cur = data
    for token in tokens:
        try:
            if isinstance(cur, dict) and token in cur:
                cur = cur[token]
            elif isinstance(cur, list) and isinstance(token, int):
                cur = cur[token]
            else:
                return None
        except (IndexError, KeyError, TypeError):
            return None
    return cur


class CreatorParser:
    """Parses TikTok user-profile HTML into the CREATOR_FIELDS dict."""

    def __init__(self, selectors_path: Path = None):
        self.selectors_path = Path(selectors_path) if selectors_path else SELECTORS_PATH
        self.selectors = self._load_selectors(self.selectors_path)

    @staticmethod
    def _load_selectors(path: Path) -> dict:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)

    # ------------------------------------------------------------------
    # Embedded JSON extraction
    # ------------------------------------------------------------------
    def extract_embedded_json(self, html: str) -> Optional[dict]:
        """Extract the embedded rehydration JSON.

        Tries __UNIVERSAL_DATA_FOR_REHYDRATION__ then SIGI_STATE.
        Returns None (logs a warning) when neither node exists — callers
        treat this as an empty/blocked page rather than crashing.
        """
        if not html or not html.strip():
            logger.warning("Empty HTML passed to CreatorParser")
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

    # ------------------------------------------------------------------
    # User-info root location
    # ------------------------------------------------------------------
    @staticmethod
    def _looks_like_user(node: dict) -> bool:
        """A user-info root is expected to carry 'user' (UNIVERSAL_DATA
        userInfo wrapper) or direct profile keys (SIGI_STATE user object)."""
        return ('user' in node or 'stats' in node or 'nickname' in node
                or 'signature' in node)

    def _find_user_root(self, data: dict, creator_id: str) -> Optional[dict]:
        """Try each root_candidate; special-case the UserModule.users dict.

        UserModule.users (SIGI_STATE) maps uniqueId -> user object; we look
        up creator_id first, then fall back to the first entry.
        """
        for candidate in self.selectors['embedded_json']['root_candidates']['value']:
            node = _walk_json_path(data, candidate)
            if not isinstance(node, dict):
                continue
            if self._looks_like_user(node):
                logger.debug(f"User root found at candidate: {candidate}")
                return node
            # users-mapping shape (uniqueId -> user object)
            values = [v for v in node.values() if isinstance(v, dict)]
            if values:
                node = node.get(creator_id) if creator_id in node \
                    else next(iter(values))
                if isinstance(node, dict) and self._looks_like_user(node):
                    logger.debug(
                        f"User root found via users-mapping candidate: {candidate}"
                    )
                    return node
        logger.warning("No user-info root matched in embedded JSON")
        return None

    # ------------------------------------------------------------------
    # Video sampling
    # ------------------------------------------------------------------
    def _extract_videos(self, data: dict, user_root: Optional[dict]) -> list:
        """Collect raw video dicts: UNIVERSAL_DATA itemList first, then the
        SIGI_STATE ItemModule dict (order not guaranteed there — sorted by
        descending playCount is NOT applied; page order is assumed recency
        for UNIVERSAL_DATA only, disclosed in docs §3)."""
        max_sample = self.selectors['videos']['max_sample']['value']

        items: list = []
        if user_root is not None:
            node = _walk_json_path(
                user_root, self.selectors['videos']['item_list_json_path']['value']
            )
            if isinstance(node, list):
                items = node

        if not items:
            module = _walk_json_path(
                data, self.selectors['videos']['sigi_item_module_key']['value']
            )
            if isinstance(module, dict):
                items = [v for v in module.values() if isinstance(v, dict)]

        return items[:max_sample]

    def _compute_avg_views(self, videos: list) -> Optional[int]:
        """Median playCount over sampled videos; None when no data."""
        path = self.selectors['videos']['play_count_json_path']['value']
        counts = [
            _walk_json_path(v, path)
            for v in videos
        ]
        counts = [float(c) for c in counts if isinstance(c, (int, float))]
        if not counts:
            return None
        median = statistics.median(counts)
        # Integer column (avg_views_30d): round halves up on ties
        return int(round(median))

    # ------------------------------------------------------------------
    # category_tags
    # ------------------------------------------------------------------
    def _compute_category_tags(self, bio: Optional[str], videos: list) -> Optional[str]:
        """Hashtags from bio (first) then sampled video descs, dedup keep
        order, max max_tags, comma-joined. None when nothing found."""
        regex = re.compile(self.selectors['category_tags']['hashtag_regex']['value'])
        max_tags = self.selectors['category_tags']['max_tags']['value']

        tags: list = []
        seen = set()

        def _collect(text):
            if not text:
                return
            for tag in regex.findall(str(text)):
                if tag not in seen:
                    seen.add(tag)
                    tags.append(tag)

        _collect(bio)
        desc_path = self.selectors['videos']['desc_json_path']['value']
        for video in videos:
            _collect(_walk_json_path(video, desc_path))
            if len(tags) >= max_tags:
                break

        if not tags:
            return None
        return ','.join(tags[:max_tags])

    # ------------------------------------------------------------------
    # Field extraction
    # ------------------------------------------------------------------
    def _css_fallback(self, soup: Optional[BeautifulSoup], html: str,
                      field: str, user_root: Optional[dict]):
        """CSS fallback for a field. product_count is a COUNT of matched
        elements (not text extraction); creator_id extracts unique_id from
        the profile-link href."""
        spec = self.selectors['fields'].get(field) or {}
        css = spec.get('css_fallback')
        if soup is None or not css:
            return None

        if field == 'product_count':
            # showcase items may live in the page or inside the user root —
            # count elements in the full document
            return len(soup.select(css)) or None

        tag = soup.select_one(css)
        if tag is None:
            return None

        if field == 'creator_id':
            href = tag.get(spec.get('attribute', 'href'), '')
            m = re.search(spec.get('href_regex', '/@([^/?#]+)'), href or '')
            return m.group(1) if m else None
        return tag.get_text(strip=True)

    def _extract_field(self, field: str, user_root: Optional[dict],
                       soup: Optional[BeautifulSoup], html: str) -> Any:
        spec = self.selectors['fields'].get(field)
        if spec is None:
            logger.warning(f"No selector configured for field '{field}'")
            return None

        value = None
        if user_root is not None:
            value = _walk_json_path(user_root, spec['value'])
            if value in (None, '') and spec['value'].startswith('user.'):
                # SIGI_STATE user objects carry the profile keys directly
                # (no 'user.' wrapper) — retry with the prefix stripped
                value = _walk_json_path(user_root, spec['value'][len('user.'):])
        if value in (None, ''):
            value = self._css_fallback(soup, html, field, user_root)
        return value

    def _normalize(self, field: str, value: Any) -> Any:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if field == 'follower_count':
            return int(parse_number_with_suffix(value)) or None
        if field == 'product_count':
            return int(parse_number_with_suffix(value)) or None
        # creator_id / nickname / bio: raw strings
        return str(value).strip()

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------
    def parse(self, html: str, creator_id: str) -> Optional[dict]:
        """Parse user-profile HTML into the CREATOR_FIELDS dict.

        Args:
            html: full page HTML.
            creator_id: the requested id (assumed unique_id); used for the
                SIGI_STATE UserModule lookup and as final fallback for the
                creator_id field.

        Returns:
            dict with all CREATOR_FIELDS (missing values None), or None when
            the page carries no usable embedded JSON (empty/blocked). Never
            raises for structural issues; logs warnings instead.
        """
        data = self.extract_embedded_json(html)
        if data is None:
            return None

        user_root = self._find_user_root(data, creator_id)
        if user_root is None:
            return None

        soup = BeautifulSoup(html, 'lxml')
        result = {field: None for field in CREATOR_FIELDS}

        for field in ('creator_id', 'nickname', 'follower_count', 'product_count'):
            raw = self._extract_field(field, user_root, soup, html)
            result[field] = self._normalize(field, raw)

        bio = None
        bio_spec = self.selectors['fields'].get('bio')
        if user_root is not None and bio_spec:
            bio = _walk_json_path(user_root, bio_spec['value'])
            if bio in (None, '') and bio_spec['value'].startswith('user.'):
                bio = _walk_json_path(user_root, bio_spec['value'][len('user.'):])
        if bio in (None, ''):
            bio = self._css_fallback(soup, html, 'bio', user_root)

        videos = self._extract_videos(data, user_root)
        result['_videos'] = videos  # internal; stripped below
        result['avg_views_30d'] = self._compute_avg_views(videos)
        result['category_tags'] = self._compute_category_tags(bio, videos)
        result.pop('_videos')

        # creator_id: JSON uid wins; input id as documented final fallback
        if not result.get('creator_id'):
            result['creator_id'] = str(creator_id).strip()

        missing = [f for f in CREATOR_FIELDS if result.get(f) is None]
        if missing:
            logger.warning(f"Creator fields missing after parse: {missing}")
        return result
