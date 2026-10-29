"""解析器子包：纯 HTML -> dict/list，不发请求、不依赖 HTTP 客户端。"""

from .pdp_parser import PdpParser
from .search_parser import SearchParser
from .offer_parser import OfferParser

__all__ = ['PdpParser', 'SearchParser', 'OfferParser']
