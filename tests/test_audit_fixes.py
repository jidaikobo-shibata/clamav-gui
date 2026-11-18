"""Regression coverage for findings from the Windows handoff audit."""
import datetime
import json
import os
import sys
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from clam_gui.core import Store, Plan
from clam_gui.automation import Automation, WatchQueue, signature
from clam_gui.notification_outbox import NotificationOutbox


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(self.temp.name)
        self.clock=[0]
        self.delivered=[]
        self.outbox=NotificationOutbox(self.store,lambda *args:self.delivered.append(args),clock=lambda:self.clock[0])
        self.addCleanup(self.outbox.close)

    def wait(self,predicate):
        deadline=time.monotonic()+3
        while not predicate() and time.monotonic()<deadline:time.sleep(.005)
        self.assertTrue(predicate())

    def test_slow_delivery_retains_all_five_critical_notices(self):
        release=threading.Event();entered=threading.Event();self.addCleanup(release.set)
        def slow(*args):
            entered.set();release.wait(2);self.delivered.append(args)
        self.outbox.deliver=slow
        for i in range(5):self.outbox.enqueue('detected',str(i),'critical')
        self.outbox.pump();self.assertTrue(entered.wait(1))
        self.assertEqual(self.outbox.state()['pending'],5)
        release.set();self.wait(lambda:self.outbox.state()['pending']==0)
        self.assertEqual([notice[1] for notice in self.delivered],['0','1','2','3','4'])

    def test_failed_delivery_survives_restart_and_retries(self):
        self.outbox.deliver=Mock(side_effect=OSError('notification unavailable'))
        self.outbox.enqueue('detected','details','critical')
        self.assertTrue(self.outbox.deliver_one())
        self.assertEqual(self.outbox.state()['pending'],1)
        self.assertIn('unavailable',self.outbox.state()['error'])
        self.assertFalse(self.outbox.deliver_one())
        self.outbox.close();self.clock[0]=6
        resumed=NotificationOutbox(self.store,lambda *args:self.delivered.append(args),clock=lambda:self.clock[0])
        self.addCleanup(resumed.close)
        self.assertTrue(resumed.deliver_one())
        self.assertEqual(resumed.state(),dict(pending=0,error=''))
        self.assertEqual(len(self.delivered),1)

    def test_storage_is_bounded_and_overflow_keeps_critical_count(self):
        self.outbox.LIMIT=2
        for i in range(7):self.outbox.enqueue('detected',str(i),'critical')
        for i in range(7):self.outbox.enqueue('normal',str(i))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM notification_outbox').fetchone()[0],4)
        self.assertEqual(self.outbox.state()['pending'],14)
        while self.outbox.deliver_one():pass
        self.assertEqual(self.outbox.state()['pending'],0)
        self.assertIn('5件',self.delivered[2][1])
        self.assertEqual(self.delivered[2][2],'critical')
        self.assertIn('7件',self.delivered[3][1])

    def test_new_arrival_during_summary_delivery_is_not_acknowledged_early(self):
        self.outbox.LIMIT=0
        for i in range(5):self.outbox.enqueue('detected',str(i),'critical')
        def deliver(*args):
            self.delivered.append(args)
            if len(self.delivered)==1:self.outbox.enqueue('new','new','critical')
        self.outbox.deliver=deliver
        self.outbox.deliver_one()
        self.assertEqual(self.outbox.state()['pending'],1)
        self.outbox.deliver_one()
        self.assertEqual(self.outbox.state()['pending'],0)
        self.assertIn('5件',self.delivered[0][1]);self.assertIn('1件',self.delivered[1][1])

    def test_two_dispatchers_do_not_deliver_or_acknowledge_the_same_claim(self):
        self.outbox.LIMIT=0
        self.outbox.enqueue('detected','one','critical')
        other=NotificationOutbox(self.store,lambda *args:self.delivered.append(args),clock=lambda:self.clock[0])
        self.addCleanup(other.close)
        def deliver(*args):
            self.delivered.append(args)
            self.assertFalse(other.deliver_one())
            self.outbox.enqueue('new','two','critical')
        self.outbox.deliver=deliver
        self.outbox.deliver_one()
        self.assertEqual(self.outbox.state()['pending'],1)
        self.assertTrue(other.deliver_one())
        self.assertEqual(self.outbox.state()['pending'],0)

    def test_expired_delivery_claim_is_recovered_after_a_crash(self):
        self.outbox.enqueue('detected','one','critical')
        with self.store.connect() as db:
            db.execute('UPDATE notification_outbox SET claim=?,lease_until=?',('stopped-worker',60))
        self.assertFalse(self.outbox.deliver_one())
        self.clock[0]=61
        self.assertTrue(self.outbox.deliver_one())
        self.assertEqual(self.outbox.state()['pending'],0)

    def test_critical_notices_precede_normal_backlog(self):
        self.outbox.enqueue('normal','normal')
        self.outbox.enqueue('detected','detected','critical')
        self.outbox.deliver_one()
        self.assertEqual(self.delivered[0][0],'detected')

    def test_thread_creation_failure_retains_pending_notice(self):
        self.outbox.enqueue('detected','detail','critical')
        with patch('clam_gui.notification_outbox.threading.Thread') as thread:
            thread.return_value.start.side_effect=RuntimeError('cannot start')
            with self.assertRaises(RuntimeError):self.outbox.pump()
        self.assertEqual(self.outbox.state()['pending'],1)


class WatchChangeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.folder=self.root/'watch';self.folder.mkdir()
        self.file=self.folder/'file';self.file.write_bytes(b'AAAA')
        self.plan=Plan('watch',[str(self.folder)],[],kind='watch')

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux inode change timestamp')
    def test_same_size_rewrite_with_restored_mtime_invalidates_snapshot(self):
        old=self.file.stat();before=signature(self.file)
        queue=WatchQueue(quiet=0);queue.completed['p',str(self.file)]=before
        self.file.write_bytes(b'BBBB');os.utime(self.file,ns=(old.st_atime_ns,old.st_mtime_ns))
        queue.put('p',self.plan,self.file)
        self.assertNotEqual(before,signature(self.file))
        self.assertEqual(len(queue.take({'p':self.plan})[1]),1)

    def test_event_invalidates_cache_even_when_metadata_does_not_change(self):
        stamp=signature(self.file)
        queue=WatchQueue(quiet=0);queue.completed['p',str(self.file)]=stamp
        with patch('clam_gui.automation.signature',return_value=stamp):
            queue.put('p',self.plan,self.file,changed=True)
            self.assertEqual(len(queue.take({'p':self.plan})[1]),1)

    def test_change_during_scan_remains_queued_even_with_identical_metadata(self):
        store=Store(self.root/'data');release=threading.Event();entered=threading.Event()
        self.addCleanup(release.set)
        def scanner(*args,**kwargs):
            entered.set();release.wait(2);return '検査完了',''
        engine=Automation(store,lambda cb:cb(),lambda *args:None,scanner=scanner)
        self.addCleanup(engine.close);engine.configure([('p',self.plan)],False);engine.queue.quiet=0
        stamp=signature(self.file)
        with patch('clam_gui.automation.signature',return_value=stamp),patch('shutil.which',return_value='/mock/clamscan'):
            engine.file_event('p',self.file);engine.tick();self.assertTrue(entered.wait(1))
            engine.file_event('p',self.file);release.set()
            deadline=time.monotonic()+2
            while engine.active and time.monotonic()<deadline:time.sleep(.005)
            self.assertFalse(engine.active)
            self.assertEqual(len(engine.queue.take({'p':self.plan})[1]),1)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux inode change timestamp')
    def test_restart_detects_mtime_preserving_rewrite(self):
        try:
            from clam_gui.monitor import Checkpoints
        except ImportError:self.skipTest('GIO is not installed')
        store=Store(self.root/'data')
        engine=Automation(store,lambda cb:cb(),lambda *args:None);engine.configure([('p',self.plan)],False)
        checkpoint=Checkpoints(store,engine);checkpoint.configure([('p',self.plan)])
        checkpoint.inventory('p',str(self.file));checkpoint.complete_baseline({('p',str(self.folder)):None})
        original=self.file.stat();self.file.write_bytes(b'BBBB')
        os.utime(self.file,ns=(original.st_atime_ns,original.st_mtime_ns))
        resumed=Checkpoints(store,engine);resumed.configure([('p',self.plan)]);resumed.inventory('p',str(self.file))
        self.assertIn(('p',str(self.file)),engine.queue.pending)

    def test_old_snapshot_version_builds_new_baseline_without_touching_history(self):
        try:
            from clam_gui.monitor import Checkpoints
        except ImportError:self.skipTest('GIO is not installed')
        store=Store(self.root/'data');store.save('date','target','検査完了','keep')
        engine=Automation(store,lambda cb:cb(),lambda *args:None);engine.configure([('p',self.plan)],False)
        checkpoint=Checkpoints(store,engine)
        old_config=json.dumps(dict(targets=self.plan.targets,excludes=[],excluded_dirs=[],recursive=True),sort_keys=True)
        with store.connect() as db:
            db.execute('INSERT INTO monitor_roots VALUES (?,?)',('p',old_config))
            db.execute('INSERT INTO monitor_files VALUES (?,?,?)',('p',str(self.file),json.dumps(signature(self.file)[:4])))
        checkpoint.configure([('p',self.plan)])
        self.assertNotIn('p',checkpoint.ready)
        checkpoint.inventory('p',str(self.file));checkpoint.complete_baseline({('p',str(self.folder)):None})
        self.assertIn('p',checkpoint.ready);self.assertEqual(len(store.recent()),1)


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux process ownership')
class GuardTests(unittest.TestCase):
    def test_failure_to_arm_parent_death_protection_never_executes_scanner(self):
        from clam_gui.platforms.scanner_guard import main
        with patch('sys.argv',['guard','--parent',str(os.getpid()),'--lock-fd','33','--','/never-execute']), \
             patch('clam_gui.platforms.scanner_guard.os.fstat'), \
             patch('clam_gui.platforms.scanner_guard.ctypes.CDLL') as library, \
             patch('clam_gui.platforms.scanner_guard.ctypes.get_errno',return_value=1), \
             patch('clam_gui.platforms.scanner_guard.os.execvpe') as execute:
            library.return_value.prctl.return_value=-1
            self.assertEqual(main(),125)
            execute.assert_not_called()
