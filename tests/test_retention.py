import datetime
from pathlib import Path
import tempfile
import unittest
from clam_gui.core import Store
from clam_gui.retention import DEFAULTS, prune


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(self.temp.name)
        self.now=datetime.datetime.now().astimezone()

    def add(self,status,age=0,output='log'):
        stamp=(self.now-datetime.timedelta(days=age)).isoformat()
        self.store.save(stamp,'test',status,output,'plan')
        with self.store.connect() as db:
            ident=db.execute('SELECT max(rowid) FROM scans').fetchone()[0]
            db.execute('UPDATE scans SET completed_at=? WHERE rowid=?',(stamp,ident))
        return ident

    def test_disabled_retention_does_not_delete_existing_history(self):
        self.add('検査完了',1000)
        self.add('検査完了')
        self.assertEqual(len(self.store.recent()),2)
        self.assertFalse(self.store.automation_settings()['retention_enabled'])

    def test_age_limits_preserve_alerts_longer(self):
        self.add('検査完了',100)
        self.add('検出あり',200)
        self.add('検査不完了',200)
        self.add('検出あり',400)
        policy=dict(DEFAULTS,retention_enabled=True)
        self.assertEqual(self.store.set_automation_settings(policy),2)
        with self.store.connect() as db:
            rows=db.execute('SELECT status FROM scans').fetchall()
        self.assertEqual({row[0] for row in rows},{'検出あり','検査不完了'})

    def test_count_limit_removes_normal_results_before_older_alerts(self):
        important=self.add('検出あり')
        for i in range(101):self.add('検査完了 '+str(i))
        self.store.set_automation_settings(dict(DEFAULTS,retention_enabled=True,max_records=100))
        with self.store.connect() as db:
            ids=[row[0] for row in db.execute('SELECT rowid FROM scans')]
        self.assertEqual(len(ids),100)
        self.assertIn(important,ids)

    def test_byte_limit_is_global_and_includes_output(self):
        important=self.add('検出あり',output='x'*1024*1024)
        for _ in range(17):self.add('検査完了',output='x'*1024*1024)
        self.store.set_automation_settings(dict(DEFAULTS,retention_enabled=True,max_mib=16))
        with self.store.connect() as db:
            self.assertLessEqual(db.execute('SELECT sum(log_bytes) FROM scans').fetchone()[0],16*1024*1024)
            self.assertIsNotNone(db.execute('SELECT rowid FROM scans WHERE rowid=?',(important,)).fetchone())

    def test_invalid_policy_does_not_change_saved_settings(self):
        with self.assertRaises(ValueError):self.store.set_automation_settings(dict(DEFAULTS,max_records=1))
        self.assertEqual(self.store.automation_settings(),DEFAULTS)
