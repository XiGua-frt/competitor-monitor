"""Product page parser: extract the 15 core fields from TikTok Shop HTML.

IMPORTANT - assumption vs verified:
All selectors/JSON paths come from src/scrapers/selectors.json where every
entry carries "verified": false. They are based on public knowledge of
Next.js-style TikTok pages (__UNIVERSAL_DATA_FOR_REHYDRATION__ /
SIGI_STATE embedded JSON) and have NOT been validated against a live
TikTok Shop page. The parsing LOGIC is unit-tested against fixture HTML
built to match these assumed structures; the real page structure still
requires on-device calibration (see selectors.json "_meta").

Strategy:
1. Find the embedded JSON node (UNIVERSAL_DATA first, SIGI_STATE fallback).
2. Walk "root_candidates" to locate the product-detail subtree.
3. For each of the 15 fields, follow its json_path; on miss, try the CSS
   fallback via BeautifulSoup.
4. Normalize values (strip currency symbols, expand "1.2k" suffixes).
5. Compute gmv_30d_est = price_current * sales_30d.
"""

import json
import re
from pathlib import Path
from typing import Any, Optional

from bs4 import BeautifulSoup

from ...common.logger import get_logger

logger = get_logger(__name__)

SELECTORS_PATH = Path(__file__).resolve().parent.parent / 'selectors.json'

# The 15 core fields (Agent D task card / schema doc)
REQUIRED_FIELDS = [
    'product_id', 'title',
    'category_l1', 'category_l2', 'category_l3',
    'price_current', 'price_original',
    'sales_30d', 'gmv_30d_est', 'listed_date',
    'shop_id', 'shop_name',
    'creator_count', 'video_count_7d',
    'review_count', 'rating',
]
# gmv_30d_est is derived, not directly extracted
EXTRACTED_FIELDS = [f for f in REQUIRED_FIELDS if f != 'gmv_30d_est']

_NUMBER_RE = re.compile(r'^[\d.,\s]+')
_KM_SUFFIX_RE = re.compile(r'([\d.,]+)\s*([kKmM])\b')

# Module-level sentinel: a unique object meaning "path not found" that can
# never collide with a real JSON value (including None/False/0).
_MISSING = object()


def parse_number_with_suffix(text: Any) -> float:
    """Parse human-formatted numbers: '1.2k'->1200.0, '3m'->3000000.0,
    '$19.99'->19.99, '1,234'->1234.0.

    Returns 0.0 when nothing numeric can be extracted (never raises).
    """
    if text is None:
        return 0.0
    if isinstance(text, (int, float)):
        return float(text)

    s = str(text).strip()
    if not s:
        return 0.0

    # Suffix form first: "10k+", "1.2k", "3M"
    m = _KM_SUFFIX_RE.search(s)
    if m:
        base = float(m.group(1).replace(',', ''))
        mult = {'k': 1_000, 'm': 1_000_000}[m.group(2).lower()]
        return base * mult

    # Plain numeric (possibly with currency symbols / commas / ranges like "500-1k" handled by caller)
    m = _NUMBER_RE.match(s)
    if m:
        cleaned = m.group(0).replace(',', '').strip()
        try:
            return float(cleaned)
        except ValueError:
            return 0.0
    return 0.0


