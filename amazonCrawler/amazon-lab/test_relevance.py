import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from collector.relevance import classify_product, prepare_candidates


class RelevanceTests(unittest.TestCase):
    def test_explicit_custom_pet_photo_apparel(self):
        result = classify_product('Custom Dog Portrait Shirt from Photo for Men')
        self.assertEqual(result['status'], 'related')
        self.assertEqual(result['product_scope'], 'pet_specific')
        self.assertTrue(result['evidence'])

    def test_pet_worn_clothing_is_excluded(self):
        for title in ('Custom T Shirts for Dogs and Cats - Adorable Pet Clothes',
                      'Custom Dog Shirt with Photo for Small Medium Large Pet Dogs Puppy Cats',
                      'Custom Dog Hoodie, Personalized Dogs Clothes'):
            self.assertEqual(classify_product(title)['status'], 'unrelated')

    def test_non_apparel_and_patch_not_confused_with_shirts(self):
        for title in ('Custom Pet Portrait Canvas from Photo, Wall Art',
                      'Nurse Patch Iron-On for Uniforms Jackets Bags Hats',
                      'Custom Pet Photo Mug'):
            self.assertEqual(classify_product(title)['status'], 'unrelated')

    def test_generic_photo_shirt_remains_pending_until_detail_evidence(self):
        title = 'Custom T Shirts Men Women Personalized Photo Text Logo'
        self.assertEqual(classify_product(title)['status'], 'needs_review')
        result = classify_product(title, 'Upload your own photo. Print a pet portrait for a partner.', stage='detail')
        self.assertEqual(result['status'], 'related')
        self.assertEqual(result['product_scope'], 'general_custom_photo_apparel')
        self.assertEqual(result['stage'], 'detail')

    def test_absence_of_customization_in_title_is_not_negative_evidence(self):
        self.assertEqual(classify_product('Dog Person T-Shirt')['status'], 'needs_review')

    def test_human_shirt_with_keychain_not_excluded(self):
        self.assertEqual(classify_product('Custom Pet Photo T Shirt with Matching Keychain')['status'], 'related')

    def test_no_keyword_or_recommended_products_used_as_evidence(self):
        self.assertEqual(classify_product(None, None)['status'], 'needs_review')
        self.assertEqual(classify_product('Custom Family Photo Shirt', 'A gift for dog lovers.')['status'], 'needs_review')

    def test_customization_explicitly_unavailable(self):
        result = classify_product('Pet Portrait T-Shirt', 'This design is not customizable.', stage='detail')
        self.assertEqual(result['status'], 'unrelated')

    def test_children_apparel_in_scope(self):
        self.assertEqual(classify_product('Custom Baby Bodysuit with Pet Portrait')['status'], 'related')

    def test_for_dog_lovers_is_human_recipient_not_pet_wear(self):
        for person in ('Dog Lovers', 'Dog Mom', 'Cat Owners', 'Dog Parents'):
            self.assertEqual(classify_product('Custom Pet Portrait Shirt for '+person)['status'], 'related')

    def test_selection_preserves_original_position_and_rejected_records(self):
        results = [{'asin':'B000000001','title':'Custom Pet Photo Mug'},
                   {'asin':'B000000002','title':'Custom Pet Photo Shirt'},
                   {'asin':'B000000002','title':'Custom Pet Photo Shirt'},
                   {'asin':'bad','title':'Custom Pet Photo Shirt'},
                   {'asin':'B000000003','title':'Custom Photo Shirt'}]
        records, selected = prepare_candidates(results, 'custom pet portrait shirt', limit=2)
        self.assertEqual(len(records), 5)
        self.assertEqual([p['asin'] for p in selected], ['B000000002','B000000003'])
        self.assertEqual([p['_search_result_position'] for p in selected], [2,5])
        self.assertEqual(records[0]['decision'], 'skip_unrelated')
        self.assertEqual(records[2]['decision'], 'skip_duplicate')
        self.assertEqual(records[3]['decision'], 'skip_invalid_asin')


if __name__ == '__main__':
    unittest.main()
