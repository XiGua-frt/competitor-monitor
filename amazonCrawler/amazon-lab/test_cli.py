import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parent


class CliTests(unittest.TestCase):
    def call(self,*args):
        return subprocess.run([sys.executable,'-I',str(ROOT/'collect.py'),*args],capture_output=True,text=True,encoding='utf-8')

    def test_status_and_dry_run_do_not_create_database(self):
        with tempfile.TemporaryDirectory() as folder:
            db=Path(folder)/'absent.sqlite3'
            for command in ('status','dry-run'):
                result=self.call(command,'--db',str(db))
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertFalse(db.exists())
                self.assertFalse(json.loads(result.stdout)['initialized'])

    def test_pause_resume_retains_persistent_state(self):
        with tempfile.TemporaryDirectory() as folder:
            db=Path(folder)/'state.sqlite3'
            self.assertEqual(self.call('pause','--db',str(db)).returncode,0)
            self.assertTrue(json.loads(self.call('status','--db',str(db)).stdout)['control']['paused'])
            self.assertEqual(self.call('resume','--db',str(db)).returncode,0)
            self.assertFalse(json.loads(self.call('status','--db',str(db)).stdout)['control']['paused'])


if __name__=='__main__':unittest.main()
