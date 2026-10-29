"""Page-display indicators only. No conversion of ranks/counts into sales."""
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation


def enrich_commerce(commerce):
    value=dict(commerce)
    raw=value.get('best_sellers_rank_raw') or ''
    ranks=[]
    for match in re.finditer(r'#\s*([\d,]+)\s+in\s+([^#\n]+)',raw):
        category=re.split(r'\(\s*See Top',match[2],maxsplit=1,flags=re.I)[0].strip(' ;,')
        if category:
            ranks.append({'rank':int(match[1].replace(',','')),'category':category})
    value['best_sellers_ranks']=ranks or None
    date=None
    for fmt in ('%B %d, %Y','%b %d, %Y','%Y-%m-%d'):
        try:
            date=datetime.strptime((value.get('date_first_available_raw') or '').strip(),fmt).date().isoformat()
            break
        except ValueError:
            pass
    value['date_first_available']=date
    text=value.get('recent_purchase_text') or ''
    match=re.search(r'([\d,.]+)\s*([KM]?)\s*\+\s+bought\s+in\s+past\s+month',text,re.I)
    purchase=None
    if match:
        try:
            lower=Decimal(match[1].replace(',',''))*{'':1,'K':1000,'M':1000000}[match[2].upper()]
            if lower>=0 and lower==int(lower):
                purchase={'lower_bound':int(lower),'window':'page_displayed_past_month','exact_sales':False,'raw':text}
        except InvalidOperation:
            pass
    value['recent_purchase_display']=purchase
    return value


def compare_snapshots(before,after):
    result={'from':before.get('fetched_at'),'to':after.get('fetched_at'),
            'price_change':None,'rating_count_change':None,'bsr_changes':[],
            'sales_change':None,'reasons':[],
            'meaning':'rating count is page-displayed ratings, not written reviews or sales; BSR is a category rank'}
    b,a=before.get('commerce',{}),after.get('commerce',{})
    bc,ac=before.get('collection_conditions',{}),after.get('collection_conditions',{})
    if (before.get('external_id')!=after.get('external_id') or not bc.get('region_verified') or not ac.get('region_verified')
            or bc.get('expected',{})!=ac.get('expected',{})):
        result['reasons'].append('observation_conditions_not_comparable')
        return result
    bo,ao=bc.get('observed',{}),ac.get('observed',{})
    same_variant=all(bo.get(k)==ao.get(k) for k in ('asin','size','color','fit_type'))
    seller_before=before.get('author_id') or b.get('seller_name')
    seller_after=after.get('author_id') or a.get('seller_name')
    if not bc.get('comparison_ready') or not ac.get('comparison_ready') or not same_variant:
        result['reasons'].append('variant_unconfirmed_or_changed')
        return result
    elif not seller_before or not seller_after:
        result['reasons'].append('seller_unverified')
    elif seller_before!=seller_after:
        result['reasons'].append('seller_changed')
    elif b.get('price') is None or a.get('price') is None:
        result['reasons'].append('price_missing')
    elif not b.get('currency') or b.get('currency')!=a.get('currency'):
        result['reasons'].append('currency_missing_or_changed')
    else:
        result['price_change']=float(Decimal(str(a['price']))-Decimal(str(b['price'])))
    if b.get('rating_count') is not None and a.get('rating_count') is not None:
        result['rating_count_change']=a['rating_count']-b['rating_count']
    else:
        result['reasons'].append('rating_count_missing')
    previous={r['category']:r['rank'] for r in b.get('best_sellers_ranks') or []}
    for rank in a.get('best_sellers_ranks') or []:
        if rank['category'] in previous:
            result['bsr_changes'].append({'category':rank['category'], 'from':previous[rank['category']],
                                          'to':rank['rank'],'rank_change':rank['rank']-previous[rank['category']]})
    return result
