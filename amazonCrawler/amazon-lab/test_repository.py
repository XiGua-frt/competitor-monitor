import json
import sys
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent))
from collector.repository import Repository, profile_key


def sample(asin='B000000001', observed_at='2026-10-08T10:20:00+00:00', price=12.95):
    return {'external_id':asin, 'title':'Custom Pet Photo Shirt', 'fetched_at':observed_at,
            'commerce':{'price':price,'currency':'USD','rating_count':5},
            'collection_conditions':{'region_verified':True,'comparison_ready':False},
            'relevance':{'status':'related'}}


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'state.sqlite3'
        self.repo = Repository(self.path)
        self.profile = 'test-profile'
    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_open_tasks_deduplicate_across_restarts(self):
        first = self.repo.enqueue('detail','B000000001',self.profile,{'asin':'B000000001'},100)
        self.repo.close()
        self.repo = Repository(self.path)
        second = self.repo.enqueue('detail','B000000001',self.profile,{'asin':'B000000001'},101)
        self.assertEqual(first, second)
        self.assertEqual(len(self.repo.tasks()),1)

    def test_task_lease_recovery_without_duplicate_snapshot(self):
        task_id = self.repo.enqueue('detail','B000000001',self.profile,{},100)
        task = self.repo.claim('worker1',100,lease_seconds=30)
        self.assertEqual(task['id'],task_id)
        self.assertIsNone(self.repo.claim('worker2',110))
        recovered = self.repo.claim('worker2',131)
        self.assertEqual(recovered['id'],task_id)
        self.repo.complete_snapshot(recovered, sample(), 'run2', 131)
        self.assertEqual(len(self.repo.snapshots()),1)
        self.assertIsNone(self.repo.claim('worker3',132))

    def test_stale_owner_cannot_complete_reclaimed_task(self):
        self.repo.enqueue('detail','B000000001',self.profile,{},100)
        old = self.repo.claim('old',100,lease_seconds=1)
        self.repo.claim('new',102)
        with self.assertRaises(ValueError):
            self.repo.complete_snapshot(old,sample(),'old-run',103)
        self.assertEqual(self.repo.snapshots(),[])

    def test_import_idempotence_and_same_day_history(self):
        self.repo.save_snapshot(sample(),self.profile,'historical:a','import',100)
        self.repo.save_snapshot(sample(),self.profile,'historical:a','import',101)
        self.repo.save_snapshot(sample(observed_at='2026-10-08T11:20:00+00:00',price=14),
                                self.profile,'historical:b','import',102)
        self.assertEqual(len(self.repo.snapshots()),2)
        self.assertEqual(self.repo.latest('B000000001',self.profile)['commerce']['price'],14)

    def test_failed_or_older_observation_does_not_replace_latest(self):
        self.repo.save_snapshot(sample(),self.profile,'one','run',100)
        self.repo.save_snapshot(sample(observed_at='2026-10-07T10:20:00+00:00',price=1),
                                self.profile,'older','run',101)
        self.assertEqual(self.repo.latest('B000000001',self.profile)['commerce']['price'],12.95)

    def test_missing_price_retained_with_last_valid_price_timestamp(self):
        self.repo.save_snapshot(sample(),self.profile,'one','run',100)
        self.repo.save_snapshot(sample(observed_at='2026-10-09T10:20:00+00:00',price=None),
                                self.profile,'two','run',101)
        latest = self.repo.latest('B000000001',self.profile)
        self.assertIsNone(latest['commerce']['price'])
        value = self.repo.last_valid_price('B000000001',self.profile)
        self.assertEqual(value['price'],12.95)
        self.assertEqual(value['observed_at'],'2026-10-08T10:20:00+00:00')

    def test_cached_product_refresh_only_when_expired(self):
        self.repo.save_snapshot(sample(observed_at=datetime.fromtimestamp(100,timezone.utc).isoformat()),self.profile,'one','run',100)
        self.assertTrue(self.repo.is_fresh('B000000001',self.profile,178,refresh_seconds=100))
        self.assertFalse(self.repo.is_fresh('B000000001',self.profile,200,refresh_seconds=100))

    def test_profile_separates_variant_and_region_but_not_keyword(self):
        a={'marketplace':'www.amazon.com','delivery_postcode':'10001','currency':'USD',
           'language':'en-US','price_scope':'display','variant_policy':'fixed','tracked_variants':{}}
        self.assertEqual(profile_key(a,'B000000001'),profile_key({**a,'keyword':'other'},'B000000001'))
        self.assertNotEqual(profile_key(a,'B000000001'),profile_key({**a,'delivery_postcode':'10002'},'B000000001'))
        b={**a,'tracked_variants':{'B000000001':{'asin':'B000000001','size':'Small','color':'Black'}}}
        self.assertNotEqual(profile_key(a,'B000000001'),profile_key(b,'B000000001'))

    def test_worker_lock_persists_and_expires(self):
        self.assertTrue(self.repo.acquire_worker('one',100,lease_seconds=30))
        self.assertFalse(self.repo.acquire_worker('two',110))
        self.assertTrue(self.repo.acquire_worker('two',131))
        self.repo.release_worker('one')
        self.assertFalse(self.repo.acquire_worker('three',132))

    def test_scheduler_records_due_time_without_backlog_burst(self):
        self.assertTrue(self.repo.scheduler_due('keyword',100,interval=60))
        self.assertFalse(self.repo.scheduler_due('keyword',101,interval=60))
        self.assertTrue(self.repo.scheduler_due('keyword',10000,interval=60))
        self.assertFalse(self.repo.scheduler_due('keyword',10001,interval=60))


if __name__ == '__main__':
    unittest.main()
