"""Replay saved pages through all six steps, with simulated time and HTTP only."""
import json
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from collector.amzpy_adapter import AmzPyAdapter
from collector.conditions import load_conditions
from collector.runtime import load_runtime
from collector.repository import Repository, profile_key
from collector.pipeline import Pipeline
from collector.scheduler import Scheduler
from collector.reporting import export_report

ROOT=Path(__file__).resolve().parent


class SavedAdapter(AmzPyAdapter):
    def __init__(self,source,keyword):
        self.source=Path(source)
        self.keyword=keyword
        self.calls=[]
        self.next_status=200
        self.listings=json.loads((self.source/'search-results.json').read_text(encoding='utf-8'))
        self.original={r['external_id']:r['fetched_at'] for r in json.loads((self.source/'posts.json').read_text(encoding='utf-8'))}

    def fetch(self,url):
        self.calls.append(url)
        status=self.next_status
        self.next_status=200
        asin=url.split('/dp/')[1].split('?')[0] if '/dp/' in url else None
        if asin:
            html=(self.source/(asin+'.html')).read_text(encoding='utf-8')
        else:
            html='<html lang="en-US"><div id="glow-ingress-line2">New York 10001</div><input id="currencyOfPreference" value="USD"></html>'
        return SimpleNamespace(status_code=status,url=url,text=html,headers={})

    def parse_search(self,response):
        return self.listings

    def parse_detail(self,response,listing,conditions):
        row=super().parse_detail(response,listing,conditions)
        if row:
            row['evidence'].update(simulation=True,replayed_source_observed_at=self.original[row['external_id']])
        return row

    def close(self):
        pass


def demo():
    output=ROOT.parent/'test/amazon/amzpy/six-step-demo'/uuid.uuid4().hex[:12]
    output.mkdir(parents=True,exist_ok=True)
    runtime=load_runtime()
    conditions=load_conditions()
    repo=Repository(output/'demo.sqlite3')
    clock=[time.time()]
    def sleep(seconds):clock[0]+=seconds
    client=SavedAdapter(ROOT.parent/'test/amazon/amzpy/us-preferences-fixed',runtime['keyword'])
    pipeline=Pipeline(repo,conditions,runtime,output/'runs',output/'evidence',clock=lambda:clock[0],sleep=sleep)
    scheduler=Scheduler(pipeline)
    first=scheduler.tick(lambda:client,mode='simulation')
    assert first['snapshots_saved']==3 and first['requests']==4
    second=scheduler.tick(lambda:client,mode='simulation')
    assert second['status']=='idle' and second['requests']==0
    clock[0]+=86401
    refreshed=scheduler.tick(lambda:client,mode='simulation')
    assert refreshed['snapshots_saved']==3 and len(repo.snapshots())==6
    client.next_status=429
    repo.enqueue('search',runtime['keyword'],profile_key(conditions),{'keyword':runtime['keyword']},clock[0])
    cooled=pipeline.once(lambda:client,mode='simulation')
    assert cooled['status']=='cooldown' and cooled['requests']==1
    calls_before_restart=len(client.calls)
    repo.close()
    repo=Repository(output/'demo.sqlite3')
    repo.resume()
    pipeline=Pipeline(repo,conditions,runtime,output/'runs',output/'evidence',clock=lambda:clock[0],sleep=sleep)
    restarted=Scheduler(pipeline).tick(lambda:client,mode='simulation')
    assert restarted['status']=='cooldown' and len(client.calls)==calls_before_restart
    report=export_report(repo,output/'reports',clock[0])
    repo.close()
    result={'mode':'simulation_only','actual_network_requests':0,
            'initial_run':{'requests_simulated':first['requests'],'snapshots':first['snapshots_saved']},
            'cached_second_tick':second,'refresh_snapshots_added':refreshed['snapshots_saved'],
            'historical_snapshots':report['snapshot_count'],
            'http_429_status':cooled['status'],'restart_and_resume_status':restarted['status'],
            'report':str((output/'reports/REPORT.md').resolve()),'database':str((output/'demo.sqlite3').resolve())}
    (output/'VALIDATION.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    (output.parent/'LATEST.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result
