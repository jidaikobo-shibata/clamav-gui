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

    def fill_new_arrivals(self):
        self.arrivals=[self.folder/('new-'+str(i)) for i in range(2)]
        for file in self.arrivals:
            file.touch();self.engine.file_event('p',str(file))

    def busy_full_queue(self):
        from clam_gui.execution import ScanBusyError
        self.engine.queue.limit=2
        self.scanner.side_effect=ScanBusyError('busy')
        self.finish_job(before_done=self.fill_new_arrivals)

    def test_contention_preserves_original_files_when_new_arrivals_fill_queue(self):
        self.busy_full_queue()
        self.assertEqual(set(self.engine.queue.deferred),{('p',str(self.gone)),('p',str(self.survivor))})
        self.assertEqual(len(self.engine.queue.pending),2)
        self.assertIn('4件',self.engine.status('p'))
        self.assertFalse(self.results);self.assertFalse(self.engine.errors)
        # A new event for a deferred file updates that entry even while pending is full.
        self.now=101;self.engine.queue.quiet=10
        self.survivor.write_text('changed while deferred')
        self.engine.file_event('p',str(self.survivor))
        self.assertEqual(self.engine.queue.deferred['p',str(self.survivor)].due,111)
        self.assertNotIn(('p',str(self.survivor)),self.engine.queue.pending)
        self.now=200;self.scanner.side_effect=None
        self.scanner.return_value=(ScanStatus('検査完了',ResultKind.CLEAN),'synthetic')
        self.finish_job()
        self.assertEqual(set(self.scanner.call_args.kwargs['files']),set(map(str,[self.gone,self.survivor]+self.arrivals)))
        self.assertFalse(self.engine.queue.pending);self.assertFalse(self.engine.queue.deferred)
        self.assertEqual(len(self.engine.queue.completed),4)

    def test_disappearing_member_uses_same_reserve_when_pending_is_full(self):
        self.engine.queue.limit=2;self.scanner.side_effect=self.disappear
        self.finish_job(before_done=self.fill_new_arrivals)
        self.assertEqual(set(self.engine.queue.deferred),{('p',str(self.survivor))})
        self.assertTrue(self.results[0][1].retry_files)
        self.assertFalse(self.engine.errors)

    def test_busy_retry_does_not_overwrite_a_newer_event_quiet_period(self):
        from clam_gui.execution import ScanBusyError
        self.scanner.side_effect=ScanBusyError('busy')
        def newer_event():
            self.now=101;self.engine.queue.quiet=10
            self.engine.file_event('p',str(self.survivor))
            self.now=102
        self.finish_job(before_done=newer_event)
        self.assertEqual(self.engine.queue.pending['p',str(self.survivor)].due,111)

    def test_deferred_files_are_revalidated_and_removed_plans_and_stop_clear_them(self):
        self.busy_full_queue()
        self.gone.unlink()
        self.engine.configure([('p',replace(self.plan,excludes=[str(self.survivor)]))],False)
        self.now=200;self.scanner.side_effect=None
        self.scanner.return_value=(ScanStatus('検査完了',ResultKind.CLEAN),'synthetic')
        self.finish_job()
        self.assertEqual(set(self.scanner.call_args.kwargs['files']),set(map(str,self.arrivals)))
        self.assertFalse(self.engine.queue.deferred)
        # Exercise cleanup through real contention, rather than injecting entries.
        def reserve_again():
            self.engine.configure([('p',self.plan)],False)
            self.now+=100
            for file in (self.gone,self.survivor):
                file.touch();self.engine.file_event('p',str(file))
            self.busy_full_queue()
            self.assertEqual(len(self.engine.queue.deferred),2)
        reserve_again()
        self.engine.forget_subtree('p',self.folder)
        self.assertFalse(self.engine.queue.deferred)
        reserve_again()
        self.engine.configure([],False);self.assertFalse(self.engine.queue.deferred)
        reserve_again()
        self.engine.close();self.assertFalse(self.engine.queue.deferred)

    def test_retry_reserve_is_bounded_and_reports_files_it_cannot_retain(self):
        self.engine.queue.RETRY_LIMIT=1
        self.busy_full_queue()
        self.assertEqual(len(self.engine.queue.pending),2)
        self.assertEqual(len(self.engine.queue.deferred),1)
        self.assertIn('上限',self.engine.errors['p'])
        self.assertEqual(len(self.store.recent()),1)
        self.assertEqual(len(self.results),1)

    def test_stop_before_contention_callback_does_not_restore_any_files(self):
        from clam_gui.execution import ScanBusyError
        self.scanner.side_effect=ScanBusyError('busy')
        self.finish_job(before_done=self.engine.close)
        self.assertFalse(self.engine.queue.pending);self.assertFalse(self.engine.queue.deferred)

    def fail_start(self, target='clam_gui.automation.threading.Thread.start', before_failure=None):
        def failure(*args,**kwargs):
            if before_failure:before_failure()
            raise RuntimeError('temporary worker startup failure')
        with patch('shutil.which',return_value='/synthetic/clamscan'),patch(target,side_effect=failure):
            self.engine.tick()
        self.assertFalse(self.engine.active);self.assertIsNone(self.engine.active_key)
        self.scanner.assert_not_called()

    def test_worker_construction_failure_preserves_files_and_retries_after_rest(self):
        self.fail_start('clam_gui.automation.threading.Thread')
        self.assertEqual(len(self.engine.queue.pending),2)
        self.assertEqual(len(self.store.recent()),1)
        with patch('shutil.which',return_value='/synthetic/clamscan'),patch('clam_gui.automation.threading.Thread') as thread:
            self.engine.tick();thread.assert_not_called()
        self.now=200;self.scanner.return_value=(ScanStatus('検査完了',ResultKind.CLEAN),'synthetic')
        self.finish_job()
        self.assertEqual(len(self.engine.queue.completed),2)
        self.assertFalse(self.engine.queue.pending)

    def test_worker_start_failure_retains_batch_when_arrivals_fill_queue(self):
        self.engine.queue.limit=2
        self.fail_start(before_failure=self.fill_new_arrivals)
        self.assertEqual(len(self.engine.queue.pending),2)
        self.assertEqual(len(self.engine.queue.deferred),2)
        self.now=200
        self.fail_start(before_failure=self.fill_new_arrivals)
        self.assertEqual(len(self.engine.queue.pending),2)
        self.assertEqual(len(self.engine.queue.deferred),2)
        self.now=300;self.scanner.return_value=(ScanStatus('検査完了',ResultKind.CLEAN),'synthetic')
        self.finish_job()
        self.assertEqual(len(self.engine.queue.completed),4)
        self.assertFalse(self.engine.queue.pending);self.assertFalse(self.engine.queue.deferred)

    def test_start_failure_does_not_restore_files_after_stop(self):
        self.fail_start(before_failure=self.engine.close)
        self.assertFalse(self.engine.queue.pending);self.assertFalse(self.engine.queue.deferred)

    def test_start_failure_revalidates_changed_scope_and_survives_history_failure(self):
        def exclude_one():
            self.engine.configure([('p',replace(self.plan,excludes=[str(self.gone)]))],False)
        with patch.object(self.store,'save',side_effect=OSError('cannot save history')):
            self.fail_start(before_failure=exclude_one)
        self.assertEqual(set(self.engine.queue.pending),{('p',str(self.survivor))})
        self.assertIn('cannot save history',self.results[-1][1])
        self.now=200;self.scanner.return_value=(ScanStatus('検査完了',ResultKind.CLEAN),'synthetic')
        self.finish_job()
        self.assertEqual(self.scanner.call_args.kwargs['files'],[str(self.survivor)])

    def queue_media(self):
        from clam_gui.automation import Media
        self.engine.configure([],True)
        info=self.folder.stat()
        medium=Media('first',str(self.folder),'first',(info.st_dev,info.st_ino))
        self.engine.add_media(medium)
        return medium

    def test_media_start_failure_requeues_once_and_can_later_complete(self):
        medium=self.queue_media()
        self.fail_start();self.assertEqual(self.engine.media_pending,['first'])
        self.assertIsNone(self.engine.active_media)
        self.now=200;self.fail_start();self.assertEqual(self.engine.media_pending,['first'])
        self.now=300;self.scanner.return_value=(ScanStatus('検査完了',ResultKind.CLEAN),'synthetic')
        self.finish_job()
        self.assertTrue(self.scanner.call_args.kwargs['available']())
        self.assertEqual(self.engine.media_pending,[])

    def test_removed_media_is_not_restored_after_start_failure(self):
        medium=self.queue_media()
        self.fail_start(before_failure=lambda:self.engine.remove_media(medium.token))
        self.assertFalse(medium.available());self.assertFalse(self.engine.media_pending)

    def test_disabled_media_is_not_restored_after_start_failure(self):
        self.queue_media()
        self.fail_start(before_failure=lambda:self.engine.configure([],False))
        self.assertFalse(self.engine.media_pending)

    def test_media_start_failure_reserves_one_slot_when_new_mounts_fill_queue(self):
        from clam_gui.automation import Media
        first=self.queue_media();self.engine.MEDIA_LIMIT=2
        def arrivals():
            for token in ('new-a','new-b'):
                self.engine.add_media(Media(token,first.path,token,first.device))
        self.fail_start(before_failure=arrivals)
        self.assertEqual(self.engine.media_pending,['first','new-a','new-b'])
        self.engine.add_media(Media('overflow',first.path,'overflow',first.device))
        self.assertEqual(len(self.engine.media_pending),3)
        self.assertIn('上限',self.engine.errors['external-media'])
        self.now=200;self.scanner.return_value=(ScanStatus('検査完了',ResultKind.CLEAN),'synthetic')
        self.finish_job()
        self.assertEqual(self.scanner.call_args.args[0].label,'外部メディア検査：first')
        self.assertEqual(self.engine.media_pending,['new-a','new-b'])
