"""Transient write failures must not strand scans, lose notices, or skip cleanup."""
from contextlib import contextmanager
from pathlib import Path
import json
import os
import sqlite3
import stat
import tempfile
import time
import unittest
from unittest.mock import Mock,patch
from clam_gui.core import Store
from clam_gui.runtime import activities,track_scan,retry_cleanup
from clam_gui.notifications import AutomaticNotice
from clam_gui.notification_retry import NotificationRetry
from clam_gui.notification_outbox import NotificationOutbox
from clam_gui.monitor import Monitor
from clam_gui.results import ScanStatus,ResultKind


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=Store(self.root/'data')
        self.now=0

    @contextmanager
    def failed_deletion(self):
        connect=self.store.connect
        @contextmanager
        def failing():
            with connect() as db:
                before={row[0] for row in db.execute('SELECT token FROM scan_runs')}
                yield db
                after={row[0] for row in db.execute('SELECT token FROM scan_runs')}
                if before-after:
                    raise sqlite3.OperationalError('synthetic cleanup failure')
        with patch.object(self.store,'connect',failing):yield

    def fail_completed(self):
        with self.assertRaises(sqlite3.OperationalError),self.failed_deletion():
            with track_scan(self.store,'completed','now',deduplicate=True) as scan:
                token=scan.token
        return token

    def test_cleanup_retries_across_store_instances_without_process_restart(self):
        token=self.fail_completed()
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT token FROM scan_runs').fetchone()[0],token)
        other=Store(self.store.path.parent)
        self.assertEqual(activities(other),[])
        with track_scan(other,'completed','later',deduplicate=True) as new:
            self.assertTrue(new.is_next())
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM scan_runs').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM scan_run_details').fetchone()[0],0)

    def test_retry_does_not_delete_a_running_scan_or_block_later_work(self):
        with track_scan(self.store,'live','now',deduplicate=True) as live:
            token=self.fail_completed()
            with self.failed_deletion():
                visible=activities(self.store)
            self.assertEqual([row.token for row in visible],[live.token])
            retry_cleanup(self.store)
            with self.store.connect() as db:
                self.assertIsNotNone(db.execute('SELECT 1 FROM scan_runs WHERE token=?',(live.token,)).fetchone())
                self.assertIsNone(db.execute('SELECT 1 FROM scan_runs WHERE token=?',(token,)).fetchone())
        with track_scan(self.store,'later','now',deduplicate=True) as later:
            self.assertTrue(later.is_next())

    def test_actual_sqlite_lock_does_not_block_ui_cleanup_retry(self):
        self.fail_completed()
        writer=sqlite3.connect(self.store.path)
        try:
            writer.execute('BEGIN IMMEDIATE')
            started=time.monotonic()
            self.assertEqual(activities(self.store),[])
            self.assertLess(time.monotonic()-started,2)
        finally:
            writer.rollback();writer.close()
        self.assertEqual(activities(self.store),[])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM scan_runs').fetchone()[0],0)

    def test_owner_identity_is_checked_before_deleting_a_token(self):
        token=self.fail_completed()
        with self.store.connect() as db:
            db.execute('UPDATE scan_runs SET identity=? WHERE token=?',('different-owner',token))
        retry_cleanup(self.store)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT identity FROM scan_runs WHERE token=?',(token,)).fetchone()[0],'different-owner')

    def test_cleanup_failure_does_not_replace_the_original_exception(self):
        with self.failed_deletion(),self.assertRaisesRegex(ValueError,'original reason'):
            with track_scan(self.store,'p','now'):
                raise ValueError('original reason')
        retry_cleanup(self.store)

    def buffer(self):
        return NotificationRetry(self.store.path.parent,clock=lambda:self.now)

    def test_failed_enqueue_is_retained_spooled_and_replayed_after_recovery(self):
        retry=self.buffer();outbox=NotificationOutbox(self.store,Mock())
        retry.add(AutomaticNotice('detected','synthetic finding','critical'))
        bad=Mock(side_effect=sqlite3.OperationalError('synthetic queue failure'))
        retry.drain(bad)
        self.assertEqual(retry.count,1)
        self.assertTrue(retry.path.is_file())
        self.assertEqual(stat.S_IMODE(retry.path.stat().st_mode),0o600)
        retry.drain(bad);self.assertEqual(bad.call_count,1)
        self.now=5;retry.drain(outbox.enqueue)
        self.assertEqual(retry.count,0);self.assertFalse(retry.path.exists())
        self.assertEqual(outbox.state()['pending'],1)
        self.assertTrue(outbox.deliver_one())
        self.assertEqual(outbox.deliver.call_args.args,('detected','synthetic finding','critical'))

    def test_actual_sqlite_write_lock_uses_recovery_file_then_transfers(self):
        retry=self.buffer();outbox=NotificationOutbox(self.store,Mock())
        retry.add(AutomaticNotice('detected','actual write lock','critical'))
        connect=self.store.connect
        @contextmanager
        def impatient():
            with connect() as db:
                db.execute('PRAGMA busy_timeout=0')
                yield db
        writer=sqlite3.connect(self.store.path)
        try:
            writer.execute('BEGIN IMMEDIATE')
            with patch.object(self.store,'connect',impatient):retry.drain(outbox.enqueue)
            self.assertEqual(retry.count,1)
            self.assertTrue(retry.path.exists())
        finally:
            writer.rollback();writer.close()
        self.now=5;retry.drain(outbox.enqueue)
        self.assertEqual(outbox.state()['pending'],1)
        self.assertFalse(retry.path.exists())

    def test_backoff_arrivals_and_restart_keep_every_count_with_bounded_rows(self):
        retry=self.buffer()
        for _ in range(300):retry.add(AutomaticNotice('normal','body','normal'))
        for _ in range(300):retry.add(AutomaticNotice('detected','body','critical'))
        self.assertEqual(len(retry.rows),258);self.assertEqual(retry.count,600)
        retry.drain(Mock(side_effect=sqlite3.OperationalError('offline')))
        retry.add(AutomaticNotice('late','arrived during backoff','critical'))
        recovered=self.buffer();recovered.load()
        self.assertEqual(recovered.count,601)
        outbox=NotificationOutbox(self.store,Mock())
        while recovered.rows:recovered.drain(outbox.enqueue,force=True)
        self.assertEqual(outbox.state()['pending'],601)
        outbox.deliver_one()
        self.assertIn('301件',outbox.deliver.call_args.args[1])
        self.assertEqual(outbox.deliver.call_args.args[2],'critical')

    def test_corrupt_recovery_file_is_preserved(self):
        retry=self.buffer();retry.path.write_text('not valid JSON')
        original=retry.path
        retry.add(AutomaticNotice('detected','body','critical'))
        deliver=Mock()
        retry.drain(deliver)
        deliver.assert_called_once()
        self.assertEqual(retry.count,0)
        self.assertEqual(original.read_text(),'not valid JSON')
        self.assertTrue(retry.error)

    def test_spool_failure_does_not_discard_memory_pending_notices(self):
        retry=self.buffer();retry.add(AutomaticNotice('detected','body','critical'))
        with patch.object(retry,'persist',side_effect=OSError('synthetic no space')):
            retry.drain(Mock(side_effect=sqlite3.OperationalError('database unavailable')))
        self.assertEqual(retry.count,1)
        self.assertIn('no space',retry.error)

    def monitor(self):
        class Source:
            def __init__(self,engine):self.tasks=[];self.monitors={};self.plans={}
            def close(self):pass
        monitor=Monitor(self.store,observers=Source,notifier=Mock())
        self.addCleanup(monitor.lease.close);self.addCleanup(monitor.outbox.close)
        monitor.notification_retry.clock=lambda:self.now
        return monitor

    def test_monitor_retries_critical_and_aggregated_normal_notices(self):
        monitor=self.monitor()
        with patch.object(monitor,'notifier',side_effect=sqlite3.OperationalError('write failed')):
            monitor.finished('critical',ScanStatus('検出あり',ResultKind.DETECTED,True),'','audit')
            monitor.finished('clean',ScanStatus('検査完了',ResultKind.CLEAN),'','audit')
            monitor.flush(True)
        self.assertEqual(monitor.notification_retry.count,2)
        self.now=monitor.notification_retry.due;monitor.flush()
        self.assertEqual(monitor.notification_retry.count,0)
        self.assertEqual(monitor.outbox.state()['pending'],2)

    def test_shutdown_always_closes_outbox_and_lease_after_flush_failure(self):
        monitor=self.monitor();monitor.lease.acquire()
        with patch.object(monitor,'flush',side_effect=sqlite3.OperationalError('flush failed')), \
             patch.object(monitor.outbox,'close',wraps=monitor.outbox.close) as close:
            monitor.shutdown()
        close.assert_called_once()
        self.assertIsNone(monitor.lease.file)
        self.assertIsInstance(monitor.failure,sqlite3.OperationalError)

    def test_shutdown_persists_failed_normal_flush_for_restart(self):
        monitor=self.monitor();monitor.lease.acquire()
        monitor.notices.record('p','audit',ScanStatus('検査完了',ResultKind.CLEAN))
        with patch.object(monitor,'notifier',side_effect=sqlite3.OperationalError('offline')):
            monitor.shutdown()
        self.assertIsNone(monitor.lease.file)
        recovered=self.buffer();recovered.load()
        self.assertEqual(recovered.count,1)

    def test_logging_failure_does_not_interrupt_notification_backup(self):
        retry=self.buffer();retry.add(AutomaticNotice('detected','body','critical'))
        with patch('builtins.print',side_effect=BrokenPipeError('closed log stream')):
            retry.drain(Mock(side_effect=sqlite3.OperationalError('offline')))
        self.assertEqual(retry.count,1)
        self.assertTrue(retry.path.exists())

    def test_logging_failure_does_not_interrupt_shutdown(self):
        monitor=self.monitor();monitor.lease.acquire()
        with patch('builtins.print',side_effect=BrokenPipeError('closed log stream')), \
             patch.object(monitor,'flush',side_effect=sqlite3.OperationalError('offline')):
            monitor.shutdown()
        self.assertIsNone(monitor.lease.file)
        self.assertTrue(monitor.outbox.stop.is_set())

    def test_startup_failure_also_releases_resources(self):
        monitor=self.monitor();monitor.lease.acquire()
        with patch('clam_gui.monitor.get_backend') as backend:
            backend.return_value.install_stop_handlers.side_effect=RuntimeError('startup failed')
            with self.assertRaisesRegex(RuntimeError,'startup failed'):
                monitor.run()
        self.assertIsNone(monitor.lease.file)
        self.assertTrue(monitor.outbox.stop.is_set())

    def test_shutdown_continues_after_outbox_and_state_cleanup_failures(self):
        monitor=self.monitor();monitor.lease.acquire()
        with patch.object(monitor.outbox,'close',side_effect=RuntimeError('close failed')), \
             patch('clam_gui.monitor.preference',side_effect=sqlite3.OperationalError('state failed')):
            monitor.shutdown()
        self.assertIsNone(monitor.lease.file)
        self.assertIsNotNone(monitor.failure)

    def test_corrupt_primary_uses_restartable_alternate_and_merges_after_repair(self):
        retry=self.buffer();primary=retry.path;primary.write_text('{broken')
        retry.add(AutomaticNotice('critical','body','critical'))
        retry.drain(Mock(side_effect=sqlite3.OperationalError('offline')))
        self.assertNotEqual(retry.path,primary)
        self.assertEqual(primary.read_text(),'{broken')
        primary.write_text(json.dumps(dict(version=1,rows=[])))
        recovered=self.buffer();recovered.load()
        self.assertEqual(recovered.count,1)
        recovered.drain(Mock())
        again=self.buffer();again.load();self.assertEqual(again.count,0)

    def test_two_corrupt_files_keep_new_notices_deliverable_in_memory(self):
        retry=self.buffer();retry.path.write_text('broken primary')
        alternate=retry.path.parent/'notification-retry-pending.json'
        alternate.write_text('broken alternate')
        retry.add(AutomaticNotice('critical','body','critical'))
        retry.drain(Mock(side_effect=sqlite3.OperationalError('offline')))
        self.assertEqual(retry.count,1)
        self.assertEqual(alternate.read_text(),'broken alternate')
        self.now=retry.due;deliver=Mock();retry.drain(deliver)
        deliver.assert_called_once();self.assertEqual(retry.count,0)
        self.assertTrue(retry.error)

    def test_corrupt_spool_does_not_stop_monitor_tick(self):
        from clam_gui.background import put_preference
        monitor=self.monitor()
        monitor.notification_retry.path.write_text('broken')
        monitor.observers.configure=Mock()
        put_preference(self.store,'background-enabled',True)
        self.assertTrue(monitor.checked_tick())
        self.assertFalse(monitor.stopping)
        self.assertIsNone(monitor.failure)
        self.assertTrue(monitor.last_state['notifications']['error'])

    def test_checkpoint_retry_keeps_monitor_alive_and_respects_backoff(self):
        from clam_gui.background import put_preference
        monitor=self.monitor();monitor.observers.configure=Mock()
        put_preference(self.store,'background-enabled',True)
        with patch('clam_gui.monitor.time.monotonic',return_value=100) as clock, \
             patch.object(monitor.checkpoints,'complete_baseline',side_effect=sqlite3.OperationalError('database is locked')) as baseline, \
             patch.object(monitor.engine,'tick') as tick:
            self.assertTrue(monitor.checked_tick())
            self.assertEqual(monitor.checkpoint_due,105)
            self.assertTrue(monitor.checkpoint_error)
            clock.return_value=101
            self.assertTrue(monitor.checked_tick());self.assertEqual(baseline.call_count,1)
            clock.return_value=105
            self.assertTrue(monitor.checked_tick());self.assertEqual(baseline.call_count,2)
            self.assertEqual(monitor.checkpoint_due,115)
            self.assertEqual(tick.call_count,3)
        self.assertFalse(monitor.stopping);self.assertIsNone(monitor.failure)
        self.assertIn('再開確認情報',monitor.last_state['statuses']['external-media'])
        with patch('clam_gui.monitor.time.monotonic',return_value=115):
            self.assertTrue(monitor.checked_tick())
        self.assertEqual(monitor.checkpoint_error,'')
        self.assertEqual(monitor.checkpoint_attempts,0)

    def test_real_database_lock_retains_dirty_metadata_and_recovers(self):
        from clam_gui.core import Plan
        from clam_gui.background import put_preference
        monitor=self.monitor();monitor.observers.configure=Mock()
        folder=self.root/'watch';folder.mkdir();file=folder/'file';file.touch()
        plan=Plan('test',[str(folder)],[],kind='watch');plans=[('p',plan)]
        monitor.checkpoints.configure(plans);monitor.engine.configure(plans,False)
        monitor.observers.monitors={('p',str(folder)):object()}
        monitor.checkpoints.inventory('p',str(file))
        put_preference(self.store,'background-enabled',True)
        with patch('clam_gui.monitor.watched_plans',return_value=plans), \
             patch('clam_gui.monitor.time.monotonic',return_value=100) as clock:
            with self.store.connect() as locker:
                locker.execute('BEGIN IMMEDIATE')
                started=time.perf_counter()
                self.assertTrue(monitor.checked_tick())
                self.assertLess(time.perf_counter()-started,1)
                self.assertTrue(monitor.checkpoints.dirty)
                self.assertNotIn('p',monitor.checkpoints.ready)
                self.assertFalse(monitor.stopping)
            clock.return_value=105
            self.assertTrue(monitor.checked_tick())
        self.assertFalse(monitor.checkpoints.dirty)
        self.assertIn('p',monitor.checkpoints.ready)
        self.assertEqual(monitor.checkpoint_error,'')
        self.assertIsNotNone(monitor.last_state)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM monitor_files').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM monitor_roots').fetchone()[0],1)

    def test_scan_completion_during_backoff_is_saved_on_baseline_retry(self):
        monitor=self.monitor()
        with patch('clam_gui.monitor.time.monotonic',return_value=100) as clock, \
             patch.object(monitor.checkpoints,'scanned',side_effect=sqlite3.OperationalError('locked')):
            monitor.save_checkpoints()
            monitor.save_checkpoints()
            self.assertTrue(monitor.checkpoint_scans_pending)
        with patch('clam_gui.monitor.time.monotonic',return_value=105), \
             patch.object(monitor.checkpoints,'scanned') as scanned:
            monitor.save_checkpoints(baseline=True)
            scanned.assert_called_once()
        self.assertFalse(monitor.checkpoint_scans_pending)

    def test_inventory_failure_preserves_healthy_gio_monitor_on_next_tick(self):
        from gi.repository import GLib
        from clam_gui.gio_watch import Observers
        from clam_gui.core import Plan
        from clam_gui.background import put_preference
        class Volumes:
            def connect(self,*args):return len(args)
            def disconnect(self,*args):pass
            def get_mounts(self):return []
        failing=self.root/'failing';failing.mkdir()
        healthy=self.root/'healthy';healthy.mkdir()
        for index in range(520):(failing/str(index)).touch()
        plans=[('healthy',Plan('healthy',[str(healthy)],[],kind='watch')),
               ('failing',Plan('failing',[str(failing)],[],kind='watch'))]
        monitor=Monitor(self.store,observers=lambda engine:Observers(engine,Volumes()),notifier=Mock())
        self.addCleanup(monitor.shutdown)
        monitor.observers.clock=lambda:100
        monitor.checkpoints.configure(plans)
        put_preference(self.store,'background-enabled',True)
        with patch('clam_gui.monitor.watched_plans',return_value=plans), \
             patch('clam_gui.monitor.time.monotonic',return_value=100), \
             patch.object(monitor.engine,'tick'), \
             patch.object(monitor.checkpoints,'flush',side_effect=sqlite3.OperationalError('database is locked')):
            self.assertTrue(monitor.checked_tick())
            deadline=time.perf_counter()+3
            while monitor.observers.idle is not None and time.perf_counter()<deadline:
                GLib.MainContext.default().iteration(False)
            self.assertIsNone(monitor.observers.idle)
            self.assertIn('failing',monitor.engine.errors)
            watcher=monitor.observers.monitors['healthy',str(healthy)]
            self.assertTrue(monitor.checked_tick())
            self.assertFalse(monitor.stopping)
            self.assertIs(monitor.observers.monitors['healthy',str(healthy)],watcher)
            file=healthy/'new';file.touch()
            deadline=time.perf_counter()+3
            while ('healthy',str(file)) not in monitor.engine.queue.pending and time.perf_counter()<deadline:
                GLib.MainContext.default().iteration(False)
            self.assertIn(('healthy',str(file)),monitor.engine.queue.pending)

    def test_failed_baseline_transaction_does_not_mark_plan_ready(self):
        from clam_gui.core import Plan
        monitor=self.monitor()
        folder=self.root/'watch';folder.mkdir()
        plans=[('p',Plan('test',[str(folder)],[],kind='watch'))]
        monitor.checkpoints.configure(plans);monitor.engine.configure(plans,False)
        monitors={('p',str(folder)):object()}
        connect=self.store.connect
        @contextmanager
        def failing_commit():
            with connect() as db:
                yield db
                raise sqlite3.OperationalError('commit failed')
        with patch.object(self.store,'connect',failing_commit), self.assertRaises(sqlite3.Error):
            monitor.checkpoints.complete_baseline(monitors)
        self.assertNotIn('p',monitor.checkpoints.ready)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM monitor_roots').fetchone()[0],0)
        monitor.checkpoints.complete_baseline(monitors)
        self.assertIn('p',monitor.checkpoints.ready)

    def test_locked_configuration_keeps_old_state_and_retries_latest_settings(self):
        from clam_gui.core import Plan
        from clam_gui.background import put_preference
        monitor=self.monitor();monitor.observers.configure=Mock()
        folders=[self.root/name for name in ('old','new','latest')]
        for folder in folders:folder.mkdir()
        plans=[Plan(folder.name,[str(folder)],[],kind='watch') for folder in folders]
        put_preference(self.store,'background-enabled',True)
        with patch('clam_gui.monitor.time.monotonic',return_value=100) as clock, \
             patch('clam_gui.monitor.watched_plans',return_value=[('p',plans[0])]) as selected, \
             patch.object(monitor.engine,'tick') as tick:
            self.assertTrue(monitor.checked_tick())
            previous=dict(monitor.checkpoints.roots)
            with self.store.connect() as lock:
                lock.execute('BEGIN IMMEDIATE')
                selected.return_value=[('p',plans[1])];clock.return_value=103
                started=time.perf_counter()
                self.assertTrue(monitor.checked_tick())
                self.assertLess(time.perf_counter()-started,1)
                self.assertEqual(monitor.config_due,108)
                self.assertEqual(monitor.checkpoints.roots,previous)
                self.assertEqual(monitor.engine.plans['p'],plans[0])
                calls=selected.call_count;clock.return_value=104
                self.assertTrue(monitor.checked_tick());self.assertEqual(selected.call_count,calls)
                clock.return_value=108
                self.assertTrue(monitor.checked_tick());self.assertEqual(monitor.config_due,118)
            selected.return_value=[('p',plans[2])];clock.return_value=118
            self.assertTrue(monitor.checked_tick())
            self.assertEqual(tick.call_count,5)
        self.assertEqual(monitor.engine.plans['p'],plans[2])
        self.assertIn(str(folders[2]),monitor.checkpoints.roots['p'])
        self.assertEqual(monitor.config_error,'');self.assertEqual(monitor.config_attempts,0)
        self.assertFalse(monitor.stopping);self.assertIsNone(monitor.failure)

    def test_configuration_rollback_preserves_all_previous_checkpoint_state(self):
        from clam_gui.core import Plan
        monitor=self.monitor()
        old=self.root/'old';old.mkdir();file=old/'file';file.touch()
        new=self.root/'new';new.mkdir()
        plans=[('p',Plan('old',[str(old)],[],kind='watch'))]
        monitor.checkpoints.configure(plans);monitor.engine.configure(plans,False)
        monitor.checkpoints.inventory('p',str(file))
        monitor.checkpoints.complete_baseline({('p',str(old)):object()})
        previous=(dict(monitor.checkpoints.roots),set(monitor.checkpoints.ready),dict(monitor.checkpoints.files))
        connect=self.store.connect
        @contextmanager
        def failing_commit():
            with connect() as db:
                yield db
                raise sqlite3.OperationalError('commit failed')
        replacement=[('p',Plan('new',[str(new)],[],kind='watch'))]
        with patch.object(self.store,'connect',failing_commit),self.assertRaises(sqlite3.Error):
            monitor.checkpoints.configure(replacement)
        self.assertEqual((monitor.checkpoints.roots,monitor.checkpoints.ready,monitor.checkpoints.files),previous)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT path FROM monitor_files').fetchone()[0],str(file))
        monitor.checkpoints.configure(replacement)
        self.assertEqual(monitor.checkpoints.ready,set());self.assertFalse(monitor.checkpoints.files)

    def test_critical_notice_does_not_wait_for_database_lock_and_survives_restart(self):
        monitor=self.monitor()
        with self.store.connect() as lock:
            lock.execute('BEGIN IMMEDIATE')
            started=time.perf_counter()
            monitor.finished('p',ScanStatus('検出あり',ResultKind.DETECTED,True),'synthetic')
            self.assertLess(time.perf_counter()-started,1)
            self.assertEqual(monitor.notification_retry.count,1)
            self.assertTrue(monitor.notification_retry.path.exists())
            recovered=self.buffer();recovered.load();self.assertEqual(recovered.count,1)
        self.now=monitor.notification_retry.due
        monitor.flush()
        self.assertEqual(monitor.notification_retry.count,0)
        self.assertEqual(monitor.outbox.state()['pending'],1)
        self.assertFalse(monitor.notification_retry.path.exists())

    def test_gio_watch_continues_until_replacement_configuration_commits(self):
        from gi.repository import GLib
        from clam_gui.gio_watch import Observers
        from clam_gui.core import Plan
        from clam_gui.background import put_preference
        class Volumes:
            def connect(self,*args):return len(args)
            def disconnect(self,*args):pass
            def get_mounts(self):return []
        old=self.root/'old';old.mkdir();new=self.root/'new';new.mkdir()
        plans=[('p',Plan('old',[str(old)],[],kind='watch'))]
        monitor=Monitor(self.store,observers=lambda engine:Observers(engine,Volumes()),notifier=Mock())
        self.addCleanup(monitor.shutdown)
        put_preference(self.store,'background-enabled',True)
        def settle():
            deadline=time.perf_counter()+3
            while monitor.observers.idle is not None and time.perf_counter()<deadline:
                GLib.MainContext.default().iteration(False)
            self.assertIsNone(monitor.observers.idle)
        with patch('clam_gui.monitor.time.monotonic',return_value=100) as clock, \
             patch('clam_gui.monitor.watched_plans',return_value=plans) as selected, \
             patch.object(monitor.engine,'tick'):
            self.assertTrue(monitor.checked_tick());settle()
            self.assertTrue(monitor.checked_tick())
            watcher=monitor.observers.monitors['p',str(old)]
            selected.return_value=[('p',Plan('new',[str(new)],[],kind='watch'))]
            with self.store.connect() as lock:
                lock.execute('BEGIN IMMEDIATE');clock.return_value=103
                self.assertTrue(monitor.checked_tick())
                self.assertIs(monitor.observers.monitors['p',str(old)],watcher)
                file=old/'arrived';file.touch()
                deadline=time.perf_counter()+3
                while ('p',str(file)) not in monitor.engine.queue.pending and time.perf_counter()<deadline:
                    GLib.MainContext.default().iteration(False)
                self.assertIn(('p',str(file)),monitor.engine.queue.pending)
            clock.return_value=108
            self.assertTrue(monitor.checked_tick());settle()
            self.assertNotIn(('p',str(old)),monitor.observers.monitors)
            self.assertIn(('p',str(new)),monitor.observers.monitors)
        self.assertFalse(monitor.stopping);self.assertFalse(monitor.config_error)
