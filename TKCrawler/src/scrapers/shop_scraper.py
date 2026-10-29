"""ShopProductScraper: TikTok Shop product detail via shop.tiktok.com PDP.

Channel basis (live-verified, docs/probe/shop_retest_2026-10-08.md):
- Entry URL: https://shop.tiktok.com/view/product/<id>?region=<REGION>.
  The old www.tiktok.com/shop/product/{id} template is dead (302 -> www
  homepage, probe R3).
- Login state (SessionManager cookies) is a HARD requirement: the same URL
  without cookies returns an argus "Security Check" challenge page (probe
  R9 control). Missing login raises SessionExpiredException before any
  request is issued.
- Full chrome headers required: CurlCffiClient defaults already carry the
  sec-ch-ua family + Sec-Fetch-* (see curl_client.py NOTE 2026-10-08); this
  scraper overrides the navigation-specific Sec-Fetch-* values and adds
  Referer per request (probe R8 recipe).
- Response: 200 SSR with <script id="__MODERN_ROUTER_DATA__">; a "Security
  Check" 200 body raises WAFBlockedException (defense in depth — the real
  CurlCffiClient body heuristic already catches it, but mocked/injected
  clients bypass that check).
- Region: an hk store-country account can access MY; US products return a
  200 shell with structured error_code 23002002 (probe R10) -> logged and
  surfaced as None.
- Caliber warning (口径): sold_count is "items sold globally since listed",
  exposed as sales_total_global. It must NEVER be mapped into sales_30d.

All request paths go through BaseScraper.fetch (rate limiting, daily quota,
429/403 soft cooldown). No real network calls are made in tests — the
client is injected/mocked.
"""

import os
import re
from typing import List, Optional

from bs4 import BeautifulSoup

from ..anti_detection.session_manager import SessionManager
from ..common.config import config
from ..common.exceptions import (
    RateLimitException,
    SessionExpiredException,
    WAFBlockedException,
)
from ..common.logger import get_logger
from .base_scraper import BaseScraper
from .parsers.product_parser import ProductParser, parse_sales_range_text
from .parsers.shop_parser import ShopPageParser, is_security_check_page

logger = get_logger(__name__)

# Verified entry template (probe R5/R8, docs/probe/shop_retest_2026-10-08.md).
# selectors.json urls.product_page still holds the dead www template; that
# file is outside this module's ownership, so the verified template lives here.
SHOP_PDP_URL_TEMPLATE = (
    'https://shop.tiktok.com/view/product/{product_id}?region={region}'
)

# Per-request navigation headers on top of CurlCffiClient defaults.
# Verified R8 recipe: full chrome131 + document-navigation Sec-Fetch-* +
# www.tiktok.com Referer (missing any -> argus "Security Check").
SHOP_PAGE_HEADERS = {
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'cross-site',
    'Referer': 'https://www.tiktok.com/',
}

# Default region query param. Probe R10: hk-account accessible site is MY;
# US products are region-blocked for this account (23002002).
DEFAULT_REGION = 'MY'


