"""SearchParser 单测 — 构造的最小搜索结果页 fixture。

结构基于公开页面常见标记（div[data-asin] / sp-sponsored-result / a-price），
待实测校准。
"""

from decimal import Decimal
from pathlib import Path

import pytest

from src.scrapers.parsers.search_parser import ParsingException, SearchParser

FIXTURES = Path(__file__).parent / 'fixtures' / 'amazon'


@pytest.fixture(scope='module')
def parser():
    return SearchParser()


@pytest.fixture(scope='module')
def items(parser):
    return parser.parse((FIXTURES / 'search_sample.html').read_text('utf-8'))


def test_parses_three_items(items):
    assert len(items) == 3
    assert [i['asin'] for i in items] == [
        'B0AAAAAAAA', 'B0BBBBBBBB', 'B0CCCCCCCC']


def test_natural_rank_numbering_skips_ads(items):
    # 自然位按 DOM 顺序编号，广告不占自然位
    assert items[0]['natural_rank'] == 1
    assert items[2]['natural_rank'] == 2
    assert 'ad_rank' not in items[0]
    assert 'ad_rank' not in items[2]


def test_ad_rank_marking(items):
    ad = items[1]
    assert ad['ad_rank'] == 1
    assert 'natural_rank' not in ad


def test_item_extra_fields(items):
    first = items[0]
    assert first['title'] == 'Natural Result One Organic Cotton Tee'
    assert first['price'] == Decimal('19.99')
    assert first['rating'] == Decimal('4.5')
    assert first['rating_count'] == 1024


def test_item_without_rating(items):
    third = items[2]
    assert 'rating' not in third
    assert third['price'] == Decimal('13.25')


def test_empty_asin_card_skipped(items):
    # data-asin="" 的占位卡片不产出条目
    assert all(i['asin'] for i in items)


def test_empty_html_raises(parser):
    with pytest.raises(ParsingException):
        parser.parse('')
