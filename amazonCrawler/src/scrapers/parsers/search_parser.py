"""搜索结果页解析器 — Agent C (scrapers 层)。

注意：暂无真实搜索结果页 HTML 样本，本解析器基于 Amazon 公开搜索页的常见
标记（div[data-asin] / data-component-type="sp-sponsored-result" /
a-price .a-offscreen / a-icon-alt 星级）做最佳努力实现，
结构待实测校准（见 tests/fixtures/amazon/search_sample.html 注释）。
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


class SearchParser:
    """搜索结果页 HTML -> [{asin, natural_rank, ad_rank?, title?, price?, rating?, rating_count?}]。

    规则：
    - 结果条目 = 带 data-asin 属性的 div[data-asin]（Amazon 搜索页的标准标记）
    - natural_rank 按 DOM 顺序从 1 编号（只计自然结果）
    - Sponsored 标记 = 条目内 [data-component-type="sp-sponsored-result"]
      （兼容旧标记 .s-sponsored-label-info-icon / 文本 "Sponsored"）
    - ad_rank 单独编号，只对广告条目给出
    """

    def parse(self, html: str) -> list:
        if not html or not html.strip():
            raise ParsingException(field='html', reason='empty html')
        soup = BeautifulSoup(html, 'lxml')

        items = []
        natural_rank = 0
        ad_rank = 0
        seen = set()

        for card in soup.select('div[data-asin]'):
            asin = (card.get('data-asin') or '').strip()
            if not asin or asin in seen:
                continue
            # 跳过嵌套容器（外层 carousel 也带 data-asin 时取最深卡片即可：
            # 取 data-asin 非空的最内层 —— 简化：如果有子 div[data-asin] 则只认子级）
            if card.select_one('div[data-asin]'):
                continue
            seen.add(asin)

            is_ad = self._is_sponsored(card)
            entry = {'asin': asin}
            if is_ad:
                ad_rank += 1
                entry['ad_rank'] = ad_rank
            else:
                natural_rank += 1
                entry['natural_rank'] = natural_rank

            title_tag = card.select_one('h2 a span, h2 span, a.a-link-normal span')
            if title_tag:
                t = title_tag.get_text(strip=True)
                if t:
                    entry['title'] = t

            price = self._parse_price(card)
            if price is not None:
                entry['price'] = price

            rating_tag = card.select_one('.a-icon-alt')
            if rating_tag:
                m = re.search(r'([0-9.]+)\s+out of 5',
                              rating_tag.get_text(strip=True))
                if m:
                    entry['rating'] = Decimal(m.group(1))
            count_tag = card.select_one(
                'span[aria-label*="ratings"], a[aria-label*="ratings"]')
            if count_tag:
                m = re.search(r'([\d,]+)', count_tag.get('aria-label', ''))
                if m:
                    entry['rating_count'] = int(m.group(1).replace(',', ''))

            items.append(entry)

        if not items:
            logger.debug('search parser: no data-asin cards found')
        return items

    @staticmethod
    def _is_sponsored(card) -> bool:
        if card.select_one('[data-component-type="sp-sponsored-result"]'):
            return True
        if card.select_one('.s-sponsored-label-info-icon, .puis-sponsored-label'):
            return True
        # 兜底：条目文本开头附近出现 "Sponsored" 徽标
        first = card.select_one('span.puis-label-popover-default + span, '
                                'span.s-light-gray')
        if first and first.get_text(strip=True).lower() == 'sponsored':
            return True
        return False

    @staticmethod
    def _parse_price(card):
        tag = card.select_one('.a-price .a-offscreen') or \
            card.select_one('.a-price-whole')
        if not tag:
            return None
        text = tag.get_text(strip=True)
        if not text:
            whole = card.select_one('.a-price-whole')
            frac = card.select_one('.a-price-fraction')
            if whole:
                text = whole.get_text(strip=True) + (
                    frac.get_text(strip=True) if frac else '00')
        cleaned = text.replace('$', '').replace(',', '').strip()
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            logger.debug('search price parse failed: %r', text)
            return None
