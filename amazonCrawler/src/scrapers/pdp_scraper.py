"""PdpScraper — 商品详情采集：URL 构造 → fetch → 解析 → snapshot dict。"""

from datetime import date

try:
    from src.common.logger import get_logger
    logger = get_logger(__name__)
except ImportError:  # pragma: no cover
    import logging
    logger = logging.getLogger(__name__)

try:
    from src.common.exceptions import ParsingException
except ImportError:  # pragma: no cover
    class ParsingException(Exception):
        def __init__(self, field: str = '', reason: str = ''):
            super().__init__(f"Failed to parse field '{field}': {reason}")

from .base_scraper import BaseScraper
from .parsers.pdp_parser import PdpParser

# marketplace 代号 → 域名（可被环境变量 MARKETPLACE 覆盖默认域名）
MARKETPLACE_DOMAINS = {
    'US': 'www.amazon.com',
    'UK': 'www.amazon.co.uk',
    'DE': 'www.amazon.de',
    'JP': 'www.amazon.co.jp',
    'CA': 'www.amazon.ca',
    'FR': 'www.amazon.fr',
}


class PdpScraper(BaseScraper):
    """Amazon PDP 采集器，产物可直接 save_snapshot('asin_snapshots')。"""

    def __init__(self, client=None, parser=None, domain: str = None, **kw):
        super().__init__(client=client, **kw)
        self.parser = parser if parser is not None else PdpParser()
        self.domain = domain

    def _domain_for(self, marketplace: str) -> str:
        if self.domain:
            return self.domain
        import os
        env = os.getenv('MARKETPLACE')
        if env:
            return env
        return MARKETPLACE_DOMAINS.get(marketplace.upper(),
                                       MARKETPLACE_DOMAINS['US'])

    def build_url(self, asin: str, marketplace: str = 'US') -> str:
        return f'https://{self._domain_for(marketplace)}/dp/{asin}'

    def scrape_product(self, asin: str, marketplace: str = 'US') -> dict:
        """采集单个 ASIN，返回字段名 = asin_snapshots 列名的 dict。

        Raises:
            BotBlockedException: 503/bot 页（fetch 内部已进入软冷却）。
            ParsingException: 页面结构性失败。
        """
        url = self.build_url(asin, marketplace)
        logger.info('Scraping PDP %s (%s)', asin, marketplace)
        self.check_quota('asin')
        response = self.fetch(url)
        html = getattr(response, 'text', '') or ''
        data = self.parser.parse(html)
        data['asin'] = asin
        data['marketplace'] = marketplace.upper()
        data['snapshot_date'] = date.today().isoformat()
        return data
