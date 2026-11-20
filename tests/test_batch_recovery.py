"""A disappearing event-batch member must not strand the other files."""
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch
from clam_gui.automation import Automation
from clam_gui.core import Store,Plan,scan
from clam_gui.execution import ScanFilesChanged
from clam_gui.jobs import execute_scan
from clam_gui.results import ScanStatus,ResultKind


class BatchRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.folder=self.root/'watched';self.folder.mkdir()
        self.gone=self.folder/'gone';self.gone.touch()
        self.survivor=self.folder/'survivor';self.survivor.touch()
        self.store=Store(self.root/'data');self.plan=Plan('watch',[str(self.folder)],[],kind='watch')
        self.now=100;self.callbacks=[];self.ready=threading.Event();self.results=[]
        def dispatch(callback):self.callbacks.append(callback);self.ready.set()
        self.scanner=Mock()
        self.engine=Automation(self.store,dispatch,lambda *args:self.results.append(args),clock=lambda:self.now,scanner=self.scanner)
        self.addCleanup(self.engine.close)
        self.engine.configure([('p',self.plan)],False);self.engine.queue.quiet=0
        for file in (self.gone,self.survivor):self.engine.file_event('p',str(file))

    def finish_job(self, *, before_done=None):
        self.ready.clear()
        with patch('shutil.which',return_value='/synthetic/clamscan'):
            self.engine.tick()
            self.assertTrue(self.ready.wait(3))
        if before_done:before_done()
        self.callbacks.pop(0)()
        self.assertFalse(self.engine.active)

    def disappear(self,plan,store,cancel,**kwargs):
        self.gone.unlink(missing_ok=True)
        return scan(plan,store,cancel,lock_path=self.root/'lock',**kwargs)

    def test_survivor_is_requeued_and_scanned_after_another_member_disappears(self):
        self.scanner.side_effect=self.disappear
        self.finish_job()
        self.assertTrue(self.results[0][1].retry_files)
        self.assertFalse(self.results[0][1].assessment.completed)
        self.assertEqual(set(self.engine.queue.pending),{('p',str(self.survivor))})
        self.assertFalse(self.engine.queue.completed)
        with patch('shutil.which',return_value='/synthetic/clamscan'):
            self.engine.tick()
        self.assertEqual(self.scanner.call_count,1)  # Existing load-rest interval still applies.
        self.now=200;self.scanner.side_effect=None
        self.scanner.return_value=(ScanStatus('検査完了',ResultKind.CLEAN),'synthetic scan')
        self.finish_job()
        self.assertEqual(self.scanner.call_args.kwargs['files'],[str(self.survivor)])
        self.assertIn(('p',str(self.survivor)),self.engine.queue.completed)
        self.assertFalse(self.engine.queue.pending)

    def test_all_removed_files_stop_without_retry_loop(self):
        def disappear_all(plan,store,cancel,**kwargs):
            self.survivor.unlink()
            return self.disappear(plan,store,cancel,**kwargs)
        self.scanner.side_effect=disappear_all;self.finish_job()
        self.assertFalse(self.engine.queue.pending)
        self.now=200
        with patch('shutil.which',return_value='/synthetic/clamscan'):self.engine.tick()
        self.assertEqual(self.scanner.call_count,1)

    def test_newer_event_and_updated_exclusion_are_respected(self):
        self.scanner.side_effect=self.disappear
        def newer_event():
            self.engine.queue.quiet=10
            self.engine.file_event('p',str(self.survivor))
        self.finish_job(before_done=newer_event)
        self.assertEqual(self.engine.queue.pending['p',str(self.survivor)].due,110)
        # A second attempt fails before scanning while the user excludes the survivor.
        self.now=200
        self.scanner.side_effect=ScanFilesChanged('batch scope changed')
        self.finish_job(before_done=lambda:self.engine.configure([('p',replace(self.plan,excludes=[str(self.survivor)]))],False))
        self.assertFalse(self.engine.queue.pending)

    def test_stop_and_generic_errors_do_not_restore_batches(self):
        self.scanner.side_effect=self.disappear
        self.finish_job(before_done=self.engine.close)
        self.assertFalse(self.engine.queue.pending)
        # General scanner failures are reported rather than blindly retried.
        self.engine.closed=False;self.now=200
        self.engine.file_event('p',str(self.survivor))
        self.scanner.side_effect=ValueError('synthetic configuration error')
        self.finish_job()
        self.assertFalse(self.results[-1][1].retry_files)
        self.assertFalse(self.engine.queue.pending)

    def test_retry_metadata_survives_failure_to_save_error_and_status_append(self):
        failure=Mock(side_effect=OSError('cannot save'))
        scanner=Mock(side_effect=ScanFilesChanged('file disappeared'))
        status,output=execute_scan(self.plan,self.store,scanner=scanner,failure=failure,profile_id='p')
        self.assertTrue(status.retry_files)
        self.assertTrue((status+'\nextra detail').retry_files)
        self.assertIn('cannot save',output)
        with self.assertRaises(ValueError) as caught:
            scan(self.plan,self.store,files=[])
        self.assertNotIsInstance(caught.exception,ScanFilesChanged)
