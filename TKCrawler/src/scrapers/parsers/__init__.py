"""Parsers subpackage for scrapers."""

from .product_parser import ProductParser, parse_number_with_suffix
from .shop_parser import SHOP_FIELDS, ShopPageParser

__all__ = [
    'ProductParser',
    'parse_number_with_suffix',
    'ShopPageParser',
    'SHOP_FIELDS',
]
