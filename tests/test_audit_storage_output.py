"""Bounded output, retained detections, monitor moves and worker startup failure."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch
from clam_gui.core import Store,Plan,scan
from clam_gui.jobs import execute_scan,start_worker
from clam_gui.options import DEFAULTS,LIMITS
from clam_gui.scan_output import ScanOutput


class OutputAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.folder=self.root/'files';self.folder.mkdir()
        self.store=Store(self.root/'data');self.plan=Plan('test',[str(self.folder)],[])

    def scan_with(self,spawn):
        with patch('clam_gui.core.shutil.which',return_value='/mock/clamscan'), \
             patch('clam_gui.core.supported_options',return_value=set(DEFAULTS)|set(LIMITS)), \
             patch('clam_gui.platforms.linux.LinuxBackend.spawn_scanner',side_effect=spawn):
            return execute_scan(self.plan,self.store,lock_path=self.root/'lock',profile_id='p')

    def found(self,command,**kwargs):
        kwargs['stdout'].write(b'/synthetic/infected: Eicar-Signature FOUND\nInfected files: 1\n')
        return Mock(wait=Mock(return_value=1))

    def test_detection_survives_history_save_failure(self):
        with patch.object(self.store,'save',side_effect=OSError('synthetic storage failure')):
            status,output=self.scan_with(self.found)
        self.assertTrue(status.assessment.detected)
        self.assertEqual(status.assessment.kind.value,'incomplete')
        self.assertIn('Eicar-Signature',output)
        self.assertIn('履歴を保存できませんでした',status)
        self.assertIsNotNone(status.started_at)
        self.assertEqual(status.splitlines()[0],'検出あり・履歴保存失敗')

    def test_wait_error_stops_child_and_keeps_received_detection(self):
        process=Mock(poll=Mock(return_value=None),wait=Mock(side_effect=[RuntimeError('synthetic wait failure'),0]))
        def spawn(command,**kwargs):
            kwargs['stdout'].write(b'/synthetic/first: Eicar-Signature FOUND\n')
            return process
        status,output=self.scan_with(spawn)
        process.terminate.assert_called_once()
        self.assertTrue(status.assessment.detected)
        self.assertIn('Eicar-Signature',output)
        self.assertIn('synthetic wait failure',output)

    def test_runtime_cleanup_failure_does_not_replace_completed_detection(self):
        real=self.store.connect
        @contextmanager
        def fail_cleanup():
            with real() as db:
                yield db
                # The final removal of scan_run_details is in the cleanup context.
                if db.execute('SELECT count(*) FROM scans').fetchone()[0] and not db.execute('SELECT count(*) FROM scan_runs').fetchone()[0]:
                    raise sqlite3.OperationalError('synthetic cleanup failure')
        with patch.object(self.store,'connect',fail_cleanup):
            status,output=self.scan_with(self.found)
        self.assertTrue(status.assessment.detected)
        self.assertIn('Eicar-Signature',output)
        self.assertIn('synthetic cleanup failure',status)

    def test_output_is_bounded_during_scan_and_early_threat_is_retained(self):
        with ScanOutput() as log:
            log.write(b'/synthetic/first: Eicar-Signature FOUND\n')
            chunk=b'ordinary diagnostic line\n'*2048
            for _ in range(128):log.write(chunk)
            log.write(b'Infected files: 1\nScanned files: 12\n')
            text=log.finish()
            self.assertLessEqual(len(log.tail),log.TAIL)
            self.assertLessEqual(log.evidence_bytes,log.EVIDENCE)
            self.assertLess(len(text.encode()),2*1024*1024)
            self.assertTrue(log.detected)
            self.assertIn('Eicar-Signature',text)
            self.assertIn('Scanned files: 12',text)
            self.assertEqual(text.count('Eicar-Signature'),1)

    def test_warning_overflow_cannot_hide_the_first_threat(self):
        with ScanOutput() as log:
            for _ in range(300):log.write(b'WARNING: '+b'x'*2000+b'\n')
            log.write(b'/synthetic/first: Eicar-Signature FOUND\n')
            for _ in range(64):log.write(b'x'*32767+b'\n')
            text=log.finish()
        self.assertTrue(log.detected)
        self.assertIn('Eicar-Signature',text[:65536])
        self.store.save('now','test','検出あり',text+'x'*2*1024*1024,'p')
        self.assertIn('Eicar-Signature',self.store.recent()[0][3])
        self.assertLessEqual(len(self.store.recent()[0][3].encode()),2*1024*1024)

    def test_long_line_and_partial_utf8_are_bounded_and_decoded(self):
        with ScanOutput() as log:
            for _ in range(100):log.write(b'x'*32768)
            raw='警告'.encode()
            log.write(b'\n'+raw[:2]);log.write(raw[2:]+b'\n')
            text=log.finish()
        self.assertLessEqual(len(log.pending),log.LINE)
        self.assertIn('警告',text)
        self.assertIn('WARNING:',text)

    def test_real_subprocess_output_drains_without_pipe_deadlock(self):
        with ScanOutput() as log:
            child=subprocess.Popen([sys.executable,'-c',"import sys;sys.stdout.write('x'*4000000+'\\nScanned files: 1\\n')"],stdout=log,stderr=subprocess.STDOUT)
            try:child.wait(timeout=5)
            finally:
                if child.poll() is None:child.kill();child.wait()
            text=log.finish()
        self.assertEqual(child.returncode,0)
        self.assertFalse(log.thread.is_alive())
        self.assertIn('Scanned files: 1',text)

    def test_capture_start_failure_closes_both_pipe_descriptors(self):
        with patch('clam_gui.scan_output.os.close',wraps=__import__('os').close) as close, \
             patch('clam_gui.scan_output.threading.Thread') as thread:
            thread.return_value.start.side_effect=RuntimeError('cannot start')
            with self.assertRaises(RuntimeError):
                with ScanOutput():pass
            self.assertEqual(close.call_count,2)

    def test_worker_start_and_construction_failure_are_reported(self):
        failed=Mock();target=Mock()
        with patch('threading.Thread',side_effect=RuntimeError('cannot construct')):
            self.assertFalse(start_worker(target,failed))
        failed.assert_called_once();target.assert_not_called()
        failed.reset_mock()
        with patch('threading.Thread') as thread:
            thread.return_value.start.side_effect=RuntimeError('cannot start')
            self.assertFalse(start_worker(target,failed))
        failed.assert_called_once();target.assert_not_called()

    def test_rename_removes_old_subtree_monitors_and_queued_discovery(self):
        from gi.repository import Gio
        from clam_gui.gio_watch import Observers
        from clam_gui.automation import Automation
        old=self.folder/'old';old.mkdir();(old/'child').mkdir()
        (old/'child'/'file').touch()
        plan=Plan('watch',[str(self.folder)],[],kind='watch')
        engine=Automation(self.store,lambda cb:cb(),lambda *args:None)
        engine.configure([('p',plan)],False)
        volumes=Mock();volumes.get_mounts.return_value=[]
        source=Observers(engine,volumes);self.addCleanup(source.close)
        source.plans={'p':plan}
        root_monitor=Mock();old_monitor=Mock();sub_monitor=Mock()
        source.monitors={('p',str(self.folder)):root_monitor,('p',str(old)):old_monitor,('p',str(old/'child')):sub_monitor}
        engine.file_event('p',old/'child'/'file')
        with patch('clam_gui.gio_watch.GLib.idle_add',return_value=1):
            source.enqueue('p',old/'child',True)
            new=self.folder/'new';old.rename(new)
            source.event(None,Gio.File.new_for_path(str(old)),Gio.File.new_for_path(str(new)),Gio.FileMonitorEvent.RENAMED,'p')
            self.assertNotIn(('p',str(old/'child')),source.queued)
            while source.drain():pass
        old_monitor.cancel.assert_called_once();sub_monitor.cancel.assert_called_once()
        root_monitor.cancel.assert_not_called()
        self.assertEqual(len(source.monitors),3)
        self.assertIn(('p',str(new/'child'/'file')),engine.queue.pending)
        self.assertFalse(any('/old' in path for _,path in source.monitors))
        self.assertFalse(any('/old' in path for _,path in engine.queue.pending))
        source.idle=None
