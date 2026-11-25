"""Behavioral contracts shared by GUI, headless jobs, and future OS adapters."""
import datetime
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from clam_gui.core import Store, Plan, summarize, ScanBusyError
from clam_gui.results import ScanStatus, ResultKind, assess
from clam_gui.notifications import AutomaticNotices, scan_notification
from clam_gui.platforms import get_backend, UnsupportedPlatformError
from clam_gui.jobs import execute_scan


class ResultContracts(unittest.TestCase):
    def test_status_text_can_change_without_changing_decisions(self):
        status = ScanStatus('Completed successfully', ResultKind.CLEAN)
        self.assertTrue(assess(status).clean)
        self.assertTrue(assess(status + '\nextra explanation').clean)
        now = datetime.datetime.now()
        title, body, urgency = scan_notification('test', status, '', now, now, '')
        self.assertEqual(urgency, 'normal')
        self.assertIn('Completed successfully', body)

    def test_incomplete_detection_and_cancel_are_distinct(self):
        incomplete = summarize(2, '/file: Test-Signature FOUND\nTotal errors: 1')
        self.assertEqual(assess(incomplete).kind, ResultKind.INCOMPLETE)
        self.assertTrue(assess(incomplete).detected)
        self.assertFalse(assess(incomplete).completed)
        cancelled = summarize(-15, '/file: Test-Signature FOUND', cancelled=True)
        self.assertEqual(assess(cancelled).kind, ResultKind.CANCELLED)
        self.assertTrue(assess(cancelled).detected)

    def test_legacy_plain_text_is_still_readable(self):
        for status, kind in [('検査完了', ResultKind.CLEAN), ('検出なし', ResultKind.CLEAN),
                             ('検出あり', ResultKind.DETECTED), ('検査不完了', ResultKind.INCOMPLETE),
                             ('確認が必要です', ResultKind.WARNING), ('検査を中止しました', ResultKind.CANCELLED)]:
            self.assertEqual(assess(status).kind, kind)

    def test_persisted_kind_survives_roundtrip_and_retention_uses_it(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            plan = Plan('test', [root], [])
            key = store.save_profile(plan)
            status = ScanStatus('検出ありという文言を含むが正常な表示テスト', ResultKind.CLEAN)
            store.save(datetime.datetime.now().astimezone().isoformat(), plan.describe(), status, '', key)
            entry = store.history_entries(key, plan)[0]
            self.assertEqual(entry.result_kind, 'clean')
            self.assertFalse(entry.is_detected())
            with store.connect() as db:
                ident = db.execute('SELECT max(rowid) FROM scans').fetchone()[0]
                db.execute('UPDATE scans SET completed_at=? WHERE rowid=?',
                           ((datetime.datetime.now().astimezone()-datetime.timedelta(days=100)).isoformat(), ident))
            policy = store.automation_settings();policy['retention_enabled'] = True
            self.assertEqual(store.set_automation_settings(policy), 1)


class NotificationContracts(unittest.TestCase):
    def test_normal_batch_and_immediate_alert_share_the_same_policy(self):
        clock = [0]
        notices = AutomaticNotices(clock=lambda:clock[0])
        clean = ScanStatus('OK', ResultKind.CLEAN)
        self.assertIsNone(notices.record('p', 'plan', clean))
        clock[0] = 10
        notices.record('p', 'plan', clean)
        alert = notices.record('p', 'plan', ScanStatus('problem', ResultKind.INCOMPLETE))
        self.assertEqual(alert.urgency, 'critical')
        self.assertFalse(notices.due())
        clock[0] = 20
        self.assertEqual(notices.due(), ['p'])
        message = notices.flush('p')
        self.assertIn('2回', message.body)
        self.assertIsNone(notices.flush('p'))

    def test_shutdown_flush_preserves_each_plan(self):
        notices = AutomaticNotices()
        for key in ('a','b'):
            notices.record(key, key, ScanStatus('OK', ResultKind.CLEAN))
        self.assertEqual(set(notices.due(force=True)), {'a','b'})


class BackendContracts(unittest.TestCase):
    def test_common_modules_import_without_fcntl(self):
        code = '''import builtins
original = builtins.__import__
def guarded(name, globals=None, *args, **kwargs):
    if name == 'fcntl' and (globals or {}).get('__name__', '').startswith('clam_gui'):
        raise AssertionError('eager Linux dependency')
    return original(name, globals, *args, **kwargs)
builtins.__import__ = guarded
import clam_gui.core, clam_gui.background, clam_gui.runtime
from clam_gui.platforms import get_backend
backend = get_backend('unsupported-test')
assert backend.process_identity(1) is None
'''
        subprocess.run([sys.executable, '-c', code], check=True, timeout=5)

    def test_unsupported_platform_does_not_fall_back_to_linux(self):
        backend = get_backend('unsupported-test')
        with self.assertRaises(UnsupportedPlatformError):backend.scheduler(None)
        self.assertIsNone(backend.process_identity(1))

    def test_linux_factories_are_lazy_and_replaceable(self):
        backend = get_backend('linux')
        with patch('clam_gui.schedule.Scheduler') as scheduler:
            marker = object()
            self.assertIs(backend.scheduler(marker), scheduler.return_value)
            scheduler.assert_called_once_with(marker)

    def test_preference_group_is_atomic_and_ignores_unrelated_malformed_state(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            store.preferences.set('one', 'original')
            with self.assertRaises(RuntimeError):
                with store.connect() as db:
                    store.preferences.set_many({'one':'changed','two':2}, db)
                    raise RuntimeError('rollback')
            self.assertEqual(store.preferences.get('one'), 'original')
            self.assertIsNone(store.preferences.get('two'))
            with store.connect() as db:
                db.execute('INSERT INTO preferences VALUES (?,?)', ('unrelated', 'not JSON'))
            self.assertTrue(store.scan_options())


class JobContracts(unittest.TestCase):
    def test_failure_reporting_failure_still_returns_a_terminal_result(self):
        scanner = Mock(side_effect=OSError('scanner failed'))
        failure = Mock(side_effect=OSError('database failed'))
        status, output = execute_scan(None, None, scanner=scanner, failure=failure)
        self.assertEqual(assess(status).kind, ResultKind.INCOMPLETE)
        self.assertIn('scanner failed', status)
        self.assertIn('database failed', output)

    def test_contention_can_be_requeued_by_automatic_jobs(self):
        with self.assertRaises(ScanBusyError):
            execute_scan(None, None, scanner=Mock(side_effect=ScanBusyError('busy')))

    def test_thread_start_failure_does_not_leave_automatic_job_running(self):
        from clam_gui.automation import Automation
        with tempfile.TemporaryDirectory() as root:
            folder=Path(root)/'watched';folder.mkdir();file=folder/'file';file.touch()
            store=Store(Path(root)/'data')
            plan=Plan('watch',[str(folder)],[],kind='watch')
            finished=Mock()
            engine=Automation(store,lambda cb:cb(),finished)
            engine.configure([('p',plan)],False)
            engine.queue.quiet=0;engine.file_event('p',file)
            with patch('shutil.which',return_value='/mock/clamscan'), patch('clam_gui.automation.threading.Thread') as thread:
                thread.return_value.start.side_effect=RuntimeError('cannot start')
                engine.tick()
            self.assertFalse(engine.active)
            self.assertIsNone(engine.active_key)
            finished.assert_called_once()
            self.assertIn('cannot start', store.recent()[0][2])
