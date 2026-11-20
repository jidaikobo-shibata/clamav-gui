"""Monitoring error histories must neither block GLib nor disappear on a lock."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock,patch
from clam_gui.core import Store,Plan
from clam_gui.automation import Automation,Media
from clam_gui.history_retry import HistoryRetry
from clam_gui.results import ScanStatus,ResultKind


class HistoryRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=Store(self.root/'data');self.now=0
        self.changed=Mock();self.history=HistoryRetry(self.store,clock=lambda:self.now,changed=self.changed)
        self.values=('2026-11-20T10:00:00+09:00','synthetic target',ScanStatus('検査不完了',ResultKind.INCOMPLETE),'synthetic reason','p')

    def test_two_unavailable_media_do_not_wait_for_write_lock_and_save_after_release(self):
        finished=Mock();engine=Automation(self.store,lambda f:f(),finished,clock=lambda:self.now)
        self.addCleanup(engine.close);engine.configure([],True)
        for index in range(2):
            folder=self.root/str(index);folder.mkdir();info=folder.stat()
            engine.add_media(Media(str(index),str(folder),'test',(info.st_dev,info.st_ino)))
            folder.rmdir()
        with self.store.connect() as lock:
            lock.execute('BEGIN IMMEDIATE')
            with patch('shutil.which',return_value='/synthetic/clamscan'):
                started=time.perf_counter();engine.tick()
                self.assertLess(time.perf_counter()-started,1)
            self.assertEqual(finished.call_count,2)
            self.assertEqual(len(engine.history.rows),2)
            self.assertIn('履歴の保存待ち',engine.status('external-media'))
            completed=[row.completed.isoformat(timespec='seconds') for row in engine.history.rows]
        self.now=5
        with patch('shutil.which',return_value=None):engine.tick()
        self.assertFalse(engine.history.rows);self.assertEqual(finished.call_count,2)
        with self.store.connect() as db:
            rows=db.execute('SELECT status,completed_at FROM scans ORDER BY rowid').fetchall()
        self.assertEqual([row[1] for row in rows],completed)
        self.assertTrue(all('検査を開始できませんでした' in row[0] for row in rows))
        self.assertTrue(all('保存待ち' not in row[0] for row in rows))

    def test_exclusive_lock_does_not_block_retention_preference_read(self):
        with self.store.connect() as lock:
            lock.execute('BEGIN EXCLUSIVE')
            started=time.perf_counter()
            with self.assertRaisesRegex(OSError,'再試行'):self.history.save(*self.values)
            self.assertLess(time.perf_counter()-started,1)
        self.now=5;self.history.drain()
        self.assertFalse(self.history.rows);self.assertEqual(len(self.store.recent()),1)

    def test_backoff_and_per_tick_retry_budget_preserve_all_rows(self):
        with patch.object(self.store,'save',side_effect=sqlite3.OperationalError('locked')) as save:
            for _ in range(10):
                with self.assertRaises(OSError):self.history.save(*self.values)
            self.assertEqual(save.call_count,1);self.assertEqual(self.history.due,5)
            self.now=4;self.history.drain();self.assertEqual(save.call_count,1)
            self.now=5;self.history.drain();self.assertEqual(save.call_count,2)
            self.assertEqual(self.history.due,15)
        self.now=15;self.history.drain()
        self.assertGreaterEqual(len(self.history.rows),6)
        while self.history.rows:self.history.drain()
        self.assertEqual(len(self.store.recent()),10)
        self.assertEqual(self.history.bytes,0);self.assertEqual(self.history.describe(),'')
        self.assertEqual(self.history.attempts,0)

    def test_failed_commit_keeps_row_and_retry_inserts_exactly_once(self):
        connect=self.store.connect
        @contextmanager
        def failed_commit():
            with connect() as db:
                yield db
                raise sqlite3.OperationalError('commit failed')
        with patch.object(self.store,'connect',failed_commit),self.assertRaises(OSError):
            self.history.save(*self.values)
        self.assertEqual(len(self.history.rows),1);self.assertFalse(self.store.recent())
        self.now=5;self.history.drain();self.history.drain()
        self.assertEqual(len(self.store.recent()),1);self.assertFalse(self.history.rows)

    def test_count_and_byte_limits_report_unretained_histories(self):
        self.history.LIMIT=2
        with patch.object(self.store,'save',side_effect=OSError('offline')):
            for _ in range(2):
                with self.assertRaises(OSError):self.history.save(*self.values)
            with self.assertRaisesRegex(OSError,'上限'):self.history.save(*self.values)
        self.assertEqual(len(self.history.rows),2)
        self.assertIn('保持できません',self.history.describe())
        small=HistoryRetry(self.store);small.MAX_BYTES=1
        with self.assertRaisesRegex(OSError,'上限'):small.save(*self.values)
        self.assertFalse(small.rows);self.assertEqual(small.bytes,0)

    def test_warning_and_worker_start_failure_use_same_nonblocking_history_path(self):
        folder=self.root/'watch';folder.mkdir();file=folder/'file';file.touch()
        finished=Mock();scanner=Mock()
        engine=Automation(self.store,lambda f:f(),finished,clock=lambda:self.now,scanner=scanner)
        self.addCleanup(engine.close)
        engine.configure([('p',Plan('test',[str(folder)],[],kind='watch'))],False)
        engine.queue.quiet=0;engine.file_event('p',str(file))
        with self.store.connect() as lock:
            lock.execute('BEGIN IMMEDIATE')
            started=time.perf_counter()
            engine.warn('p',engine.plans['p'],'coverage warning')
            with patch('shutil.which',return_value='/synthetic/clamscan'), \
                 patch('clam_gui.automation.threading.Thread.start',side_effect=RuntimeError('cannot start')):
                engine.tick()
            self.assertLess(time.perf_counter()-started,1)
            self.assertEqual(len(engine.history.rows),2)
            self.assertIn(('p',str(file)),engine.queue.pending)
            self.assertEqual(engine.errors['p'],'coverage warning')
        self.now=5;engine.tick()
        self.assertFalse(engine.history.rows);self.assertEqual(len(self.store.recent()),2)
        self.assertEqual(finished.call_count,2);scanner.assert_not_called()

    def test_refresh_failure_does_not_misreport_or_repeat_committed_history(self):
        self.history.changed=Mock(side_effect=RuntimeError('view closed'))
        self.history.save(*self.values)
        self.assertFalse(self.history.rows);self.assertEqual(len(self.store.recent()),1)
        self.history.drain();self.assertEqual(len(self.store.recent()),1)

    def test_shutdown_flushes_all_recovered_rows_without_refreshing_closing_view(self):
        engine=Automation(self.store,lambda f:f(),Mock(),changed=self.changed)
        with patch.object(self.store,'save',side_effect=OSError('offline')):
            for _ in range(10):
                with self.assertRaises(OSError):engine.history.save(*self.values)
        self.changed.reset_mock();engine.close()
        self.assertFalse(engine.history.rows)
        self.assertEqual(len(self.store.recent()),10)
        self.changed.assert_not_called()

    def test_shutdown_does_not_wait_or_spin_when_database_stays_locked(self):
        engine=Automation(self.store,lambda f:f(),Mock())
        with self.store.connect() as lock:
            lock.execute('BEGIN IMMEDIATE')
            with self.assertRaises(OSError):engine.history.save(*self.values)
            started=time.perf_counter();engine.close()
            self.assertLess(time.perf_counter()-started,1)
        self.assertEqual(len(engine.history.rows),1)
