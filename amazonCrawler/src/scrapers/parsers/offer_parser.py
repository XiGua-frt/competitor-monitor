"""Offer 列表页（所有卖家）解析器 — Agent C (scrapers 层)。

注意：暂无真实 offer 页 HTML 样本，本解析器基于 Amazon 公开 offer 列表页的
常见标记（#olpOfferList / .olpOffer / .olpSellerColumn / .olpPriceColumn /
"Ships from Amazon" / "Sold by Amazon"）做最佳努力实现，结构待实测校准
（见 tests/fixtures/amazon/offer_sample.html 注释）。
"""

import logging
import re
from decimal import Decimal, InvalidOperation

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

try:
    from src.common.exceptions import ParsingException
except ImportError:  # pragma: no cover
    class ParsingException(Exception):
        def __init__(self, field: str = "", reason: str = ""):
            self.field = field
            self.reason = reason
            super().__init__(f"Failed to parse field '{field}': {reason}")


class OfferParser:
    """Offer 列表页 HTML -> [{seller_id?, seller_name, price, is_buybox,
    is_amazon_offered}, ...]（按 DOM 顺序，第一条默认视为 buybox）。"""

    def parse(self, html: str) -> list:
        if not html or not html.strip():
            raise ParsingException(field='html', reason='empty html')
        soup = BeautifulSoup(html, 'lxml')

        rows = soup.select('#olpOfferList .olpOffer, ol.olpOfferList .olpOffer, '
                           'div.olpOffer')
        offers = []
        seen = set()
        for idx, row in enumerate(rows):
            seller_name, seller_id = self._parse_seller(row)
            price = self._parse_price(row)
            if seller_name is None and price is None:
                continue  # 空行/分隔行
            key = (seller_id, str(price))
            if key in seen:
                continue
            seen.add(key)
            offers.append({
                'seller_id': seller_id,
                'seller_name': seller_name,
                'price': price,
                'is_buybox': idx == 0,   # offer 列表第一条 = buybox winner
                'is_amazon_offered': self._is_amazon_offered(row),
            })

        if not offers:
            logger.debug('offer parser: no .olpOffer rows found')
        return offers

    @staticmethod
    def _parse_seller(row):
        """返回 (seller_name, seller_id)。

        - seller_id: 卖家链接 /gp/seller/...&seller=XXXX 或
          at-a-glance.html?seller=XXXX
        - seller_name: 链接文本；无链接时列文本（如 "Amazon.com"）
        """
        link = row.select_one(
            '.olpSellerColumn a[href*="seller="], a[href*="seller="]')
        if link:
            name = link.get_text(strip=True) or None
            m = re.search(r'[?&]seller=([A-Z0-9]+)', link.get('href', ''))
            seller_id = m.group(1) if m else None
            return name, seller_id
        # 无链接：纯文本列（Amazon 自营常见）
        col = row.select_one('.olpSellerColumn')
        if col:
            text = col.get_text(' ', strip=True)
            if text:
                return text, None
        return None, None

    @staticmethod
    def _parse_price(row):
        tag = row.select_one('.olpPriceColumn .a-price .a-offscreen') or \
            row.select_one('.olpPriceColumn .a-offscreen') or \
            row.select_one('.olpPriceColumn .olpOfferPrice') or \
            row.select_one('.olpPriceColumn')
        if not tag:
            return None
        text = tag.get_text(strip=True)
        m = re.search(r'[\d,]+(?:\.\d+)?', text.replace(',', ''))
        if not m:
            return None
        try:
            return Decimal(m.group(0))
        except InvalidOperation:
            logger.debug('offer price parse failed: %r', text)
            return None

    @staticmethod
    def _is_amazon_offered(row) -> bool:
        text = row.get_text(' ', strip=True).lower()
        return ('ships from amazon' in text or 'sold by amazon' in text
                or 'fulfilled by amazon' in text)