class ShopProductScraper(BaseScraper):
    """Scrapes TikTok Shop product detail pages (shop.tiktok.com channel)."""

    RESOURCE_TYPE = 'products'  # daily quota bucket

    def __init__(self, client=None, session_manager: SessionManager = None,
                 region: str = None, **kwargs):
        """Args mirror BaseScraper; `client` and `session_manager` are
        injectable for tests.

        Args:
            client: HTTP client exposing request(url, method, headers, ...).
            session_manager: Login-state cookie source. Defaults to
                SessionManager(data/session.json).
            region: TikTok Shop site code for the region query param
                (defaults to SHOP_REGION env or 'MY').
        """
        super().__init__(client=client, **kwargs)
        self.parser = ProductParser()  # keeps selectors.json category specs
        self.shop_parser = ShopPageParser()
        self.session_manager = session_manager or SessionManager()
        self.region = (
            region or os.getenv('SHOP_REGION') or DEFAULT_REGION
        ).upper()

    # ------------------------------------------------------------------
    # Sales range parsing (legacy pure function, kept for compat)
    # ------------------------------------------------------------------
    @staticmethod
    def parse_sales_range(text: str) -> int:
        """Convert a front-end sales label to a median integer.

        Examples:
            "500-1k" -> 750   (median of the range)
            "10k+"   -> 10000 (floor of the open-ended range)
            "1.2k"   -> 1200  (plain suffixed number)

        Contract: empty string / None / unparseable input returns 0
        (never raises). Note: the verified shop channel reports
        sales_total_global instead; this helper stays for offline parsing
        of legacy front-end labels.
        """
        return parse_sales_range_text(text)

    # ------------------------------------------------------------------
    # URL building
    # ------------------------------------------------------------------
    def _product_url(self, product_id: str) -> str:
        """Build the verified shop.tiktok.com PDP entry URL.

        The entry URL 301s to the canonical slug URL
        /{region}/pdp/{slug}/{id} when the slug is missing; the HTTP
        client follows redirects and the slug page carries the full SSR
        data (probe R7/R8).
        """
        return SHOP_PDP_URL_TEMPLATE.format(
            product_id=product_id, region=self.region)

    # ------------------------------------------------------------------
    # Login state
    # ------------------------------------------------------------------
    def _cookie_header(self) -> str:
        """Serialize cookies into a Cookie header.

        优先级: client cookie jar (池模式 inject_cookie_to_client 注入的
        租约身份) > SessionManager 文件 session (data/session.json)。

        Raises:
            SessionExpiredException: no usable login session — the PDP
                channel is useless without it (probe R9: Security Check).
        """
        # 池模式: client cookie jar 由 integration 层注入租约账号身份
        # (get_client_cookies 兼容 AntiDetectionClient 包装结构)
        from ..pools.integration import get_client_cookies
        client_cookies = get_client_cookies(self.client)
        if client_cookies:
            logger.info(f'cookie 来源: 池租约注入 ({len(client_cookies)} 条)')
            return '; '.join(f'{k}={v}' for k, v in client_cookies.items())
        try:
            cookies = self.session_manager.get_cookies()
        except SessionExpiredException:
            raise SessionExpiredException(
                "Shop 商品页通道需要登录态 cookie "
                "(data/session.json 缺失或已过期)。"
                "请先运行 scripts/login_tiktok.py 登录后再采集 "
                "(探针 R9 对照: 无 cookie → Security Check 挑战页)"
            )
        return '; '.join(f'{k}={v}' for k, v in cookies.items())

    # ------------------------------------------------------------------
    # Product detail page
    # ------------------------------------------------------------------
    def scrape_product(self, product_id: str) -> Optional[dict]:
        """Fetch and parse one shop.tiktok.com product page into the
        15-field dict (SHOP_FIELDS).

        Returns:
            dict with the 15 SHOP_FIELDS keys, or None when the page is a
            shell / the product does not exist for this account region
            (23002002 logged explicitly) / network error (logged, batch
            keeps running).

        Raises:
            SessionExpiredException: no login cookies available.
            QuotaExceededException: daily product quota exhausted.
            RateLimitException: still in soft cooldown, or 429 received.
            WAFBlockedException: 403/418/503 received, or the response body
                is an argus "Security Check" challenge page.
        """
        # Daily quota gate (DAILY_QUOTA_PRODUCTS=200 via common config)
        self.check_quota(self.RESOURCE_TYPE, config.DAILY_QUOTA_PRODUCTS)

        cookie_header = self._cookie_header()

        url = self._product_url(product_id)
        logger.info(
            f"Scraping product {product_id} (region={self.region}) -> {url}"
        )

        headers = dict(SHOP_PAGE_HEADERS)
        headers['Cookie'] = cookie_header

        try:
            response = self.fetch(url, headers=headers)
        except (RateLimitException, WAFBlockedException):
            # fetch() already armed the soft cooldown; let it propagate
            raise
        except Exception as exc:
            # Network-level errors are logged and surfaced as None so batch
            # jobs keep running.
            logger.error(f"Network error scraping product {product_id}: {exc}")
            return None

        status = getattr(response, 'status_code', None)
        if status == 404:
            logger.warning(f"Product {product_id} returned 404, skipping")
            return None
        if status != 200:
            logger.warning(
                f"Product {product_id} returned unexpected status {status}"
            )
            return None

        html = response.text

        # Defense in depth: argus returns the challenge page with status
        # 200 when cookies/headers degrade (probe R2/R9). The real
        # CurlCffiClient body heuristic already raises WAFBlockedException;
        # this catches injected/mocked clients and heuristic gaps.
        if is_security_check_page(html):
            logger.warning(
                f"Product {product_id}: argus 'Security Check' challenge "
                f"page (login cookies or chrome headers degraded)"
            )
            raise WAFBlockedException(url, 200)

        data = self.shop_parser.parse(html)
        if data is None:
            # No router data / region error / blocked page. Fall back to the
            # legacy selectors.json parser (assumption-based channel, kept
            # for pages that still embed __UNIVERSAL_DATA_FOR_REHYDRATION__ /
            # SIGI_STATE). Its 15-field contract (sales_30d caliber) applies
            # only to that legacy path.
            data = self._parse_legacy(html)
            if data is None:
                logger.warning(
                    f"Product {product_id}: no parseable embedded data "
                    f"(shell page / region error / blocked)"
                )
                return None

        # product_id from the URL wins if page data disagrees/missing
        if not data.get('product_id'):
            data['product_id'] = str(product_id)

        missing = [f for f, v in data.items() if v is None]
        if missing:
            logger.info(f"Product {product_id} incomplete fields: {missing}")
        if 'sales_total_global' in data:
            logger.info(
                f"Product {product_id}: "
                f"sales_total_global={data.get('sales_total_global')} "
                f"price_current={data.get('price_current')}"
            )
        return data

    def _parse_legacy(self, html: str) -> Optional[dict]:
        """Legacy fallback: selectors.json-based parse of old-format pages.

        Returns the legacy 15-field dict (REQUIRED_FIELDS contract) or
        None. Logs clearly when the legacy (unverified) channel was used.
        """
        data = self.parser.parse(html)
        if data is not None:
            logger.info("Parsed via legacy selectors.json channel "
                        "(no __MODERN_ROUTER_DATA__ found)")
        return data

    # ------------------------------------------------------------------
    # Category ranking page
    # ------------------------------------------------------------------
    def scrape_category(self, category_url: str) -> List[str]:
        """Scrape a category ranking page and return product IDs.

        NOTE (unverified channel): the category ranking page structure is
        NOT covered by the 2026-10-08 probe; product-ID extraction still
        follows the selectors.json category_page spec (assumption-based,
        same as before this rewrite). The old www.tiktok.com/shop/*
        category templates 302 to the homepage (probe R3), so callers must
        supply a working shop.tiktok.com category URL.

        Returns:
            List of product ID strings (de-duplicated, page order kept).
            Empty list when the page is empty / 404 / no product links
            (logged, never crashes).

        Raises:
            RateLimitException / WAFBlockedException: same contract as
                scrape_product.
        """
        logger.info(f"Scraping category ranking: {category_url}")

        try:
            response = self.fetch(category_url)
        except (RateLimitException, WAFBlockedException):
            raise
        except Exception as exc:
            logger.error(f"Network error scraping category {category_url}: {exc}")
            return []

        status = getattr(response, 'status_code', None)
        if status == 404:
            logger.warning(f"Category page 404: {category_url}")
            return []
        if status != 200:
            logger.warning(f"Category page unexpected status {status}: {category_url}")
            return []

        if is_security_check_page(response.text):
            logger.warning(
                f"Category page blocked by argus 'Security Check': "
                f"{category_url}"
            )
            raise WAFBlockedException(category_url, 200)

        html = response.text
        soup = BeautifulSoup(html, 'lxml')
        product_ids = self._extract_product_ids(soup, html)

        if not product_ids:
            logger.warning(f"No product links found on category page: {category_url}")
        else:
            logger.info(f"Category {category_url}: {len(product_ids)} products")
        return product_ids

    def _extract_product_ids(self, soup: BeautifulSoup, html: str) -> List[str]:
        """Extract product IDs from a category page.

        Primary: CSS selector from selectors.json (category_page.product_link
        + product_id_regex on the href). Fallback: scan every <a href> with
        the same regex. Both paths are assumptions pending real-page
        calibration.
        """
        category_selectors = self.parser.selectors['category_page']
        link_spec = category_selectors['product_link']
        regex_spec = category_selectors['product_id_regex']
        pattern = re.compile(regex_spec['value'])

        ids: List[str] = []
        seen = set()

        def _collect(href: str):
            m = pattern.search(href or '')
            if m:
                pid = m.group(1)
                if pid not in seen:
                    seen.add(pid)
                    ids.append(pid)

        # Primary CSS path
        for tag in soup.select(link_spec['value']):
            _collect(tag.get(link_spec.get('attribute', 'href'), ''))

        # XPath-style fallback: any anchor whose href mentions /product/
        if not ids:
            for tag in soup.find_all('a', href=True):
                _collect(tag['href'])

        return ids
