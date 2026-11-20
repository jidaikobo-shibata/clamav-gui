"""Regression checks for bounded work, asynchronous reads and indexed cleanup."""
from pathlib import Path
import tempfile
import threading
import time
import tracemalloc
import unittest
from unittest.mock import patch, Mock
from clam_gui.async_reader import LatestReader
from clam_gui.core import Store, Plan
from clam_gui.retention import DEFAULTS, prune


class PerformanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)

    def test_history_headers_do_not_retain_bodies_and_details_are_scoped(self):
        plan = Plan('test', [self.temp.name], [])
        key = self.store.save_profile(plan)
        with self.store.connect() as db:
            db.executemany('INSERT INTO scans(started,target,status,output,profile_id,completed_at,detected) VALUES (?,?,?,?,?,?,?)',
                           [('2026-01-01', plan.describe(), '検査完了', 'x'*262144, key, '2026-01-01', 0)]*100)
        tracemalloc.start()
        try:
            entries = self.store.history_headers(key, plan)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(len(entries), 100)
        self.assertTrue(all(not entry.output and not entry.target for entry in entries))
        self.assertLess(peak, 1024*1024)
        self.assertEqual(len(self.store.history_entry(entries[0].id,key,plan).output),262144)
        self.assertIsNone(self.store.history_entry(entries[0].id,'other',plan))
        with self.store.connect() as db:
            db.execute('DELETE FROM scans WHERE rowid=?',(entries[0].id,))
        self.assertIsNone(self.store.history_entry(entries[0].id,key,plan))

    def test_legacy_headers_preserve_detection_and_completion(self):
        plan = Plan('test', [self.temp.name], [])
        with self.store.connect() as db:
            db.execute('INSERT INTO scans VALUES (?,?,?,?,?,?,?,?,?)',
                       ('2026-01-01',plan.describe(),'検出あり','End Date: 2026:11:18 12:34:56\n',None,None,None,None,None))
        entry = self.store.history_headers('p',plan)[0]
        self.assertTrue(entry.is_detected())
        self.assertIn('12:34:56',entry.date_label())
        self.assertFalse(entry.output)

    def test_reader_discards_stale_and_bounds_pending_requests(self):
        deliveries = []
        reader = LatestReader(deliveries.append)
        entered, release = threading.Event(), threading.Event()
        results = []
        def slow():
            entered.set()
            release.wait(2)
            return 'old'
        reader.submit(slow, lambda value,error: results.append(value))
        self.assertTrue(entered.wait(2))
        for index in range(100):
            reader.submit(lambda value=index: value, lambda value,error: results.append(value))
        release.set()
        deadline = time.monotonic()+3
        while reader.active and time.monotonic()<deadline:
            time.sleep(.001)
        self.assertFalse(reader.active)
        self.assertEqual(len(deliveries),1)
        deliveries.pop()()
        self.assertEqual(results,[99])
        reader.submit(lambda:'closed',lambda *args:results.append('bad'))
        reader.close()
        deadline = time.monotonic()+3
        while reader.active and time.monotonic()<deadline:
            time.sleep(.001)
        for delivery in deliveries: delivery()
        self.assertEqual(results,[99])

    def test_retention_reclassifies_only_changed_rows_and_tracks_deletion(self):
        policy = dict(DEFAULTS,retention_enabled=True)
        for _ in range(3): self.store.save('2026-01-01','test','検査完了','body','p')
        self.store.set_automation_settings(policy)
        with self.store.connect() as db, patch('clam_gui.retention.assess', wraps=__import__('clam_gui.retention',fromlist=['assess']).assess) as assess:
            self.assertEqual(prune(db,policy),0)
            assess.assert_not_called()
            db.execute("UPDATE scans SET completed_at='2000-01-01', output='changed' WHERE rowid=1")
            self.assertEqual(prune(db,policy),1)
            self.assertEqual(assess.call_count,1)
            db.execute('DELETE FROM scans WHERE rowid=2')
            count, size = db.execute('SELECT records,bytes FROM retention_totals').fetchone()
            actual = db.execute('SELECT count(*),sum(size) FROM retention_info').fetchone()
            self.assertEqual((count,size),actual)
            self.assertEqual(count,1)

    def test_legacy_retention_backfill_is_batched_and_old_writer_remains_supported(self):
        policy = dict(DEFAULTS,retention_enabled=True,max_records=1000)
        with self.store.connect() as db:
            db.executemany('INSERT INTO scans(started,target,status,output) VALUES (?,?,?,?)',
                           [('invalid date','test','検査完了','log')]*600)
        self.assertEqual(self.store.set_automation_settings(policy),0)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM retention_pending').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT records FROM retention_totals').fetchone()[0],600)
            db.execute('INSERT INTO scans(started,target,status,output) VALUES (?,?,?,?)',
                       ('2000-01-01','test','検出あり','old writer'))
            self.assertEqual(prune(db,policy),1)
            db.execute("UPDATE scans SET target='different',output='longer body' WHERE rowid=1")
            self.assertEqual(prune(db,policy),0)
            self.assertEqual(db.execute('SELECT size FROM retention_info WHERE id=1').fetchone()[0],
                             len(('different'+'longer body'+'検査完了').encode())+128)
        # Reopening the database must not seed the queue again or lose totals.
        reopened=Store(self.temp.name)
        with reopened.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM retention_pending').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT records FROM retention_totals').fetchone()[0],600)

    def test_discovery_yields_and_closes_iterator_on_reconfigure(self):
        from clam_gui.gio_watch import Observers
        folder = Path(self.temp.name)/'watch';folder.mkdir()
        for index in range(300): (folder/str(index)).touch()
        class Volumes:
            def get_mounts(self): return []
            def connect(self,*args): return 1
            def disconnect(self,*args): pass
        engine = Mock();engine.errors={}
        observer = Observers(engine,Volumes())
        self.addCleanup(observer.close)
        seen=[];observer.inventory=lambda key,path:seen.append(path)
        with patch('clam_gui.gio_watch.GLib.idle_add',return_value=1), patch('clam_gui.gio_watch.GLib.source_remove'):
            observer.configure([('p',Plan('watch',[str(folder)],[],kind='watch'))])
            self.assertTrue(observer.drain())
            self.assertLessEqual(len(seen),128)
            self.assertTrue(observer.discovering)
            while observer.drain(): pass
            self.assertEqual(len(seen),300)
            self.assertFalse(observer.discovering)
            observer.enqueue('p',folder,False);observer.drain()
            self.assertIsNotNone(observer.current)
            observer.configure([])
            self.assertIsNone(observer.current)
            self.assertFalse(observer.discovering)
            observer.idle=None

    def test_idle_monitor_heartbeat_is_throttled_and_commands_are_polled(self):
        from clam_gui.monitor import Monitor
        from clam_gui.background import put_preference, preference
        class Source:
            def __init__(self,engine): self.tasks=[];self.monitors={};self.plans={};self.count=0
            def configure(self,plans): self.plans=dict(plans)
            def scan_connected(self): self.count+=1;return 2
            def close(self): pass
        monitor=Monitor(self.store,observers=Source,notifier=lambda *args:None)
        self.addCleanup(monitor.lease.close);self.addCleanup(monitor.outbox.close)
        put_preference(self.store,'background-enabled',True)
        with patch('clam_gui.monitor.time.monotonic',return_value=100) as clock, patch.object(self.store.preferences,'set',wraps=self.store.preferences.set) as put:
            monitor.tick()
            for _ in range(4): monitor.tick()
            self.assertEqual(put.call_count,1)
            clock.return_value=103
            monitor.tick()
            self.assertEqual(put.call_count,2)
            put_preference(self.store,'background-command',dict(token='t',action='media',created=time.time()))
            clock.return_value=104
            monitor.tick()
            self.assertEqual(monitor.observers.count,1)
            self.assertEqual(preference(self.store,'background-command-result')['token'],'t')
            put_preference(self.store,'background-enabled',False)
            clock.return_value=105
            self.assertFalse(monitor.tick())
