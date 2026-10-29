"""Shop PDP parser: extract product data from shop.tiktok.com product pages.

Channel basis (live-verified, docs/probe/shop_retest_2026-10-08.md):
- Entry URL: https://shop.tiktok.com/view/product/<id>?region=<REGION>
  (the old www.tiktok.com/shop/product/{id} template is dead: 302 -> homepage)
- Data lives in <script type="application/json" id="__MODERN_ROUTER_DATA__">
  (the shop domain uses modern.js and has NO
  __UNIVERSAL_DATA_FOR_REHYDRATION__ block).
- Inside the router data:
  loaderData["(region)/pdp/(product_name_slug$)/(product_id)/page"]
    .page_config.components_map[N].component_data
  N is NOT stable (probe evidence: index 3 on the final MY page, index 5 on
  the 23002002 error page) — the product component MUST be located by
  component_type / component_data keys, never by array index.
- Field keys verified against docs/probe/shop_product_fields_2026-10-08.json
  (evidence product 1729480426738058662, MY site).

Caliber warning (口径): sold_count is "items sold globally since listed"
(product_info.sold_count_note), NOT a 30-day figure. It is exposed as
sales_total_global and must NEVER be mapped into sales_30d.
"""

import json
import re
from typing import Any, List, Optional

from ...common.logger import get_logger

logger = get_logger(__name__)

# The 15 output fields of the verified shop channel. Caliber note: this
# channel does NOT provide sales_30d / gmv_30d_est / listed_date /
# creator_count / video_count_7d (those belonged to the old unverified
# selectors.json contract); sold_count is exposed as sales_total_global.
SHOP_FIELDS = [
    'product_id',
    'title',
    'category_l1',
    'category_l2',
    'category_l3',
    'price_current',
    'price_original',
    'sales_total_global',
    'rating',
    'review_count',
    'shop_id',
    'shop_name',
    'image_urls',
    'sku_count',
    'stock_status',
]

# Router-data script node id (shop domain, modern.js architecture)
ROUTER_DATA_SCRIPT_ID = '__MODERN_ROUTER_DATA__'

# component_type of the component whose component_data carries product data
PRODUCT_COMPONENT_TYPE = 'product_info'

# Structured error code returned inside component_data when the product is
# invisible for the current account region (probe R10: US product viewed
# with an hk store-country account -> error_code 23002002
# "get product detail not exist"). 200 shell page, not an HTTP error.
ERROR_REGION_NOT_EXIST = 23002002

_ROUTER_SCRIPT_RE = re.compile(
    r'<script[^>]*\bid=["\']' + re.escape(ROUTER_DATA_SCRIPT_ID) +
    r'["\'][^>]*>(.*?)</script>', re.S)

_TITLE_RE = re.compile(r'<title[^>]*>(.*?)</title>', re.S | re.I)

# argus challenge page (probe R9 sample: 5.7KB, <title>Security Check</title>)
SECURITY_CHECK_TITLE = 'security check'

_PRICE_DECIMAL_RE = re.compile(r'[\d.]+')


def _parse_price(text: Any) -> Optional[float]:
    """'RM19.90' -> 19.9, '19.90' -> 19.9, 19.9 -> 19.9, garbage -> None."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return round(float(text), 2)
    m = _PRICE_DECIMAL_RE.search(str(text))
    if not m:
        return None
    try:
        return round(float(m.group(0)), 2)
    except ValueError:
        return None


def _parse_int(text: Any) -> Optional[int]:
    """'1584' -> 1584, '419' -> 419, 1584 -> 1584, garbage -> None."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return int(text)
    m = re.search(r'\d+', str(text))
    return int(m.group(0)) if m else None


def is_security_check_page(html: str) -> bool:
    """Detect the argus 'Security Check' challenge page.

    Probe evidence (R9): returned with status 200 when login cookies or the
    full chrome header set are missing; the page is ~5.7KB and its
    <title> is exactly "Security Check".
    """
    if not html:
        return False
    m = _TITLE_RE.search(html)
    return bool(m and SECURITY_CHECK_TITLE in m.group(1).strip().lower())


