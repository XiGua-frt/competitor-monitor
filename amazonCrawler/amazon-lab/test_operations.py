import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent))
from collector.repository import Repository, profile_key
from collector.conditions import load_conditions
from collector.reporting import export_report
from collector.scheduler import Scheduler
from collector.pipeline import Pipeline
from test_pipeline import FakeAdapter


class OperationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.repo=Repository(self.root/'state.sqlite3');self.now=1791450000.0
        self.conditions=load_conditions()
        self.runtime={'keyword':'custom pet portrait shirt','search_interval_seconds':86400,'refresh_seconds':86400,
          'max_requests_per_run':4,'max_tasks_per_run':4,'max_requests_per_hour':8,'max_requests_per_day':12,
          'min_interval_seconds':15,'cooldown_seconds':3600,'max_consecutive_errors':3,'max_attempts':2,'retry_base_seconds':60}
        self.client=FakeAdapter()
        self.pipeline=Pipeline(self.repo,self.conditions,self.runtime,self.root/'runs',self.root/'evidence',clock=lambda:self.now,sleep=self.sleep)
        self.scheduler=Scheduler(self.pipeline)
    def tearDown(self):self.repo.close();self.tmp.cleanup()
    def sleep(self,seconds):self.now+=seconds

    def test_scheduler_idle_tick_does_not_create_run_or_client(self):
        self.scheduler.tick(lambda:self.client,mode='simulation')
        run_count=self.repo.db.execute('SELECT COUNT(*) FROM runs').fetchone()[0]
        result=self.scheduler.tick(lambda:self.fail('idle tick initialized client'),mode='simulation')
        self.assertEqual(result['status'],'idle')
        self.assertEqual(self.repo.db.execute('SELECT COUNT(*) FROM runs').fetchone()[0],run_count)

    def test_scheduler_pause_does_not_advance_schedule(self):
        self.repo.pause('manual')
        result=self.scheduler.tick(lambda:self.fail('paused tick initialized client'))
        self.assertEqual(result['status'],'paused')
        self.assertEqual(self.repo.tasks(),[])

    def test_report_contains_latest_history_quality_and_usage(self):
        self.scheduler.tick(lambda:self.client,mode='simulation')
        result=export_report(self.repo,self.root/'reports',self.now)
        self.assertEqual(result['snapshot_count'],2)
        self.assertEqual(result['usage']['day'],3)
        self.assertIn('price',result['missing_field_rates'])
        self.assertTrue((self.root/'reports/REPORT.md').exists())
        self.assertTrue((self.root/'reports/history.json').exists())

    def test_resuming_does_not_clear_cooldown_or_usage(self):
        self.repo.set_state('guard',{'paused':True,'cooldown_until':self.now+100,'next_request_at':self.now+15})
        self.repo.resume()
        self.assertEqual(self.repo.state('guard')['cooldown_until'],self.now+100)
        self.assertEqual(self.scheduler.tick(lambda:self.fail('cooldown bypassed'))['status'],'cooldown')

    def test_config_change_cancels_old_tasks_before_request(self):
        self.repo.enqueue('detail','B000000001','old-profile',{'asin':'B000000001'},self.now)
        self.pipeline.once(lambda:self.client,refresh_only=True)
        self.assertEqual(self.client.calls,[])
        self.assertEqual(self.repo.tasks()[0]['reason'],'configuration_changed')


if __name__=='__main__':unittest.main()
