"""Persistent single-worker pipeline; scheduling never bypasses request controls."""
import hashlib
import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from .conditions import require_region
from .guard import RequestGuard, Blocked, CollectionError
from .metrics import enrich_commerce
from .repository import profile_key, LeaseLost
from .relevance import prepare_candidates


class Pipeline:
    def __init__(self,repo,conditions,runtime,run_dir,evidence_dir,clock=time.time,sleep=time.sleep):
        self.repo,self.conditions,self.runtime=repo,conditions,runtime
        self.run_dir,self.evidence_dir=Path(run_dir),Path(evidence_dir)
        self.clock,self.sleep=clock,sleep
        self.guard=RequestGuard(repo,conditions,runtime,clock,sleep)

    def plan(self,refresh_only=False):
        self.guard.ensure_active()
        now=self.clock()
        if not refresh_only:
            self.repo.schedule_search(self.runtime['keyword'],profile_key(self.conditions),now,self.runtime['search_interval_seconds'])
        asins={p['asin'] for p in self.repo.products()} | set(self.conditions['tracked_variants'])
        for asin in sorted(asins):
            profile=profile_key(self.conditions,asin)
            latest=self.repo.latest(asin,profile)
            if latest and latest.get('relevance',{}).get('status')=='unrelated':
                continue
            if not self.repo.is_fresh(asin,profile,now,self.runtime['refresh_seconds']):
                self.repo.enqueue('detail',asin,profile,{'asin':asin,'title':latest.get('title') if latest else None},now)

    def _url(self,task):
        if task['kind']=='search':
            return 'https://'+self.conditions['marketplace']+'/s?'+urlencode({'k':task['payload']['keyword'],'language':'en_US','mr_donotredirect':''})
        if not re.fullmatch(r'[A-Z0-9]{10}',task['entity']):
            raise CollectionError('invalid_task_asin')
        return 'https://'+self.conditions['marketplace']+'/dp/'+task['entity']+'?mr_donotredirect'

    def _evidence_fetch(self,client,task,run_id):
        def fetch(url):
            response=client.fetch(url)
            directory=self.evidence_dir/run_id
            directory.mkdir(parents=True,exist_ok=True)
            name=task['id']+'-'+uuid.uuid4().hex[:8]
            html_path=directory/(name+'.html')
            html_path.write_text(response.text,encoding='utf-8')
            self.last_evidence={'html_file':str(html_path.resolve()),
                                'sha256':hashlib.sha256(response.text.encode()).hexdigest(),
                                'http_status':response.status_code,'response_url':response.url}
            (directory/(name+'.json')).write_text(json.dumps(self.last_evidence,ensure_ascii=False,indent=2),encoding='utf-8')
            return response
        return fetch

    def once(self,client_factory,refresh_only=False,mode='live'):
        run_id=uuid.uuid4().hex
        summary={'run_id':run_id,'mode':mode,'status':'completed','requests':0,
                 'snapshots_saved':0,'tasks_done':0,'errors':[], 'started_at':datetime.fromtimestamp(self.clock(),timezone.utc).isoformat()}
        self.repo.start_run(run_id,self.clock(),mode)
        owner='worker:'+run_id
        locked=self.repo.acquire_worker(owner,self.clock())
        task=None
        client=None
        try:
            if not locked:
                summary['status']='worker_busy'
                return summary
            self.plan(refresh_only)
            for _ in range(self.runtime['max_tasks_per_run']):
                self.guard.ensure_active()
                if not self.repo.acquire_worker(owner,self.clock()):
                    raise Blocked('worker_lease_lost')
                task=self.repo.claim(owner,self.clock())
                if task is None:
                    break
                expected_profile=profile_key(self.conditions,task['entity'] if task['kind']=='detail' else None)
                if task['profile']!=expected_profile or (task['kind']=='search' and task['entity']!=self.runtime['keyword']):
                    self.repo.finish_task(task,'failed','configuration_changed')
                    task=None
                    continue
                if task['kind']=='detail' and summary['snapshots_saved']>=self.runtime.get('max_details_per_run',3):
                    self.repo.finish_task(task,'pending','detail_budget',self.clock())
                    task=None
                    summary['status']='detail_budget'
                    break
                if task['attempts']>=self.runtime['max_attempts']:
                    self.repo.finish_task(task,'failed','attempts_exhausted',self.clock()+self.runtime['refresh_seconds'])
                    task=None
                    continue
                # Recheck queued details: another completed observation may have made them fresh.
                if task['kind']=='detail' and self.repo.is_fresh(task['entity'],task['profile'],self.clock(),self.runtime['refresh_seconds']):
                    self.repo.finish_task(task,'done','cache_hit')
                    summary['tasks_done']+=1
                    task=None
                    continue
                try:
                    if client is None:
                        try:
                            client=client_factory()
                        except Exception:
                            raise self.guard.error('session_or_client_unavailable',pause=True)
                    response=self.guard.request(self._url(task),self._evidence_fetch(client,task,run_id),run_id,
                                                task,task['entity'] if task['kind']=='detail' else None)
                    if not self.repo.valid_leases(task,self.clock()):
                        summary['status']='worker_lease_lost'
                        task=None
                        break
                    observed_at=datetime.fromtimestamp(self.clock(),timezone.utc).isoformat()
                    if task['kind']=='search':
                        listings=client.parse_search(response)
                        if not self.repo.valid_leases(task,self.clock()):
                            raise LeaseLost('worker_lease_lost')
                        if not listings:
                            raise self.guard.error('search_structure_unverified',pause=True)
                        records,_=prepare_candidates(listings,task['entity'],limit=0)
                        for record,item in zip(records,listings):
                            record['evidence']=self.last_evidence
                            if record['decision'] in ('skip_invalid_asin','skip_duplicate','skip_unrelated'):
                                continue
                            asin=record['asin']
                            profile=profile_key(self.conditions,asin)
                            self.repo.discover(asin,profile,{'title':item.get('title'),'relevance':record['relevance']},self.clock())
                            if self.repo.is_fresh(asin,profile,self.clock(),self.runtime['refresh_seconds']):
                                record['decision']='cache_hit'
                            else:
                                queued=self.repo.enqueue('detail',asin,profile,{**item,'_search_result_position':record['returned_position']},self.clock())
                                record['decision']='queued_detail' if queued else 'retry_hold'
                        self.repo.save_discoveries(task['id'],records)
                        self.repo.finish_task(task,now=self.clock())
                    else:
                        row=client.parse_detail(response,task['payload'],self.conditions)
                        if not row or not row.get('title') or row.get('external_id')!=task['entity']:
                            raise self.guard.error('detail_structure_unverified',pause=True)
                        try:
                            require_region(row['collection_conditions'])
                        except ValueError as error:
                            raise self.guard.error(str(error),pause=True)
                        row['fetched_at']=observed_at
                        row['commerce']=enrich_commerce(row.get('commerce',{}))
                        row.setdefault('evidence',{}).update(self.last_evidence)
                        self.repo.complete_snapshot(task,row,run_id,self.clock())
                        summary['snapshots_saved']+=1
                    summary['tasks_done']+=1
                    task=None
                except Blocked as error:
                    if error.reason=='worker_lease_lost':
                        summary['status']=error.reason
                        task=None
                        break
                    self.repo.finish_task(task,'pending',error.reason,error.until or self.clock())
                    task=None
                    summary['status']=error.reason
                    break
                except CollectionError as error:
                    summary['errors'].append({'task_id':task['id'],'reason':error.reason})
                    state=self.repo.state('guard',{})
                    if state.get('paused') or state.get('cooldown_until',0)>self.clock():
                        self.repo.finish_task(task,'pending',error.reason,state.get('cooldown_until',self.clock()))
                        summary['status']='paused' if state.get('paused') else 'cooldown'
                        task=None
                        break
                    if error.retryable and task['attempts']<self.runtime['max_attempts']:
                        self.repo.finish_task(task,'pending',error.reason,self.clock()+self.runtime['retry_base_seconds']*2**max(0,task['attempts']-1))
                    else:
                        self.repo.finish_task(task,'failed',error.reason,self.clock()+self.runtime['refresh_seconds'])
                    task=None
        except Blocked as error:
            summary['status']=error.reason
        except LeaseLost:
            summary['status']='worker_lease_lost'
        except KeyboardInterrupt:
            summary['status']='interrupted'
        except Exception as error:
            self.repo.pause('internal_error:'+type(error).__name__)
            summary['status']='paused'
            summary['errors'].append({'reason':'internal_error:'+type(error).__name__})
        finally:
            if task is not None:
                if self.repo.valid_leases(task,self.clock()):
                    self.repo.finish_task(task,'pending','interrupted',self.clock())
            if locked:
                self.repo.release_worker(owner)
            if client is not None and hasattr(client,'close'):
                try:
                    client.close()
                except Exception:
                    summary['errors'].append({'reason':'client_cleanup_failed'})
            summary['requests']=self.repo.request_counts(self.clock(),run_id)['run']
            summary['finished_at']=datetime.fromtimestamp(self.clock(),timezone.utc).isoformat()
            self.repo.finish_run(run_id,self.clock(),summary)
            self.run_dir.mkdir(parents=True,exist_ok=True)
            (self.run_dir/(run_id+'.json')).write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
        return summary