class ShopPageParser:
    """Parses shop.tiktok.com PDP HTML (__MODERN_ROUTER_DATA__) into the
    15-field dict."""

    # ------------------------------------------------------------------
    # Router data extraction
    # ------------------------------------------------------------------
    def extract_router_data(self, html: str) -> Optional[dict]:
        """Extract and JSON-decode the __MODERN_ROUTER_DATA__ script node.

        Returns None (logs a warning) when the node is missing or the
        payload is not valid JSON — callers treat this as an empty /
        blocked / shell page rather than crashing.
        """
        if not html or not html.strip():
            logger.warning("Empty HTML passed to ShopPageParser")
            return None

        m = _ROUTER_SCRIPT_RE.search(html)
        if not m:
            logger.warning(
                f"No <script id={ROUTER_DATA_SCRIPT_ID!r}> node found "
                f"(shell / blocked / non-shop page)"
            )
            return None

        try:
            data = json.loads(m.group(1))
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning(
                f"{ROUTER_DATA_SCRIPT_ID} payload failed to parse: {exc}"
            )
            return None

        if not isinstance(data, dict):
            logger.warning(f"{ROUTER_DATA_SCRIPT_ID} payload is not a dict")
            return None
        return data

    def find_pdp_loader(self, router_data: dict) -> Optional[dict]:
        """Locate the PDP page loader dict inside loaderData.

        The loaderData key embeds region/slug/product_id segments and its
        exact spelling varies between the 301-slug page and the view URL
        page ("(region)/pdp/..." vs "(product_name_slug$)/..."), so the
        loader is located structurally: the first loaderData value that
        carries a page_config.components_map. Never by key name.
        """
        loader_data = router_data.get('loaderData')
        if not isinstance(loader_data, dict):
            logger.warning("router data has no loaderData dict")
            return None

        for key, value in loader_data.items():
            if isinstance(value, dict) and isinstance(
                    value.get('page_config', {}).get('components_map'), list):
                logger.debug(f"PDP loader found at loaderData[{key!r}]")
                return value

        logger.warning("no loaderData entry carries page_config.components_map")
        return None

    def find_product_component_data(self, components_map: List[dict]) \
            -> Optional[dict]:
        """Locate the product component_data inside components_map.

        Located by component_type == 'product_info', with a structural
        fallback for any component_data carrying a 'product_info' dict.
        Array index is deliberately never used (probe: index moved 3 -> 5
        between pages).

        Note: on structured-error pages (e.g. 23002002) the product
        component's component_data carries error_code/error_message but NO
        'product_info' dict — the component is still returned so callers
        can surface the error.
        """
        if not isinstance(components_map, list):
            return None

        typed: Optional[dict] = None
        fallback: Optional[dict] = None
        for component in components_map:
            if not isinstance(component, dict):
                continue
            data = component.get('component_data')
            if not isinstance(data, dict):
                continue
            if component.get('component_type') == PRODUCT_COMPONENT_TYPE \
                    and typed is None:
                typed = data
            if fallback is None and isinstance(data.get('product_info'), dict):
                fallback = data

        chosen = typed if typed is not None else fallback
        if chosen is None:
            logger.warning("no product component found in components_map")
        elif not isinstance(chosen.get('product_info'), dict):
            logger.debug(
                "product component carries no product_info payload "
                f"(error_code={chosen.get('error_code')})"
            )
        return chosen

    def find_product_error(self, component_data: dict) -> Optional[tuple]:
        """Return (error_code, error_message) when the product component
        carries a structured backend error (e.g. 23002002 region
        restriction). Success pages carry error_code == 0."""
        code = component_data.get('error_code')
        if not code:
            return None
        return int(code), str(component_data.get('error_message') or '')

    # ------------------------------------------------------------------
    # Field extraction helpers (keys verified by probe, see module docstring)
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_categories(component_data: dict) -> dict:
        """Map the category chain inside the product component_data to
        category_l1/l2/l3.

        New structure (live E2E, docs/probe/e2e_shop_2026-10-08.md):
        component_data.category_info.recommended_categories[] carries the
        3-level chain WITHOUT `level` keys — array order IS the level
        chain (documented page structure, not unstable index guessing).
        Old structure (probe period, docs/probe/shop_product_response_
        2026-10-08.json): component_data.categories[] with an explicit
        `level` key — kept as fallback, mapped by `level`, never by index.
        """
        # New structure first: category_info.recommended_categories[]
        category_info = component_data.get('category_info')
        recommended = (
            category_info.get('recommended_categories')
            if isinstance(category_info, dict) else None)
        if isinstance(recommended, list) and recommended:
            names = [str(c.get('category_name') or '').strip() or None
                     if isinstance(c, dict) else None
                     for c in recommended[:3]]
            names += [None] * (3 - len(names))
            return {
                'category_l1': names[0],
                'category_l2': names[1],
                'category_l3': names[2],
            }

        # Fallback: legacy categories[] with explicit level keys
        names = {1: None, 2: None, 3: None}
        for cat in component_data.get('categories') or []:
            if not isinstance(cat, dict):
                continue
            level = cat.get('level')
            if level in names and names[level] is None:
                names[level] = str(cat.get('category_name') or '').strip() \
                    or None
        return {
            'category_l1': names[1],
            'category_l2': names[2],
            'category_l3': names[3],
        }

    @staticmethod
    def _extract_image_urls(product_info: dict) -> Optional[List[str]]:
        """First URL of every image in product_info.images[].url_list."""
        urls: List[str] = []
        for image in product_info.get('images') or []:
            if not isinstance(image, dict):
                continue
            url_list = image.get('url_list') or []
            if url_list:
                urls.append(str(url_list[0]))
        return urls or None

    @staticmethod
    def _extract_stock_status(product_info: dict) -> Optional[str]:
        """Derive stock status from skus[].stock + unavailable_info.

        Probe evidence: stock 0 + unavailable_info.text
        "This item is out of stock" on a sold-out product.
        """
        skus = [s for s in product_info.get('skus') or []
                if isinstance(s, dict)]
        if not skus:
            return None
        total_stock = sum(_parse_int(s.get('stock')) or 0 for s in skus)
        unavailable = product_info.get('unavailable_info') or {}
        if total_stock > 0:
            return 'in_stock'
        if unavailable.get('text') or all(
                (s.get('unavailable_info') or {}).get('text') for s in skus):
            return 'out_of_stock'
        return 'out_of_stock'

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------
    def parse(self, html: str) -> Optional[dict]:
        """Parse shop PDP HTML into the 15-field dict.

        Returns:
            dict with all 15 SHOP_FIELDS (missing values None, missing
            product_id filled by the caller), or None when the page has no
            usable router data / no product component. Structured backend
            errors (e.g. 23002002) are logged with a region hint and also
            yield None. Never raises for structural issues.
        """
        router_data = self.extract_router_data(html)
        if router_data is None:
            return None

        loader = self.find_pdp_loader(router_data)
        if loader is None:
            return None

        components_map = loader['page_config']['components_map']
        component_data = self.find_product_component_data(components_map)
        if component_data is None:
            return None

        error = self.find_product_error(component_data)
        if error:
            code, message = error
            if code == ERROR_REGION_NOT_EXIST:
                logger.warning(
                    f"Product not visible for this account/region "
                    f"(error_code={code}: {message}) — pick a region the "
                    f"login account can access (probe: hk account -> MY ok, "
                    f"US blocked)"
                )
            else:
                logger.warning(
                    f"Product component returned error_code={code}: {message}"
                )
            return None

        if not isinstance(component_data.get('product_info'), dict):
            logger.warning(
                "product component carries neither product_info nor a "
                "structured error — treating as shell page"
            )
            return None

        product_info = component_data['product_info']
        price = product_info.get('price') or {}
        review = product_info.get('review') or {}
        seller = product_info.get('seller') or {}

        result = {field: None for field in SHOP_FIELDS}
        result['product_id'] = str(
            product_info.get('product_id') or '').strip() or None
        result['title'] = str(product_info.get('title') or '').strip() \
            or None
        result.update(self._extract_categories(component_data))
        # price.real_price is the display string ("RM19.90"); the decimal
        # value comes from price.min_sku_price ("19.90")
        result['price_current'] = _parse_price(
            price.get('min_sku_price') or price.get('real_price'))
        result['price_original'] = _parse_price(
            price.get('min_sku_original_price') or price.get('original_price'))
        # 口径: sold_count = "items sold globally since listed" (sold_count_note)
        result['sales_total_global'] = _parse_int(product_info.get('sold_count'))
        result['rating'] = _parse_price(review.get('product_rating'))
        result['review_count'] = _parse_int(review.get('review_count'))
        result['shop_id'] = str(seller.get('seller_id')
                                or product_info.get('seller_id')
                                or '').strip() or None
        result['shop_name'] = str(seller.get('name') or '').strip() or None
        result['image_urls'] = self._extract_image_urls(product_info)
        skus = [s for s in product_info.get('skus') or []
                if isinstance(s, dict)]
        result['sku_count'] = len(skus) or None
        result['stock_status'] = self._extract_stock_status(product_info)

        missing = [f for f, v in result.items() if v is None]
        if missing:
            logger.info(f"Shop PDP incomplete fields: {missing}")
        return result
