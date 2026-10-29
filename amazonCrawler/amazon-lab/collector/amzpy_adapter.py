"""Reuse local AmzPy transport/parsers without its automatic request/retry loop."""
import contextlib
import io
import json

from amzpy import AmazonScraper
from amzpy.parser import parse_product_page, parse_search_page
from .conditions import session_path


class AmzPyAdapter:
    def __init__(self,conditions,runtime):
        path=session_path(conditions)
        state=json.loads(path.read_text(encoding='utf-8'))
        cookies=[c for c in state.get('cookies',[]) if c.get('domain','').lstrip('.')=='amazon.com']
        if not cookies:
            raise ValueError('preferences_session_empty')
        with contextlib.redirect_stdout(io.StringIO()):
            scraper=AmazonScraper(country_code=conditions['country_code'],MAX_RETRIES=0,
                                  DELAY_BETWEEN_REQUESTS=(0,0),REQUEST_TIMEOUT=runtime['request_timeout_seconds'])
        self.transport=scraper.session.session
        self.runtime=runtime
        self.keyword=runtime['keyword']
        for cookie in cookies:
            self.transport.cookies.set(cookie['name'],cookie['value'],domain=cookie['domain'],path=cookie.get('path','/'))
        self.transport.cookies.set('i18n-prefs',conditions['currency'],domain='.amazon.com',path='/')
        self.transport.cookies.set('lc-main',conditions['language'].replace('-','_'),domain='.amazon.com',path='/')
        self.transport.headers['Accept-Language']='en-US,en;q=0.9'

    def fetch(self,url):
        return self.transport.get(url,timeout=self.runtime['request_timeout_seconds'],allow_redirects=False)

    def parse_search(self,response):
        with contextlib.redirect_stdout(io.StringIO()):
            return parse_search_page(response.text,'https://www.amazon.com',country_code='com')

    def parse_detail(self,response,listing,conditions):
        from probe import normalize
        with contextlib.redirect_stdout(io.StringIO()):
            product=parse_product_page(response.text,response.url,country_code='com')
        if not product:
            return None
        return normalize(product,listing,response.text,listing.get('_search_result_position'),response.url,
                         conditions_config=conditions,keyword=self.keyword)

    def close(self):
        self.transport.close()
