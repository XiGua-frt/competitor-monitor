"""PdpParser 单测 — 三个真实裁剪 fixture 全解析断言。"""

import re
from decimal import Decimal
from pathlib import Path

import pytest

from src.scrapers.parsers.pdp_parser import ParsingException, PdpParser

FIXTURES = Path(__file__).parent / 'fixtures' / 'amazon'


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding='utf-8')


@pytest.fixture(scope='module')
def parser():
    return PdpParser()


class TestB0DLQ8MVF7:
    """主样本：任务卡点名的精确断言。"""

    @pytest.fixture(scope='class')
    def data(self, parser):
        return parser.parse(load('B0DLQ8MVF7.html'))

    def test_title(self, data):
        assert data['title'].startswith('Custom Best Friend')
        assert "Pet Photo T-Shirt" in data['title']

    def test_price(self, data):
        assert data['price_current'] == Decimal('14.99')
        assert data['price_original'] is None  # 样本无划线价

    def test_rating(self, data):
        assert data['rating'] == Decimal('4.6')
        assert data['rating_count'] == 104

    def test_bsr(self, data):
        assert data['bsr_ranks'][0]['rank'] == 729415
        assert data['bsr_ranks'][0]['category'] == 'Clothing, Shoes & Jewelry'
        assert data['bsr_root_category'] == 'Clothing, Shoes & Jewelry'
        # zg_hrsr 子项也解析进来
        cats = [r['category'] for r in data['bsr_ranks']]
        assert "Boys' Novelty T-Shirts" in cats
        assert {'category': "Women's Novelty T-Shirts",
                'rank': 31092} in data['bsr_ranks']

    def test_date_first_available(self, data):
        assert data['date_first_available'] == '2024-11-01'
        assert data['date_first_available_raw'] == 'November 1, 2024'

    def test_availability(self, data):
        assert data['availability'] is not None
        assert 'In Stock' in data['availability']

    def test_brand(self, data):
        assert data['brand'] == 'Modify by Amazon Merch on Demand'

    def test_variant(self, data):
        # selected option "0,MC_Custom_1##B0752XJYNL"
        assert data['variant_asin'] == 'B0752XJYNL'
        assert data['variant_status'] == 'fixed'

    def test_fixed_scope_fields(self, data):
        assert data['comparison_ready'] is True
        assert data['region_verified'] is True
        assert data['price_scope'] == 'display_page_price'
        assert data['currency'] == 'USD'


class TestB0GWVTMR4Q:
    @pytest.fixture(scope='class')
    def data(self, parser):
        return parser.parse(load('B0GWVTMR4Q.html'))

    def test_title(self, data):
        assert isinstance(data['title'], str) and data['title']
        assert 'Custom Embroidered Dog Mom' in data['title']

    def test_price(self, data):
        assert data['price_current'] == Decimal('12.95')

    def test_rating(self, data):
        assert data['rating'] == Decimal('5.0')
        assert data['rating_count'] == 5

    def test_bsr(self, data):
        assert data['bsr_ranks'][0]['rank'] == 56481
        assert data['bsr_root_category'] == 'Handmade Products'

    def test_date(self, data):
        assert data['date_first_available'] == '2026-04-13'

    def test_seller(self, data):
        assert data['seller_name'] == 'EmbrolyStore'
        assert data['buybox_winner'] == 'EmbrolyStore'

    def test_variant_single(self, data):
        # 该样本无变体 dropdown（单变体/无 twister），variant_asin 应为 None
        assert data['variant_asin'] is None


class TestB0H22J6F1B:
    @pytest.fixture(scope='class')
    def data(self, parser):
        return parser.parse(load('B0H22J6F1B.html'))

    def test_title(self, data):
        assert isinstance(data['title'], str) and data['title']
        assert 'Custom T Shirts' in data['title']

    def test_price(self, data):
        assert data['price_current'] == Decimal('24.99')

    def test_rating(self, data):
        assert data['rating'] == Decimal('5.0')
        assert data['rating_count'] == 2

    def test_bsr(self, data):
        assert data['bsr_ranks'][0]['rank'] == 1318004
        assert data['bsr_root_category'] == 'Clothing, Shoes & Jewelry'

    def test_date(self, data):
        assert data['date_first_available'] == '2026-05-16'

    def test_seller(self, data):
        assert data['seller_name'] == 'Just Customized'


class TestRobustness:
    def test_empty_html_raises(self, parser):
        with pytest.raises(ParsingException):
            parser.parse('')
        with pytest.raises(ParsingException):
            parser.parse('   ')

    def test_structural_failure_raises(self, parser):
        # 无 productTitle 的空壳页 → 结构性失败
        with pytest.raises(ParsingException):
            parser.parse('<html><body><p>Robot Check</p></body></html>')

    def test_partial_page_returns_nones(self, parser):
        # 只有标题的裁剪页：其余字段 None 不抛异常
        html = ('<html><body>'
                '<span id="productTitle">  Only Title Here  </span>'
                '</body></html>')
        data = parser.parse(html)
        assert data['title'] == 'Only Title Here'
        assert data['price_current'] is None
        assert data['rating'] is None
        assert data['rating_count'] is None
        assert data['bsr_ranks'] is None
        assert data['bsr_root_category'] is None
        assert data['date_first_available'] is None
        assert data['variant_asin'] is None

    def test_price_whole_fraction_fallback(self, parser):
        # 无 a-offscreen 时用 whole+fraction 组合
        html = ('<html><body><span id="productTitle">T</span>'
                '<span class="priceToPay"><span class="a-price-whole">9'
                '<span class="a-price-decimal">.</span></span>'
                '<span class="a-price-fraction">49</span></span>'
                '</body></html>')
        data = parser.parse(html)
        assert data['price_current'] == Decimal('9.49')

    def test_basis_price_original(self, parser):
        html = ('<html><body><span id="productTitle">T</span>'
                '<span class="priceToPay"><span class="a-offscreen">$9.99'
                '</span></span>'
                '<span class="basisPrice"><span class="a-offscreen">'
                'List: $12.99</span></span></body></html>')
        data = parser.parse(html)
        assert data['price_current'] == Decimal('9.99')
        assert data['price_original'] == Decimal('12.99')
