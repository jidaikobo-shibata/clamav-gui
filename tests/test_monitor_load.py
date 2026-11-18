"""Bounded monitor work, launch pacing, and child-only resource priorities."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from clam_gui.automation import Automation, WatchQueue
from clam_gui.core import Plan, Store, ScanBusyError


class MonitorLoadTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.folder=self.root/'watch';self.folder.mkdir()
        self.plan=Plan('watch',[str(self.folder)],[],kind='watch')
        self.now=0

    def file(self,name):
        p=self.folder/str(name);p.touch();return str(p)

    def test_quiet_queue_performs_no_filesystem_reads(self):
        queue=WatchQueue(lambda:self.now)
        for i in range(1000):queue.put('p',self.plan,self.file(i))
        with patch('clam_gui.automation.signature') as signature, patch('clam_gui.automation.allowed_file') as allowed:
            self.assertEqual(queue.take({'p':self.plan}),(None,[]))
            signature.assert_not_called();allowed.assert_not_called()
        self.assertEqual(len(queue.pending),1000)

    def test_due_queue_validation_is_bounded_even_if_all_files_disappeared(self):
        queue=WatchQueue(lambda:self.now)
        for i in range(100):queue.put('p',self.plan,self.file(i))
        self.now=4
        with patch('clam_gui.automation.signature',return_value=None) as signature:
            self.assertEqual(queue.take({'p':self.plan}),(None,[]))
            self.assertEqual(signature.call_count,64)
        self.assertEqual(len(queue.pending),36)

    def test_changes_without_events_still_require_a_fresh_quiet_period(self):
        queue=WatchQueue(lambda:self.now);path=self.file(1)
        queue.put('p',self.plan,path)
        Path(path).write_text('changed without a GIO event')
        self.now=4;self.assertEqual(queue.take({'p':self.plan})[1],[])
        self.now=6;self.assertEqual(queue.take({'p':self.plan})[1],[])
        self.now=7;self.assertEqual(len(queue.take({'p':self.plan})[1]),1)

    def test_old_completed_signature_does_not_hide_a_later_change(self):
        queue=WatchQueue(lambda:self.now);path=self.file(1)
        queue.put('p',self.plan,path)
        queue.completed['p',path]=queue.pending['p',path].signature
        Path(path).write_text('a new change after the cached completion')
        self.now=4;self.assertEqual(queue.take({'p':self.plan})[1],[])
        self.assertIn(('p',path),queue.pending)
        self.now=7;self.assertEqual(len(queue.take({'p':self.plan})[1]),1)

    def test_directory_waiting_queue_is_bounded_and_warns_once(self):
        from clam_gui.gio_watch import Observers
        volumes=Mock();volumes.get_mounts.return_value=[]
        engine=Mock();engine.errors={}
        observer=Observers(engine,volumes,limit=4)
        observer.plans={'p':self.plan}
        with patch('clam_gui.gio_watch.GLib.idle_add',return_value=1):
            for i in range(20000):observer.enqueue('p',self.folder/str(i),False)
            self.assertEqual(len(observer.tasks),4)
            self.assertEqual(len(observer.queued),4)
            self.assertTrue(observer.enqueue('p',self.folder/'0',False))
            engine.warn.assert_called_once()
        observer.idle=None;observer.close()
        self.assertFalse(observer.tasks);self.assertFalse(observer.queued)

    def test_overflow_is_reported_and_never_marks_baseline_complete(self):
        from clam_gui.gio_watch import Observers
        from clam_gui.monitor import Checkpoints
        engine=Automation(Store(self.root/'data'),lambda cb:None,lambda *args:None)
        engine.configure([('p',self.plan)],False)
        cp=Checkpoints(engine.store,engine);cp.configure([('p',self.plan)])
        volumes=Mock();volumes.get_mounts.return_value=[]
        observer=Observers(engine,volumes,limit=1)
        with patch('clam_gui.gio_watch.GLib.idle_add',return_value=1):
            observer.configure([('p',self.plan)])
            self.assertFalse(observer.enqueue('p',self.folder/'child',False))
        self.assertIn('p',engine.errors)
        cp.complete_baseline({('p',str(self.folder)):None})
        self.assertNotIn('p',cp.ready)
        self.assertEqual(len(engine.store.recent()),1)
        observer.idle=None;observer.close()

    def engine(self,scanner):
        self.callbacks=[];self.ready=threading.Event();self.finished=[]
        def dispatch(cb):self.callbacks.append(cb);self.ready.set()
        engine=Automation(Store(self.root/'data'),dispatch,lambda *args:self.finished.append(args),
                          clock=lambda:self.now,scanner=scanner)
        self.addCleanup(engine.close)
        engine.configure([('p',self.plan)],False)
        return engine

    def finish(self):
        self.assertTrue(self.ready.wait(3))
        self.callbacks.pop(0)();self.ready.clear()

    def test_start_pacing_retains_pending_files_and_batches_new_arrivals(self):
        scanner=Mock(return_value=('検査完了',''))
        engine=self.engine(scanner)
        engine.file_event('p',self.file(1));self.now=4
        with patch('shutil.which',return_value='/mock/clamscan'):
            engine.tick();self.finish()
            for i in range(2,5):engine.file_event('p',self.file(i))
            self.now=10;engine.tick()
            self.assertEqual(scanner.call_count,1)
            self.assertEqual(len(engine.queue.pending),3)
            self.assertIn('負荷を抑えて待機中',engine.status('p'))
            self.now=19;engine.tick();self.finish()
        self.assertEqual(scanner.call_count,2)
        self.assertEqual(len(scanner.call_args.kwargs['files']),3)
        self.assertFalse(engine.queue.pending)

    def test_long_scan_has_a_rest_interval_and_busy_retry_is_not_lost(self):
        scanner=Mock(side_effect=[ScanBusyError('busy'),('検査完了','')])
        engine=self.engine(scanner)
        engine.file_event('p',self.file(1));self.now=4
        with patch('shutil.which',return_value='/mock/clamscan'):
            engine.tick();self.now=30;self.finish()
            self.assertTrue(engine.queue.pending)
            self.now=31;engine.tick();self.assertEqual(scanner.call_count,1)
            self.now=33;engine.tick();self.finish()
        self.assertEqual(scanner.call_count,2)
        self.assertEqual(len(self.finished),1)

    def test_real_guard_lowers_only_the_child_priority(self):
        import os, sys, subprocess, json
        if not sys.platform.startswith('linux'):self.skipTest('Linux resource policy')
        from clam_gui.platforms import get_backend
        parent_nice=os.getpriority(os.PRIO_PROCESS,0)
        lock=get_backend().lock(self.root/'lock');self.addCleanup(lock.close)
        self.assertTrue(lock.acquire())
        code="import os,json;print(json.dumps(dict(nice=os.getpriority(os.PRIO_PROCESS,0))))"
        child=get_backend().spawn_scanner([sys.executable,'-c',code],lock=lock,
                                         stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            output, error=child.communicate(timeout=5)
        finally:
            if child.poll() is None:child.kill();child.communicate()
        self.assertEqual(child.returncode,0,error.decode())
        self.assertGreaterEqual(json.loads(output.splitlines()[-1])['nice'],10)
        self.assertEqual(os.getpriority(os.PRIO_PROCESS,0),parent_nice)

    def test_linux_priority_is_lowered_without_affecting_scan_on_failure(self):
        import sys
        if not sys.platform.startswith('linux'):self.skipTest('Linux resource policy')
        from clam_gui.platforms.scanner_guard import lower_priority
        with patch('clam_gui.platforms.scanner_guard.os.getpriority',return_value=0), \
             patch('clam_gui.platforms.scanner_guard.os.setpriority') as nice, \
             patch('clam_gui.platforms.scanner_guard.shutil.which',return_value='/mock/ionice'), \
             patch('clam_gui.platforms.scanner_guard.subprocess.run',return_value=Mock(returncode=0)) as run:
            lower_priority()
            nice.assert_called_once()
            self.assertEqual(nice.call_args.args[-1],10)
            self.assertEqual(run.call_args.args[0][1:4],['-c','3','-p'])
        with patch('clam_gui.platforms.scanner_guard.os.getpriority',return_value=15), \
             patch('clam_gui.platforms.scanner_guard.os.setpriority') as nice, \
             patch('clam_gui.platforms.scanner_guard.shutil.which',return_value=None):
            lower_priority();nice.assert_not_called()
        with patch('clam_gui.platforms.scanner_guard.os.getpriority',side_effect=OSError('denied')), \
             patch('clam_gui.platforms.scanner_guard.shutil.which',return_value='/mock/ionice'), \
             patch('clam_gui.platforms.scanner_guard.subprocess.run',side_effect=OSError('unsupported')):
            lower_priority()  # A best-effort policy never replaces the scan with a failure.