def parse_sales_range_text(text: str) -> int:
    """Convert a sales-range label to a median integer.

    "500-1k" -> 750   (median of 500 and 1000)
    "10k+"   -> 10000 (floor value)
    "1.2k"   -> 1200  (plain value)
    "" / unparseable -> 0 (documented contract: invalid input yields 0,
    callers decide whether 0 is acceptable; never raises)
    """
    if text is None:
        return 0
    s = str(text).strip()
    if not s:
        return 0

    if '+' in s:
        return int(parse_number_with_suffix(s.replace('+', '')))

    if '-' in s:
        parts = s.split('-', 1)
        low = parse_number_with_suffix(parts[0])
        high = parse_number_with_suffix(parts[1])
        if low == 0 and high == 0:
            return 0
        return int((low + high) // 2)

    value = parse_number_with_suffix(s)
    return int(value)


def _walk_json_path(data: Any, path: str) -> Any:
    """Walk a dotted JSON path with bracket segments.

    Supports: "a.b[0].c", "__DEFAULT_SCOPE__['webapp.shop-product-detail']".
    Returns the module-level _MISSING sentinel when any segment fails.
    """
    # Tokenize: split on dots outside brackets, expand ['key'] / ["key"] / [0]
    tokens: list = []
    for raw_dot in re.split(r'\.(?![^\[]*\])', path):
        m = re.match(r"^([^\[\]]+)((?:\[[^\]]*\])*)$", raw_dot.strip())
        if not m:
            return _MISSING
        base = m.group(1).strip()
        if base:
            tokens.append(base)
        for bracket in re.findall(r"\[([^\]]*)\]", m.group(2) or ''):
            key = bracket.strip().strip("'\"")
            tokens.append(int(key) if re.fullmatch(r'\d+', key) else key)

    cur = data
    for token in tokens:
        try:
            if isinstance(cur, dict):
                if token not in cur:
                    return _MISSING
                cur = cur[token]
            elif isinstance(cur, list) and isinstance(token, int):
                cur = cur[token]
            else:
                return _MISSING
        except (IndexError, KeyError, TypeError):
            return _MISSING
    return cur


class ProductParser:
    """Parses TikTok Shop product page HTML into the 15-field dict."""

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
            logger.warning("Empty HTML passed to ProductParser")
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

    def _find_product_root(self, data: dict) -> Optional[dict]:
        """Try each root_candidate JSON path, return the first dict hit."""
        for candidate in self.selectors['embedded_json']['root_candidates']['value']:
            node = _walk_json_path(data, candidate)
            if isinstance(node, dict):
                logger.debug(f"Product root found at candidate: {candidate}")
                return node
        logger.warning("No root candidate matched in embedded JSON")
        return None

    # ------------------------------------------------------------------
    # Field extraction
    # ------------------------------------------------------------------
    def _extract_field(self, product_root: dict, soup: Optional[BeautifulSoup],
                       field: str) -> Any:
        """Extract one field: json_path first, CSS fallback second."""
        spec = self.selectors['fields'].get(field)
        if spec is None:
            logger.warning(f"No selector configured for field '{field}'")
            return None

        value = _walk_json_path(product_root, spec['value'])
        if value is None:
            # json path miss -> try CSS fallback
            css = spec.get('css_fallback')
            if soup is not None and css:
                tag = soup.select_one(css)
                if tag is not None:
                    if field == 'product_id' and tag.has_attr('data-product-id'):
                        value = tag.get('data-product-id')
                    else:
                        value = tag.get_text(strip=True)
        return value

    def _normalize(self, field: str, value: Any) -> Any:
        """Normalize raw extracted values to schema types."""
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if field in ('price_current', 'price_original', 'rating'):
            return round(parse_number_with_suffix(value), 2) or None
        if field in ('creator_count', 'video_count_7d', 'review_count'):
            return int(parse_number_with_suffix(value)) or None
        if field in ('product_id', 'shop_id'):
            return str(value).strip()
        # title / categories / sales_30d / listed_date / shop_name: raw strings
        return str(value).strip()

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------
    def parse(self, html: str) -> Optional[dict]:
        """Parse product page HTML into the 15-field dict.

        Returns:
            dict with all 15 REQUIRED_FIELDS (missing values as None), or
            None when the page carries no usable embedded JSON
            (empty page / 404 / blocked). Never raises for structural issues;
            logs warnings instead.
        """
        data = self.extract_embedded_json(html)
        if data is None:
            return None

        product_root = self._find_product_root(data)
        if product_root is None:
            return None

        soup = BeautifulSoup(html, 'lxml')
        result = {field: None for field in REQUIRED_FIELDS}

        for field in EXTRACTED_FIELDS:
            raw = self._extract_field(product_root, soup, field)
            result[field] = self._normalize(field, raw)

        # Derive sales median and GMV estimate
        result['sales_30d'] = parse_sales_range_text(result.get('sales_30d'))
        price = result.get('price_current') or 0.0
        result['gmv_30d_est'] = round(price * result['sales_30d'], 2) \
            if result['sales_30d'] and price else None

        missing = [f for f, v in result.items() if v is None]
        if missing:
            logger.warning(f"Fields missing after parse: {missing}")
        return result
