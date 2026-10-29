import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from amzpy import AmazonScraper

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
from collector.conditions import load_conditions
from collector.repository import Repository
from collector.import_saved import import_saved


class ImportTests(unittest.TestCase):
    def test_import_is_idempotent_and_preserves_source_and_time(self):
        base=ROOT.parent/'test/amazon/amzpy'
        paths=list((base/'us-preferences-fixed').glob('*.json'))+list((base/'us-preferences-fixed').glob('*.html'))+[base/'search-results.json']
        hashes={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        with tempfile.TemporaryDirectory() as directory:
            repo=Repository(Path(directory)/'state.sqlite3')
            try:
                first=import_saved(repo,load_conditions(),base)
                second=import_saved(repo,load_conditions(),base)
                self.assertEqual(first['snapshots_added'],3)
                self.assertEqual(second['snapshots_added'],0)
                self.assertEqual(repo.request_counts(1791500000)['day'],0)
                self.assertTrue(all(r['observed_at'].startswith('2026-10-08') for r in repo.snapshots()))
            finally:repo.close()
        self.assertEqual(hashes,{p:hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})

if __name__=='__main__':unittest.main()
