"""OfferParser 单测 — 构造的最小 offer 列表页 fixture。

结构基于公开页面常见标记（#olpOfferList / .olpOffer / olpPriceColumn /
olpSellerColumn / "Ships from Amazon"），待实测校准。
"""

from decimal import Decimal
from pathlib import Path

import pytest

from src.scrapers.parsers.offer_parser import ParsingException, OfferParser

FIXTURES = Path(__file__).parent / 'fixtures' / 'amazon'


@pytest.fixture(scope='module')
def parser():
    return OfferParser()


@pytest.fixture(scope='module')
def offers(parser):
    return parser.parse((FIXTURES / 'offer_sample.html').read_text('utf-8'))


def test_parses_three_offers(offers):
    assert len(offers) == 3


def test_first_offer_is_buybox(offers):
    assert offers[0]['is_buybox'] is True
    assert offers[1]['is_buybox'] is False
    assert offers[2]['is_buybox'] is False


def test_seller_ids(offers):
    assert offers[0]['seller_id'] == 'A21T9VMSLBQ6TP'
    assert offers[0]['seller_name'] == 'Just Customized'
    assert offers[1]['seller_id'] == 'A358NHPDS1ZMEA'
    assert offers[1]['seller_name'] == 'EmbrolyStore'


def test_amazon_row_without_link(offers):
    third = offers[2]
    assert third['seller_id'] is None
    assert 'Amazon.com' in third['seller_name']
    assert third['price'] == Decimal('16.99')


def test_prices(offers):
    assert offers[0]['price'] == Decimal('14.99')
    assert offers[1]['price'] == Decimal('15.49')


def test_is_amazon_offered(offers):
    assert offers[0]['is_amazon_offered'] is True   # Ships from Amazon
    assert offers[1]['is_amazon_offered'] is False
    assert offers[2]['is_amazon_offered'] is True   # Sold by Amazon.com


def test_empty_html_raises(parser):
    with pytest.raises(ParsingException):
        parser.parse('')
