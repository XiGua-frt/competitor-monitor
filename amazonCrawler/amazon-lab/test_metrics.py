import sys
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parent))
from collector.metrics import enrich_commerce, compare_snapshots


def row(price, count, seller='seller1', ready=True):
    return {'external_id':'B000000001','author_id':seller,'fetched_at':'2026-10-08T10:00:00+00:00',
            'commerce':{'price':price,'currency':'USD','rating_count':count,'seller_name':None,
                        'best_sellers_ranks':[{'category':'Clothing','rank':100}],
                        'recent_purchase_text':'100+ bought in past month'},
            'collection_conditions':{'region_verified':True,'comparison_ready':ready,
                'observed':{'asin':'B000000001','size':'Small','color':'Black','fit_type':'Men'}}}


class MetricTests(unittest.TestCase):
    def test_unfixed_or_changed_variant_blocks_all_trends(self):
        for after in (row(15,8,ready=False),row(15,8)):
            if after['collection_conditions']['comparison_ready']:
                after['collection_conditions']['observed']['size']='Large'
            result=compare_snapshots(row(12,5),after)
            self.assertIsNone(result['rating_count_change'])
            self.assertEqual(result['bsr_changes'],[])

    def test_raw_bsr_categories_and_purchase_lower_bound(self):
        value=enrich_commerce({'best_sellers_rank_raw':'#1,200 in Clothing (See Top 100 in Clothing) #12 in Mens Novelty T-Shirts',
                               'recent_purchase_text':'1K+ bought in past month',
                               'date_first_available_raw':'October 8, 2026'})
        self.assertEqual(value['best_sellers_ranks'],[{'rank':1200,'category':'Clothing'},{'rank':12,'category':'Mens Novelty T-Shirts'}])
        self.assertEqual(value['recent_purchase_display']['lower_bound'],1000)
        self.assertFalse(value['recent_purchase_display']['exact_sales'])
        self.assertEqual(value['date_first_available'],'2026-10-08')

    def test_missing_data_is_not_zero(self):
        value=enrich_commerce({})
        self.assertIsNone(value['recent_purchase_display'])
        self.assertIsNone(value['best_sellers_ranks'])
        self.assertIsNone(value['date_first_available'])

    def test_comparable_price_and_displayed_rating_count(self):
        before,after=row(12.95,5),row(14.99,8)
        result=compare_snapshots(before,after)
        self.assertEqual(result['price_change'],2.04)
        self.assertEqual(result['rating_count_change'],3)
        self.assertIsNone(result['sales_change'])

    def test_unknown_variant_or_seller_change_blocks_price(self):
        for after in (row(14.99,8,ready=False),row(14.99,8,seller='seller2'),row(14.99,8,seller=None)):
            self.assertIsNone(compare_snapshots(row(12.95,5),after)['price_change'])

    def test_missing_price_or_currency_blocks_price(self):
        after=row(None,8)
        self.assertIsNone(compare_snapshots(row(12.95,5),after)['price_change'])
        after=row(14.99,8);after['commerce']['currency']='JPY'
        self.assertIsNone(compare_snapshots(row(12.95,5),after)['price_change'])

    def test_different_bsr_category_not_compared(self):
        before,after=row(12.95,5),row(14.99,8)
        after['commerce']['best_sellers_ranks']=[{'category':'Pet Supplies','rank':90}]
        self.assertEqual(compare_snapshots(before,after)['bsr_changes'],[])

    def test_rating_count_decrease_not_interpreted_as_negative_sales(self):
        result=compare_snapshots(row(12.95,5),row(12.95,2))
        self.assertEqual(result['rating_count_change'],-3)
        self.assertIsNone(result['sales_change'])


if __name__=='__main__':unittest.main()
