"""Local inspectable exports of observations, quality, changes and stop state."""
import json
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .metrics import compare_snapshots


def export_report(repo,directory,now):
    directory=Path(directory)
    directory.mkdir(parents=True,exist_ok=True)
    history=repo.snapshots()
    groups={}
    for record in history:
        groups.setdefault((record['asin'],record['profile']),[]).append(record)
    latest=[]
    changes=[]
    for (asin,profile),records in groups.items():
        data=dict(records[-1]['data'])
        data['storage']={'profile':profile,'snapshot_id':records[-1]['id'],
                         'last_valid_price':repo.last_valid_price(asin,profile)}
        latest.append(data)
        if len(records)>1:
            changes.append({'asin':asin,'profile':profile,**compare_snapshots(records[-2]['data'],records[-1]['data'])})
    fields=('price','currency','rating','rating_count','brand','seller_name',
            'date_first_available','best_sellers_ranks','recent_purchase_text')
    rates={field:(sum(r.get('commerce',{}).get(field) is None for r in latest)/len(latest) if latest else None) for field in fields}
    runs=[{**dict(r),'summary':json.loads(r['summary']) if r['summary'] else None} for r in repo.db.execute('SELECT * FROM runs ORDER BY started_at DESC LIMIT 20')]
    modes={r['mode'] for r in repo.db.execute('SELECT DISTINCT mode FROM runs')}
    report={'generated_at':datetime.fromtimestamp(now,timezone.utc).isoformat(),
            'product_count':len(repo.products()),'latest_observation_count':len(latest),'snapshot_count':len(history),
            'task_counts':dict(Counter(t['state'] for t in repo.tasks())),
            'usage':repo.request_counts(now),'control':repo.state('guard',{}),
            'missing_field_rates':rates,'changes':changes,'recent_runs':runs,
            'simulation_only':bool(modes and modes<= {'simulation','offline_import'}),
            'usage_window':'rolling_3600_and_86400_seconds_includes_reserved_failed_and_retried_requests',
            'data_semantics':'latest observation is not a current live quote; last valid price retains its original timestamp; ratings and BSR are not exact sales'}
    for name,value in [('latest.json',latest),('history.json',history),('tasks.json',repo.tasks()),('report.json',report)]:
        (directory/name).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    local=datetime.fromtimestamp(now,timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
    lines=['# Amazon 六步采集模块运行报告','',f'报告时间：{local}（北京时间）。','',
           ('数据来源：保存页面的模拟回放；观察时间和请求计数属于模拟，不是真实新采集。' if 'simulation' in modes else '数据来源：历史证据离线导入，保留原采集时间；没有发起新请求。') if report['simulation_only'] else '数据来源以每条记录的采集时间和最近运行模式为准；没有重新请求的报价仍是历史观察。','',
           f"商品档案 {report['product_count']}；历史快照 {len(history)}；有快照的商品/条件组合 {len(latest)}。",'',
           f"请求账本：过去一小时 {report['usage']['hour']}，过去 24 小时 {report['usage']['day']}（含失败、重试及中断前预留）。",'',
           '运行控制：'+json.dumps(report['control'],ensure_ascii=False),'',
           '任务状态：'+json.dumps(report['task_counts'],ensure_ascii=False),'',
           '## 最新观察','',
           '| ASIN | 观察时间（UTC） | 显示价 | 最近有效价格 | 尺码 | 颜色 | 版型 | 相关性 | 变体比较条件 |',
           '|---|---|---|---|---|---|---|---|---|']
    for row in latest:
        c=row.get('commerce',{});valid=row['storage']['last_valid_price']
        old=(str(valid['price'])+' '+str(valid['currency'])+' @ '+valid['observed_at']) if valid else '空'
        price='空' if c.get('price') is None else str(c['price'])+' '+str(c.get('currency'))
        variant=row.get('collection_conditions',{}).get('observed',{})
        lines.append('| '+' | '.join([row['external_id'],row['fetched_at'],price,old,
                     str(variant.get('size') or '空'),str(variant.get('color') or '空'),str(variant.get('fit_type') or '空'),
                     row.get('relevance',{}).get('status','未确认'),
                     '满足' if row.get('collection_conditions',{}).get('comparison_ready') else '未确认'])+' |')
    lines.extend(['','## 最近两次观察变化','',
                  '| ASIN | 价格差 | 页面评分数量差 | 同类目 BSR 变化条数 | 不能比较的原因 |',
                  '|---|---|---|---|---|'])
    for change in changes:
        lines.append('| '+' | '.join([change['asin'],str(change['price_change']) if change['price_change'] is not None else '不可计算',
                      str(change['rating_count_change']) if change['rating_count_change'] is not None else '不可计算',
                      str(len(change['bsr_changes'])),', '.join(change['reasons']) or '无'])+' |')
    if not changes:lines.append('当前没有同一商品/采集条件下的两次快照，变化不可计算。')
    lines.extend(['','## 最新观察字段空值率','',
                  '| 字段 | 空值率 |','|---|---|'])
    for field,rate in rates.items():lines.append('| '+field+' | '+(f'{rate:.0%}' if rate is not None else '无观察')+' |')
    lines.extend(['','报告包含全部最新观察，包括不相关/待确认商品，筛选请查看 relevance.status。',
                  '评分数量不等于文字评论数；评分数量差分不等于新增销量；BSR 只在相同类目比较。',
                  '近期购买量提示仅保留原文、下界和页面窗口，不相减为新增订单；页面价格不代表最终定制订单金额。',
                  '历史、任务、变化与每次运行详细信息见同目录 JSON 文件；持续真实运行稳定性尚未验证。',''])
    (directory/'REPORT.md').write_text('\n'.join(lines),encoding='utf-8')
    return report
