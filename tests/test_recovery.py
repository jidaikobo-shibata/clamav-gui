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
        retry.add(AutomaticNotice('detected','body','critical'))
        retry.drain(Mock())
        self.assertEqual(retry.count,1)
        self.assertEqual(retry.path.read_text(),'not valid JSON')
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
