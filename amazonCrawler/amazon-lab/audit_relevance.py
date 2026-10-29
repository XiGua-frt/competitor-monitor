"""Relevance evaluation on historical search and detail evidence; no requests."""
import importlib.util
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('amazon_probe', ROOT/'probe.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def audit():
    base = ROOT.parent/'test/amazon/amzpy'
    source = base/'us-preferences-fixed'
    output = base/'step2-relevance'
    search_path = base/'search-results.json'
    listings = json.loads(search_path.read_text(encoding='utf-8'))
    discoveries, selected = probe.prepare_candidates(listings, probe.QUERY, limit=3)
    original = {r['external_id']:r for r in json.loads((source/'posts.json').read_text(encoding='utf-8'))}
    products = json.loads((source/'upstream-details.json').read_text(encoding='utf-8'))
    by_asin = {r['asin']:{**r,'_search_result_position':i} for i,r in enumerate(listings,1)}
    old_report = json.loads((source/'report.json').read_text(encoding='utf-8'))
    requests = {r['path']:r for r in old_report['requests']}
    rows = []
    for product in products:
        asin = product['asin']
        request = requests.get('/dp/'+asin, {})
        host = request.get('final_host')
        url = 'https://'+host+'/dp/'+asin if host else None
        html_path = source/(asin+'.html')
        row = probe.normalize(product, by_asin.get(asin, {}), html_path.read_text(encoding='utf-8'), 0, url)
        row['fetched_at'] = original[asin]['fetched_at']
        row['relevance']['evidence_files'] = [str(html_path), str(source/'upstream-details.json')]
        row['relevance']['search_stage'] = probe.classify_product(by_asin.get(asin, {}).get('title'))
        rows.append(row)
    report = {'mode':'offline_historical_evidence', 'network_requests':0,
              'evaluated_at':datetime.now(timezone.utc).isoformat(),
              'keyword':probe.QUERY, 'rule_version':probe.classify_product()['rule_version'],
              'search_evidence_file':str(search_path), 'search_rows':len(discoveries),
              'search_counts':dict(Counter(r['relevance']['status'] for r in discoveries)),
              'detail_counts':dict(Counter(r['relevance']['status'] for r in rows)),
              'planned_detail_asins':[r['asin'] for r in selected],
              'note':'Search decisions are a plan, not new executed requests; historical search prices are not current US quotes.'}
    output.mkdir(parents=True, exist_ok=True)
    for name, data in [('discoveries.json',discoveries), ('posts.json',rows), ('report.json',report)]:
        (output/name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    labels = {'related':'相关', 'unrelated':'不相关', 'needs_review':'待确认'}
    lines = ['# 第二步：搜索与商品相关性筛选', '',
             '本次只分析已保存样本，网络请求数 0。规则为文本初筛，未通过图片或定制下单验证。', '',
             '## 搜索初筛', '',
             f'历史搜索返回 {len(discoveries)} 条；不是已验证页面排名，也不是当前搜索结果。', '',
             '；'.join(labels[s]+' '+str(report['search_counts'].get(s,0))+' 条' for s in labels)+'。', '',
             '## 已保存详情复核', '',
             '| ASIN | 标题初筛 | 详情复核 | 商品范围 |', '|---|---|---|---|']
    for row in rows:
        relevance = row['relevance']
        scope = '宠物肖像定制服装' if relevance['product_scope']=='pet_specific' else '通用照片定制服装，详情明确支持宠物肖像' if relevance['product_scope'] else '未确认'
        lines.append('| '+' | '.join([row['external_id'], labels[relevance['search_stage']['status']], labels[relevance['status']],scope])+' |')
    lines.extend(['', '## 全部搜索判断', '',
                  '明显不相关商品不安排详情；待确认商品保留。选择仍受三件详情预算限制，未执行新请求。', '',
                  '| 返回顺序 | ASIN | 标题 | 判断 | 计划动作 |', '|---|---|---|---|---|'])
    decisions = {'skip_unrelated':'跳过详情', 'skip_duplicate':'本轮重复', 'skip_invalid_asin':'ASIN 无效',
                 'defer_budget':'预算外保留', 'fetch_detail':'可安排详情（未执行）'}
    for row in discoveries:
        title = (row['title'] or '').replace('|','\\|').replace('\n',' ')
        lines.append('| '+' | '.join([str(row['returned_position']), str(row['asin']), title,
                                      labels[row['relevance']['status']],decisions[row['decision']]])+' |')
    lines.extend(['', '每条判断的命中原文、理由与规则版本见 discoveries.json；详情证据见 posts.json。',
                  '本阶段没有实现跨次缓存、数据库、持久限流或定时调度；在线入口已接入，尚未发起真实请求复测。', ''])
    (output/'RESULT.md').write_text('\n'.join(lines), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    audit()
