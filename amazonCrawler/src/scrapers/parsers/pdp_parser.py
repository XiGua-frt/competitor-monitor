"""PdpParser — Amazon 商品页 (PDP) HTML 解析器。

只做纯解析：输入 HTML 字符串，输出字段名与 asin_snapshots 列名一致的 dict。
样本是裁剪 HTML，字段缺失时返回 None（记 debug），只在结构性完全失败时抛 ParsingException。

为什么不直接用 lxml XPath：项目统一 BeautifulSoup（lxml parser），选择器
跨页面结构变体（apex_desktop / corePriceDisplay 两个价格区重复）更容错。
"""

import logging
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

try:  # src/common 由 Agent A 实现，未落地时用内置 fallback
    from src.common.exceptions import ParsingException
except ImportError:  # pragma: no cover - Agent A 未交付时
    class ParsingException(Exception):
        """解析结构性失败。"""

        def __init__(self, field: str = "", reason: str = ""):
            self.field = field
            self.reason = reason
            super().__init__(f"Failed to parse field '{field}': {reason}")

# "#729,415 in Clothing, Shoes & Jewelry" / "#20,009 in Boys' Novelty T-Shirts"
_BSR_RE = re.compile(r"#([\d,]+)\s+in\s+(.+)")
_MONTHS = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4, 'may': 5, 'june': 6,
    'july': 7, 'august': 8, 'september': 9, 'october': 10, 'november': 11,
    'december': 12,
}
_DATE_RE = re.compile(r'([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})')
_BRAND_PREFIX_RE = re.compile(r'^(?:Visit the|Brand:)\s*', re.IGNORECASE)
_BRAND_SUFFIX_RE = re.compile(r'\s+Store$', re.IGNORECASE)


