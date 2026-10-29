"""OfferScraper — 商品全部卖家 offer 采集 → offer_snapshots 结构。"""

from datetime import date

try:
    from src.common.logger import get_logger
    logger = get_logger(__name__)
except ImportError:  # pragma: no cover
    import logging
    logger = logging.getLogger(__name__)

from .base_scraper import BaseScraper
from .pdp_scraper import MARKETPLACE_DOMAINS
from .parsers.offer_parser import OfferParser


class OfferScraper(BaseScraper):
    """Offer 列表采集器，产物可直接 save_snapshot('offer_snapshots')。"""

    def __init__(self, client=None, parser=None, domain: str = None, **kw):
        super().__init__(client=client, **kw)
        self.parser = parser if parser is not None else OfferParser()
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

    def build_url(self, asin: str, marketplace: str = 'US') -> str:
        # fulfillment=all 拿全部卖家（含非 FBA）；条件=New
        return (f'https://{self._domain(marketplace)}/gp/offer-listing/{asin}'
                f'?condition=new&fulfillment=all')

    def scrape_offers(self, asin: str, marketplace: str = 'US') -> list:
        """采集 ASIN 的全部 offer，返回 offer_snapshots 字段结构 list[dict]。

        每条：{asin, seller_id, seller_name, price, is_buybox,
        is_amazon_offered, snapshot_date}。
        """
        url = self.build_url(asin, marketplace)
        logger.info('Scraping offers for %s (%s)', asin, marketplace)
        self.check_quota('asin')
        response = self.fetch(url)
        html = getattr(response, 'text', '') or ''
        offers = self.parser.parse(html)
        today = date.today().isoformat()
        for offer in offers:
            offer['asin'] = asin
            offer['snapshot_date'] = today
            offer.setdefault('seller_id', None)
        return offers
