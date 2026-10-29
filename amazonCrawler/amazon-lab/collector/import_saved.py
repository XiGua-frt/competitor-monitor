"""Idempotent migration of already-collected evidence, never a fresh request."""
import json
import time
import uuid
from pathlib import Path

from .metrics import enrich_commerce
from .repository import profile_key
from .relevance import prepare_candidates


def import_saved(repo,conditions,base,clock=time.time):
    from probe import normalize
    base=Path(base)
    source=base/'us-preferences-fixed'
    original={r['external_id']:r for r in json.loads((source/'posts.json').read_text(encoding='utf-8'))}
    products=json.loads((source/'upstream-details.json').read_text(encoding='utf-8'))
    old_report=json.loads((source/'report.json').read_text(encoding='utf-8'))
    requests={r['path']:r for r in old_report['requests']}
    listings=json.loads((base/'search-results.json').read_text(encoding='utf-8'))
    records,_=prepare_candidates(listings,'custom pet portrait shirt',limit=0)
    by_asin={r['asin']:{**r,'_search_result_position':i} for i,r in enumerate(listings,1)}
    run_id='import:'+uuid.uuid4().hex
    now=clock()
    before=len(repo.snapshots())
    repo.start_run(run_id,now,'offline_import')
    for record in records:
        record['decision']='historical_discovery_only'
        record['evidence_file']=str((base/'search-results.json').resolve())
        asin=record['asin']
        if record['relevance']['status']!='unrelated' and isinstance(asin,str) and len(asin)==10:
            repo.discover(asin,profile_key(conditions,asin),{'title':record['title'],'relevance':record['relevance']},now)
    # Stable search evidence key makes repeated imports idempotent too.
    repo.save_discoveries('historical:search:2026-10-07',records)
    imported=[]
    for product in products:
        asin=product['asin']
        host=requests.get('/dp/'+asin,{}).get('final_host')
        page_url='https://'+host+'/dp/'+asin if host else None
        html_path=source/(asin+'.html')
        row=normalize(product,by_asin.get(asin,{}),html_path.read_text(encoding='utf-8'),None,page_url,
                      conditions_config=conditions,keyword='custom pet portrait shirt')
        row['fetched_at']=original[asin]['fetched_at']
        if not row['collection_conditions']['region_verified']:
            raise ValueError('saved_region_evidence_unverified')
        row['commerce']=enrich_commerce(row['commerce'])
        row['evidence']['import_source']=str(html_path.resolve())
        repo.save_snapshot(row,profile_key(conditions,asin),'historical:'+asin+':'+row['fetched_at'],run_id,now)
        imported.append(asin)
    summary={'run_id':run_id,'mode':'offline_import','requests':0,'imported_asins':imported,
             'snapshots_added':len(repo.snapshots())-before,'search_records':len(records),
             'note':'Original observed timestamps retained; no request or budget consumption.'}
    repo.finish_run(run_id,now,summary)
    return summary
