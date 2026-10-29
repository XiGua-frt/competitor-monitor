"""Read saved evidence only; write a separate report without changing old results."""
import json
import importlib.util
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('amazon_probe', ROOT/'probe.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
normalize = probe.normalize


def audit():
    source = ROOT.parent / 'test/amazon/amzpy/us-preferences-fixed'
    output = source.parent / 'step1-conditions'
    products = json.loads((source/'upstream-details.json').read_text(encoding='utf-8'))
    old_report = json.loads((source/'report.json').read_text(encoding='utf-8'))
    original_rows = {r['external_id']:r for r in json.loads((source/'posts.json').read_text(encoding='utf-8'))}
    requests = {r['path']:r for r in old_report['requests']}
    rows = []
    for i, product in enumerate(products, 1):
        asin = product['asin']
        request = requests.get('/dp/' + asin, {})
        host = request.get('final_host')
        url = 'https://' + host + '/dp/' + asin if host else None
        row = normalize(product, {}, (source/(asin+'.html')).read_text(encoding='utf-8'), i, url)
        row['fetched_at'] = original_rows.get(asin, {}).get('fetched_at', old_report['fetched_at'])
        row['collection_conditions']['evidence_files'] = [str(source/(asin+'.html')), str(source/'report.json')]
        rows.append(row)
    report = {'mode':'offline_saved_evidence', 'network_requests':0,
              'audited_at':datetime.now(timezone.utc).isoformat(),
              'original_run_completed_at':old_report['fetched_at'],
              'products':[{'asin':r['external_id'], 'price':r['commerce']['price'],
                           'currency':r['commerce']['currency'], 'observed_at':r['fetched_at'],
                           **r['collection_conditions']} for r in rows]}
    output.mkdir(parents=True, exist_ok=True)
    for name, data in [('posts.json', rows), ('conditions-report.json', report)]:
        (output/name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    lines = ['# 第一步采集条件核对结果', '',
             '离线重读 2026-10-08 保存的页面；本次网络请求数：0。不是当前实时价格。', '',
             '预期条件：Amazon.com / 美国邮编 10001 / USD / 英文。', '',
             '| ASIN | 历史显示价 | 美国地区与币种 | 尺码 | 颜色 | 版型 | 可直接比较 | 说明 |',
             '|---|---|---|---|---|---|---|---|']
    for product in report['products']:
        observed = product['observed']
        if 'variant_binding_unverified' in product['reasons']:
            note = '已记录选中值；定制复合标识与当前报价的关联未确认'
        elif any(r in product['reasons'] for r in ('variant_evidence_conflict', 'variant_value_conflict')):
            note = '变体证据不一致，需核对'
        else:
            note = '尚未固定商品变体；缺失值表示未取得明确证据'
        lines.append('| ' + ' | '.join([product['asin'], str(product['price']) + ' ' + str(product['currency']),
                     '匹配' if product['region_verified'] else '未确认', observed['size'] or '空',
                     observed['color'] or '空', observed['fit_type'] or '空',
                     '是' if product['comparison_ready'] else '否', note]) + ' |')
    lines.extend(['', '详细页面观察值、原因与证据路径见同目录 conditions-report.json。',
                  '原始历史文件保持不变；新的在线核对行为只通过模拟测试，尚未进行真实请求复测。', ''])
    (output/'RESULT.md').write_text('\n'.join(lines), encoding='utf-8')
    print(json.dumps({'report':str(output/'conditions-report.json'), 'network_requests':0,
                      'products':[{'asin':p['asin'], 'region_verified':p['region_verified'],
                                   'comparison_ready':p['comparison_ready'], 'reasons':p['reasons']} for p in report['products']]}, indent=2))


if __name__ == '__main__':
    audit()
