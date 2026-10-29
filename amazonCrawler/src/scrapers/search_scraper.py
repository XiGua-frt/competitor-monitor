"""SearchScraper — 关键词搜索排名采集 → keyword_rank_snapshots 结构。"""

from datetime import date

try:
    from src.common.logger import get_logger
    logger = get_logger(__name__)
except ImportError:  # pragma: no cover
    import logging
    logger = logging.getLogger(__name__)

from .base_scraper import BaseScraper
from .pdp_scraper import MARKETPLACE_DOMAINS
from .parsers.search_parser import SearchParser


class SearchScraper(BaseScraper):
    """关键词搜索采集器，产物可直接 save_snapshot('keyword_rank_snapshots')。"""

    def __init__(self, client=None, parser=None, domain: str = None, **kw):
        super().__init__(client=client, **kw)
        self.parser = parser if parser is not None else SearchParser()
        self.domain = domain

    def _domain(self, marketplace: str) -> str:
        if self.domain:
            return self.domain
        import os
        env = os.getenv('MARKETPLACE')
        if env:
            return env
        return MARKETPLACE_DOMAINS.get(marketplace.upper(),
                                       MARKETPLACE_DOMAINS['US'])

    def build_url(self, keyword: str, page: int = 1,
                  marketplace: str = 'US') -> str:
        from urllib.parse import quote_plus
        return (f'https://{self._domain(marketplace)}/s?k={quote_plus(keyword)}'
                f'&page={page}')

    def search(self, keyword: str, page: int = 1,
               marketplace: str = 'US') -> list:
        """搜索关键词，返回 keyword_rank_snapshots 字段结构的 list[dict]。

        每条：{keyword, asin, snapshot_date, natural_rank, ad_rank?, page}。
        """
        url = self.build_url(keyword, page, marketplace)
        logger.info('Searching %r page=%s (%s)', keyword, page, marketplace)
        self.check_quota('search')
        response = self.fetch(url)
        html = getattr(response, 'text', '') or ''
        items = self.parser.parse(html)
        today = date.today().isoformat()
        for item in items:
            item['keyword'] = keyword
            item['snapshot_date'] = today
            item['page'] = page
            # keyword_rank_snapshots 口径：natural_rank/ad_rank 缺省置 None
            item.setdefault('natural_rank', None)
            item.setdefault('ad_rank', None)
        return items
