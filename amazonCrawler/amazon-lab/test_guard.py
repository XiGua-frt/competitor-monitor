import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parent))
from collector.repository import Repository
from collector.guard import RequestGuard, Blocked, CollectionError
from collector.conditions import load_conditions


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'db.sqlite3'
        self.repo=Repository(self.path)
        self.now=100.0
        self.calls=[]
        self.limits={'max_requests_per_run':4,'max_requests_per_hour':5,'max_requests_per_day':6,
                     'min_interval_seconds':15,'cooldown_seconds':3600,'max_consecutive_errors':3}
        self.guard=RequestGuard(self.repo,load_conditions(),self.limits,clock=lambda:self.now,sleep=self.sleep)
    def tearDown(self):
        self.repo.close();self.tmp.cleanup()
    def sleep(self,seconds):
        self.now+=seconds
    def page(self,status=200,html=None,headers=None):
        return SimpleNamespace(status_code=status,url='https://www.amazon.com/dp/B000000001',
            text=html or '<html lang="en-US"><div id="glow-ingress-line2">10001</div><input id="currencyOfPreference" value="USD"><input id="ASIN" value="B000000001"></html>',
            headers=headers or {})
    def fetch(self,url):
        self.calls.append(url)
        return self.page()
    def request(self,fetch=None,run='run'):
        return self.guard.request('https://www.amazon.com/dp/B000000001',fetch or self.fetch,run,asin='B000000001')

    def test_budget_reserved_before_fetch_and_persists(self):
        def fetch(url):
            self.assertEqual(self.repo.request_counts(self.now)['day'],1)
            return self.page()
        self.request(fetch)
        self.repo.close();self.repo=Repository(self.path)
        self.assertEqual(self.repo.request_counts(self.now)['day'],1)

    def test_run_limit_blocks_fifth_without_fetch(self):
        for _ in range(4):self.request()
        with self.assertRaises(Blocked):self.request()
        self.assertEqual(len(self.calls),4)

    def test_hour_budget_shared_across_runs_and_profiles(self):
        for i in range(5):self.request(run=str(i))
        with self.assertRaises(Blocked) as error:self.request(run='new')
        self.assertEqual(error.exception.reason,'hour_budget_exhausted')
        self.assertEqual(len(self.calls),5)

    def test_rate_interval_persisted(self):
        self.request();self.request()
        self.assertEqual(self.now,115)

    def test_429_retry_after_survives_restart(self):
        with self.assertRaises(CollectionError):self.request(lambda url:self.page(429,headers={'Retry-After':'7200'}))
        self.repo.close();self.repo=Repository(self.path)
        guard=RequestGuard(self.repo,load_conditions(),self.limits,clock=lambda:self.now,sleep=self.sleep)
        with self.assertRaises(Blocked) as error:guard.request('https://www.amazon.com/dp/B000000001',self.fetch,'new',asin='B000000001')
        self.assertEqual(error.exception.reason,'cooldown')
        self.assertEqual(self.calls,[])
        self.assertEqual(self.repo.state('guard')['cooldown_until'],7300)

    def test_captcha_200_pauses_without_retry(self):
        html='<html><form action="/errors/validateCaptcha"><input id="captchacharacters"></form></html>'
        with self.assertRaises(CollectionError):self.request(lambda url:self.page(html=html))
        with self.assertRaises(Blocked):self.request()
        self.assertEqual(self.repo.state('guard')['pause_reason'],'verification_required')
        self.assertEqual(self.repo.request_counts(self.now)['day'],1)

    def test_region_failure_pauses(self):
        html=self.page().text.replace('10001','Japan')
        with self.assertRaises(CollectionError):self.request(lambda url:self.page(html=html))
        self.assertIn('delivery_postcode_mismatch',self.repo.state('guard')['pause_reason'])

    def test_timeout_consumes_budget_and_is_retryable(self):
        def fail(url):raise TimeoutError('not logged')
        with self.assertRaises(CollectionError) as error:self.request(fail)
        self.assertTrue(error.exception.retryable)
        self.assertEqual(self.repo.request_counts(self.now)['day'],1)

    def test_redirect_never_followed_or_treated_as_product(self):
        with self.assertRaises(CollectionError):self.request(lambda url:self.page(302))
        self.assertEqual(self.repo.state('guard')['pause_reason'],'unexpected_redirect')

    def test_wrong_domain_not_requested(self):
        with self.assertRaises(CollectionError):self.guard.request('https://www.amazon.co.jp/dp/B000000001',self.fetch,'r')
        self.assertEqual(self.repo.request_counts(self.now)['day'],0)

    def test_consecutive_network_errors_pause(self):
        def fail(url):raise TimeoutError()
        for i in range(3):
            with self.assertRaises(CollectionError):self.request(fail,run=str(i))
        self.assertEqual(self.repo.state('guard')['pause_reason'],'consecutive_errors')


if __name__=='__main__':unittest.main()
