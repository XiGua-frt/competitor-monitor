import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parent))
from collector.repository import Repository, profile_key
from collector.pipeline import Pipeline
from collector.conditions import load_conditions, check_page
from collector.relevance import classify_product


class FakeAdapter:
    def __init__(self):
        self.calls=[]
        self.next_status=200
        self.listings=[{'asin':'B000000001','title':'Custom Pet Portrait Shirt'},
                       {'asin':'B000000002','title':'Custom Photo Shirt'},
                       {'asin':'B000000003','title':'Custom Pet Photo Mug'}]
    def fetch(self,url):
        self.calls.append(url)
        asin=url.split('/dp/')[1].split('?')[0] if '/dp/' in url else None
        html='<html lang="en-US"><div id="glow-ingress-line2">10001</div><input id="currencyOfPreference" value="USD">'
        if asin:html+='<input id="ASIN" value="'+asin+'"><span id="productTitle">Custom Pet Portrait Shirt</span>'
        html+='</html>'
        status=self.next_status
        self.next_status=200
        return SimpleNamespace(status_code=status,url=url,text=html,headers={})
    def parse_search(self,response):return self.listings
    def parse_detail(self,response,listing,conditions):
        asin=listing['asin']
        return {'external_id':asin,'title':'Custom Pet Portrait Shirt','source_url':'https://www.amazon.com/dp/'+asin,
                'commerce':{'price':12.95,'currency':'USD','rating_count':5},
                'collection_conditions':check_page(response.text,response.url,asin,conditions,displayed_currency='USD'),
                'relevance':classify_product('Custom Pet Portrait Shirt',stage='detail'),
                'search':{'selected_result_position':listing.get('_search_result_position')}}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.repo=Repository(self.root/'state.sqlite3')
        self.now=1791450000.0
        self.conditions=load_conditions()
        self.runtime={'keyword':'custom pet portrait shirt','search_interval_seconds':86400,
          'refresh_seconds':86400,'max_requests_per_run':4,'max_requests_per_hour':8,'max_requests_per_day':12,
          'min_interval_seconds':15,'cooldown_seconds':3600,'max_consecutive_errors':3,
          'max_attempts':2,'retry_base_seconds':60,'max_tasks_per_run':4}
        self.client=FakeAdapter()
        self.pipeline=Pipeline(self.repo,self.conditions,self.runtime,self.root/'runs',self.root/'evidence',
                               clock=lambda:self.now,sleep=self.sleep)
    def tearDown(self):self.repo.close();self.tmp.cleanup()
    def sleep(self,seconds):self.now+=seconds
    def once(self,**kwargs):return self.pipeline.once(lambda:self.client,**kwargs)

    def test_search_to_snapshots_and_second_run_uses_cache(self):
        first=self.once()
        self.assertEqual(first['snapshots_saved'],2)
        self.assertEqual(len(self.client.calls),3)
        self.assertEqual(len(self.repo.products()),2)
        self.assertEqual(self.once()['requests'],0)
        self.assertEqual(len(self.client.calls),3)

    def test_refresh_without_repeating_search_when_due(self):
        self.once();self.now+=86401
        calls=len(self.client.calls)
        result=self.once(refresh_only=True)
        self.assertEqual(result['snapshots_saved'],2)
        self.assertEqual(len(self.client.calls)-calls,2)
        self.assertTrue(all('/dp/' in url for url in self.client.calls[calls:]))
        self.assertEqual(len(self.repo.snapshots()),4)

    def test_pause_prevents_factory_creation(self):
        self.repo.pause('manual')
        def factory():self.fail('paused worker initialized client')
        report=self.pipeline.once(factory)
        self.assertEqual(report['status'],'paused')
        self.assertEqual(report['requests'],0)

    def test_429_stops_batch_and_survives_reopen(self):
        self.client.next_status=429
        first=self.once()
        self.assertEqual(first['status'],'cooldown')
        self.repo.close();self.repo=Repository(self.root/'state.sqlite3')
        pipeline=Pipeline(self.repo,self.conditions,self.runtime,self.root/'runs',self.root/'evidence',clock=lambda:self.now,sleep=self.sleep)
        second=pipeline.once(lambda:self.client)
        self.assertEqual(second['requests'],0)
        self.assertEqual(len(self.client.calls),1)

    def test_transient_error_retried_at_persistent_due_time(self):
        self.client.next_status=503
        first=self.once()
        self.assertEqual(first['requests'],1)
        task=self.repo.tasks()[0]
        self.assertEqual(task['state'],'pending')
        self.assertEqual(task['attempts'],1)
        self.assertGreater(task['due_at'],self.now)
        self.now+=61
        self.assertEqual(self.once()['snapshots_saved'],2)

    def test_two_workers_do_not_overlap(self):
        self.repo.acquire_worker('other',self.now)
        self.assertEqual(self.once()['status'],'worker_busy')
        self.assertEqual(self.client.calls,[])

    def test_worker_takeover_during_fetch_stops_old_worker(self):
        fetch=self.client.fetch
        def takeover(url):
            response=fetch(url)
            self.now+=301
            self.assertTrue(self.repo.acquire_worker('new-worker',self.now))
            return response
        self.client.fetch=takeover
        result=self.once()
        self.assertEqual(result['status'],'worker_lease_lost')
        self.assertEqual(len(self.client.calls),1)
        self.assertEqual(len(self.repo.snapshots()),0)

    def test_worker_takeover_during_parse_does_not_pause_new_worker(self):
        self.once()
        self.now+=86401
        parse=self.client.parse_detail
        def takeover(*args):
            row=parse(*args)
            self.now+=301
            self.repo.acquire_worker('new-worker',self.now)
            return row
        self.client.parse_detail=takeover
        result=self.once(refresh_only=True)
        self.assertEqual(result['status'],'worker_lease_lost')
        self.assertFalse(self.repo.state('guard',{}).get('paused'))
        self.assertEqual(len(self.repo.snapshots()),2)

    def test_request_budget_leaves_pending_tasks_for_next_run(self):
        self.runtime['max_requests_per_run']=2
        result=self.once()
        self.assertEqual(result['requests'],2)
        self.assertTrue(any(t['state']=='pending' for t in self.repo.tasks()))
        self.assertEqual(self.once()['snapshots_saved'],1)

    def test_failed_parse_preserves_prior_snapshots(self):
        self.once();count=len(self.repo.snapshots())
        self.now+=86401
        self.client.parse_detail=lambda *args:None
        result=self.once(refresh_only=True)
        self.assertEqual(result['status'],'paused')
        self.assertEqual(len(self.repo.snapshots()),count)

    def test_exhausted_retry_does_not_create_new_task_each_tick(self):
        self.repo.discover('B000000001',profile_key(self.conditions,'B000000001'),{},self.now)
        self.client.next_status=503
        self.once(refresh_only=True);self.now+=61
        self.client.next_status=503
        self.once(refresh_only=True)
        self.assertEqual(self.repo.tasks()[0]['state'],'failed')
        count=len(self.repo.tasks())
        self.now+=1
        self.once(refresh_only=True)
        self.assertEqual(len(self.repo.tasks()),count)
        self.assertEqual(len(self.client.calls),2)

    def test_permanent_detail_error_not_requeued_immediately(self):
        self.client.listings=[{'asin':'B000000001','title':'Custom Pet Portrait Shirt'}]
        fetch=self.client.fetch
        def missing(url):
            response=fetch(url)
            if '/dp/' in url:response.status_code=404
            return response
        self.client.fetch=missing
        self.once()
        count=len(self.repo.tasks());calls=len(self.client.calls)
        self.once()
        self.assertEqual(len(self.repo.tasks()),count)
        self.assertEqual(len(self.client.calls),calls)


if __name__=='__main__':unittest.main()