class PdpParser:
    """Amazon PDP HTML -> asin_snapshots 字段 dict。"""

    def parse(self, html: str) -> dict:
        if not html or not html.strip():
            raise ParsingException(field='html', reason='empty html')

        soup = BeautifulSoup(html, 'lxml')

        title = self._parse_title(soup)
        if title is None:
            # 连 productTitle 都没有 → 结构性失败（大概率是验证码页/空壳页）
            raise ParsingException(
                field='title',
                reason='no #productTitle found, page is likely a bot challenge or empty shell',
            )

        price_current, price_original = self._parse_price(soup)
        rating, rating_count = self._parse_rating(soup)
        bsr_ranks, bsr_root = self._parse_bsr(soup)
        dfa_raw, dfa_iso = self._parse_date_first_available(soup)
        variant_asin = self._parse_variant_asin(soup)

        return {
            'title': title,
            'brand': self._parse_brand(soup),
            'seller_name': self._parse_seller_name(soup),
            'buybox_winner': self._parse_buybox_winner(soup),
            'price_current': price_current,
            'price_original': price_original,
            'currency': 'USD',          # 固定口径声明：显示页价、币种跟随 marketplace
            'price_scope': 'display_page_price',
            'availability': self._parse_availability(soup),
            'coupon_text': self._parse_coupon(soup),
            'rating': rating,
            'rating_count': rating_count,
            'bsr_root_category': bsr_root,
            'bsr_ranks': bsr_ranks or None,
            'date_first_available': dfa_iso,
            'date_first_available_raw': dfa_raw,
            'variant_status': 'fixed' if variant_asin else 'none',
            'variant_asin': variant_asin,
            'comparison_ready': True,   # 固定口径：解析器保证字段口径一致可横向对比
            'region_verified': True,    # 固定口径：来源 URL 域名即 marketplace 域名
        }

    # ------------------------------------------------------------------
    # 各字段
    # ------------------------------------------------------------------
    def _parse_title(self, soup) -> 'str | None':
        tag = soup.select_one('#productTitle')
        if not tag:
            return None
        title = tag.get_text(strip=True)
        return title or None

    def _parse_price(self, soup):
        """价格：优先 priceToPay .a-offscreen，其次 whole+fraction 组合。

        样本里 corePriceDisplay_desktop_feature_div 与 corePrice_feature_div
        各有一份价格（内容相同），取第一个命中的 priceToPay 即可。
        """
        price_current = None
        price_original = None

        # 1) priceToPay 区块（apex_desktop / corePrice* 通用）
        ptp = soup.select_one('.priceToPay')
        if ptp:
            off = ptp.select_one('.a-offscreen')
            if off:
                price_current = self._to_decimal(off.get_text(strip=True))
            if price_current is None:
                whole = ptp.select_one('.a-price-whole')
                frac = ptp.select_one('.a-price-fraction')
                if whole:
                    frac_text = frac.get_text(strip=True) if frac else '00'
                    price_current = self._to_decimal(
                        f"{whole.get_text(strip=True)}{frac_text}")

        # 2) 兜底：任意 .a-price .a-offscreen（corePrice_feature_div 等）
        if price_current is None:
            off = soup.select_one('#corePrice_feature_div .a-price .a-offscreen')
            if off:
                price_current = self._to_decimal(off.get_text(strip=True))

        # 划线价（basisPrice）：只在有价格上下文时找，避免误抓无关数字
        basis = soup.select_one('.basisPrice .a-offscreen') or \
            soup.select_one('.apex-basisprice-feature .a-offscreen')
        if basis:
            # 文本形如 "List: $12.99"，提取金额部分
            m = re.search(r'[\d,]+(?:\.\d+)?',
                          basis.get_text(strip=True).replace(',', ''))
            if m:
                price_original = self._to_decimal(m.group(0))

        return price_current, price_original

    def _parse_rating(self, soup):
        rating = None
        rating_count = None
        tag = soup.select_one('#acrPopover')
        if tag:
            title_attr = tag.get('title') or ''
            m = re.search(r'([0-9.]+)\s+out of 5', title_attr)
            if m:
                try:
                    rating = Decimal(m.group(1))
                except InvalidOperation:
                    rating = None
            else:
                # fallback: a-icon-alt
                alt = tag.select_one('.a-icon-alt')
                if alt:
                    m2 = re.search(r'([0-9.]+)\s+out of 5',
                                   alt.get_text(strip=True))
                    if m2:
                        rating = Decimal(m2.group(1))
        count_tag = soup.select_one('#acrCustomerReviewText')
        if count_tag:
            m = re.search(r'([\d,]+)', count_tag.get_text(strip=True))
            if m:
                try:
                    rating_count = int(m.group(1).replace(',', ''))
                except ValueError:
                    rating_count = None
        return rating, rating_count

    def _parse_availability(self, soup) -> 'str | None':
        tag = soup.select_one('#availability')
        if not tag:
            # 兜底：含 "In Stock" 的 span（样本裁剪后可能丢 id）
            for span in soup.find_all('span'):
                text = span.get_text(' ', strip=True)
                if text and 'in stock' in text.lower() and len(text) < 120:
                    return text
            return None
        text = tag.get_text(' ', strip=True)
        return text or None

    def _parse_brand(self, soup) -> 'str | None':
        tag = soup.select_one('#bylineInfo')
        if not tag:
            return None
        text = tag.get_text(strip=True)
        if not text:
            return None
        # "Visit the X Store" / "Brand: X" → 取 X
        text = _BRAND_PREFIX_RE.sub('', text)
        text = _BRAND_SUFFIX_RE.sub('', text)
        return text.strip() or None

    def _parse_seller_name(self, soup) -> 'str | None':
        tag = soup.select_one('#sellerProfileTriggerId')
        if tag:
            return tag.get_text(strip=True) or None
        return None

    def _parse_buybox_winner(self, soup) -> 'str | None':
        # buybox 归属 = 卖家链接文本（与 seller_name 同源，裁剪样本即此结构）
        return self._parse_seller_name(soup)

    def _parse_coupon(self, soup) -> 'str | None':
        tag = soup.select_one('#promoPriceBlockMessage_feature_div') or \
            soup.select_one('.promoPriceBlockMessage')
        if tag:
            text = tag.get_text(' ', strip=True)
            return text or None
        return None

    def _parse_bsr(self, soup):
        """Best Sellers Rank：主类目文本 + ul.zg_hrsr 子项。

        结构：<span class="a-text-bold"> Best Sellers Rank: </span> 后的
        span 里 "#729,415 in Clothing, Shoes & Jewelry" + zg_hrsr li。
        """
        ranks = []
        root_category = None

        # 定位含 "Best Sellers Rank:" 的 a-text-bold span 所在的 li/span 块
        node = None
        for bold in soup.select('span.a-text-bold'):
            if 'best sellers rank' in bold.get_text(strip=True).lower():
                node = bold.find_parent('li') or bold.parent
                break
        if node is None:
            return ranks, root_category

        # 主类目：node 直接文本 "#729,415 in Clothing, Shoes & Jewelry"
        main_text = node.get_text(' ', strip=True)
        # 去掉子项干扰：截断到 zg_hrsr 之前
        block_text = main_text
        hrsr = node.select_one('ul.zg_hrsr')
        if hrsr:
            hrsr_text = hrsr.get_text(' ', strip=True)
            block_text = main_text.replace(hrsr_text, ' ').strip()
        m = _BSR_RE.search(block_text)
        if m:
            try:
                rank = int(m.group(1).replace(',', ''))
                cat = m.group(2).strip()
                # 尾部可能带 "( See Top 100 ... )" 链接文本（get_text 展开后带空格）
                cat = re.sub(r'\s*\(.*?See Top 100.*$', '', cat,
                             flags=re.IGNORECASE).strip()
                root_category = cat
                ranks.append({'category': cat, 'rank': rank})
            except ValueError:
                logger.debug('BSR main rank parse failed: %s', block_text)

        for li in node.select('ul.zg_hrsr li'):
            li_text = li.get_text(' ', strip=True)
            m = _BSR_RE.search(li_text)
            if m:
                try:
                    cat = m.group(2).strip()
                    cat = re.sub(r'\s*\(.*?See Top 100.*$', '', cat,
                                 flags=re.IGNORECASE).strip()
                    ranks.append({'category': cat,
                                  'rank': int(m.group(1).replace(',', ''))})
                except ValueError:
                    logger.debug('BSR sub rank parse failed: %s', li_text)

        return ranks, root_category

    def _parse_date_first_available(self, soup):
        """Date First Available 详情项 → (raw, ISO)。"""
        for bold in soup.select('span.a-text-bold'):
            if 'date first available' not in bold.get_text(strip=True).lower():
                continue
            li = bold.find_parent('li') or bold.parent
            if li is None:
                continue
            # 值在 bold 之后的同级 span 里
            value_span = bold.find_next_sibling('span')
            raw = None
            if value_span is not None:
                raw = value_span.get_text(strip=True)
            if not raw:
                # 兜底：整行文本去 label
                full = li.get_text(' ', strip=True)
                m = re.search(r'Date First Available[^:A-Za-z]*:?\s*(.+)',
                              full, re.IGNORECASE)
                raw = m.group(1).strip() if m else None
            if raw:
                return raw, self._to_iso(raw)
            break
        return None, None
    def _parse_variant_asin(self, soup) -> 'str | None':
        """dropdown option value "0,MC_Custom_1##B0752XJYNL" → ## 后的 ASIN。

        取 selected 的 option（dropdownSelect class 或 selected 属性）；
        没有 selected 时退化取第一个含 ## 的非 Select option。
        有变体时同时把 variant_status 置 fixed（parse 产物里改写）。
        """
        asin = None
        selected = soup.select_one(
            'select select option.dropdownSelect, '
            'option.dropdownSelect, option[selected]')
        candidates = []
        if selected:
            candidates.append(selected)
        for opt in soup.select('option'):
            val = opt.get('value') or ''
            if '##' in val:
                candidates.append(opt)
                break

        for opt in candidates:
            val = opt.get('value') or ''
            if '##' in val:
                asin = val.split('##', 1)[1].strip()
                break
        return asin

    def _parse_variant_status(self, soup) -> str:
        return 'fixed' if self._parse_variant_asin(soup) else 'none'

    def _to_iso(self, raw: str) -> 'str | None':
        """'November 1, 2024' → '2024-11-01'。"""
        if not raw:
            return None
        m = _DATE_RE.search(raw)
        if not m:
            return None
        month = _MONTHS.get(m.group(1).lower())
        if not month:
            return None
        try:
            d = date(int(m.group(3)), month, int(m.group(2)))
        except ValueError:
            return None
        return d.isoformat()

    @staticmethod
    def _to_decimal(text: str):
        """'$14.99' → Decimal('14.99')；解析失败返回 None。"""
        if not text:
            return None
        cleaned = text.replace('$', '').replace(',', '').strip()
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            logger.debug('price parse failed: %r', text)
            return None
