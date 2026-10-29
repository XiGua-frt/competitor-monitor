"""Formal collector entry. Default/status/dry-run never makes HTTP requests."""
import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

# Import the installed package before the checkout directory can shadow it.
from amzpy import AmazonScraper

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
from collector.conditions import load_conditions
from collector.runtime import load_runtime
from collector.repository import Repository
from collector.pipeline import Pipeline
from collector.scheduler import Scheduler
from collector.reporting import export_report


def read_status(path,now):
    path=Path(path)
    if not path.exists():
        return {'initialized':False,'database':str(path),'requests':0}
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
    try:
        counts={name:db.execute('SELECT COUNT(*) FROM '+name).fetchone()[0] for name in ('products','snapshots','tasks')}
        control=db.execute("SELECT value FROM state WHERE key='guard'").fetchone()
        usage={'hour':db.execute('SELECT COUNT(*) FROM requests WHERE reserved_at>?',(now-3600,)).fetchone()[0],
               'day':db.execute('SELECT COUNT(*) FROM requests WHERE reserved_at>?',(now-86400,)).fetchone()[0]}
        return {'initialized':True,'database':str(path),'counts':counts,'usage':usage,
                'control':json.loads(control[0]) if control else {}}
    finally:
        db.close()


def main(argv=None):
    parser=argparse.ArgumentParser(description='本地 Amazon 商品采集：默认只查看状态')
    parser.add_argument('command',nargs='?',default='status',choices=['status','dry-run','import-saved','once','schedule','pause','resume','report','demo'])
    parser.add_argument('--config',type=Path)
    parser.add_argument('--db',type=Path)
    parser.add_argument('--refresh-only',action='store_true')
    parser.add_argument('--once',action='store_true',help='调度只执行一次 tick')
    parser.add_argument('--reason',default='manual')
    args=parser.parse_args(argv)
    runtime=load_runtime(args.config)
    conditions=load_conditions()
    db_path=args.db or Path(runtime['database'])
    if args.command in ('status','dry-run'):
        status=read_status(db_path,time.time())
        if args.command=='dry-run':
            status.update(mode='dry_run',network_requests=0,writes=0,keyword=runtime['keyword'],
                          requested_conditions={k:conditions[k] for k in ('marketplace','delivery_postcode','currency','language')},
                          budgets={k:runtime[k] for k in ('max_requests_per_run','max_requests_per_hour','max_requests_per_day')})
        print(json.dumps(status,ensure_ascii=False,indent=2))
        return 0
    if args.command=='demo':
        from demo_collector import demo
        print(json.dumps(demo(),ensure_ascii=False,indent=2))
        return 0
    repo=Repository(db_path)
    try:
        if args.command=='pause':
            repo.pause(args.reason)
            result={'status':'paused','reason':args.reason}
        elif args.command=='resume':
            repo.resume()
            result={'status':'pause_cleared','control':repo.state('guard',{}),
                    'note':'Cooldown and request accounting remain in force.'}
        elif args.command=='import-saved':
            from collector.import_saved import import_saved
            result=import_saved(repo,conditions,ROOT.parent/'test/amazon/amzpy')
        elif args.command=='report':
            result=export_report(repo,runtime['report_dir'],time.time())
            result={'report':str(Path(runtime['report_dir'])/'REPORT.md'),
                    'snapshots':result['snapshot_count'],'tasks':result['task_counts']}
        else:
            from collector.amzpy_adapter import AmzPyAdapter
            pipeline=Pipeline(repo,conditions,runtime,runtime['run_dir'],runtime['evidence_dir'])
            factory=lambda:AmzPyAdapter(conditions,runtime)
            if args.command=='once':
                result=pipeline.once(factory,refresh_only=args.refresh_only)
            elif args.once:
                result=Scheduler(pipeline).tick(factory)
            else:
                scheduler=Scheduler(pipeline)
                previous=None
                try:
                    while True:
                        result=scheduler.tick(factory)
                        if result.get('requests',0) or result['status']!=previous:
                            print(json.dumps(result,ensure_ascii=False),flush=True)
                        previous=result['status']
                        if result['status']=='interrupted':break
                        time.sleep(runtime['poll_seconds'])
                except KeyboardInterrupt:
                    result={'status':'scheduler_stopped','note':'Persistent state retained.'}
            export_report(repo,runtime['report_dir'],time.time())
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 1 if result.get('errors') or (args.command in ('once','schedule') and result.get('status') in ('paused','cooldown','interrupted','worker_lease_lost')) else 0
    finally:
        repo.close()


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except (ValueError,OSError,sqlite3.Error) as error:
        print(json.dumps({'status':'configuration_or_dependency_error','type':type(error).__name__},ensure_ascii=False),file=sys.stderr)
        raise SystemExit(2)
