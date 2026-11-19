import sys
from pathlib import Path
from clam_gui.platforms import get_backend
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from clam_gui.core import Store, scan, summarize


class CoreTestCase(unittest.TestCase):
    def setUp(self):
        from clam_gui.options import DEFAULTS
        support = patch('clam_gui.core.supported_options', return_value=set(DEFAULTS))
        support.start()
        self.addCleanup(support.stop)


class ScannerTests(CoreTestCase):
    def test_result_classification(self):
        cases = [
            (0, 'Scanned files: 168', '検査完了：168'),
            (1, 'a: Test FOUND\nInfected files: 1', '検出あり：'),
            (1, 'a: Test FOUND\nTotal errors: 1', '検出あり・検査不完了'),
            (2, 'a: Heuristics.Limits.Exceeded.MaxFileSize FOUND\n'
             'Infected files: 0\nTotal errors: 1', '検査不完了\nサイズ'),
            (1, 'a: Heuristics.Encrypted.Zip FOUND', '検査不完了\n暗号化'),
            (0, 'WARNING: skipped', '検査不完了'),
        ]
        for code, output, expected in cases:
            with self.subTest(output=output):
                self.assertTrue(summarize(code, output).startswith(expected))
        self.assertIn('中止', summarize(0, '', True))


    def test_arguments_history_and_cancellation(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            cancel = threading.Event()
            cancel.set()
            with patch('clam_gui.core.executable', return_value='/usr/bin/clamscan'), \
                 patch.object(type(get_backend()), 'spawn_scanner') as popen:
                process = popen.return_value
                process.wait.side_effect = [subprocess.TimeoutExpired('scan', .2), -15]
                status, _ = scan(root, store, cancel, Path(root)/'lock')
                process.terminate.assert_called_once()
                args = popen.call_args.args[0]
                self.assertEqual(args[-2:], ['--', root])
                self.assertNotIn('--remove', args)
                self.assertIn('中止', status)
                self.assertEqual(store.recent()[0][2], status)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux flock integration')
    def test_lock_prevents_scanner_start(self):
        import fcntl
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'lock'
            with path.open('a') as lock, patch.object(type(get_backend()), 'spawn_scanner') as run, \
                 patch('clam_gui.core.executable', return_value='/usr/bin/clamscan'):
                fcntl.flock(lock, fcntl.LOCK_EX)
                with self.assertRaisesRegex(ValueError, '別の検査'):
                    scan(root, Store(root), lock_path=path)
                run.assert_not_called()

    def test_missing_target_does_not_run(self):
        with tempfile.TemporaryDirectory() as root, patch.object(type(get_backend()), 'spawn_scanner') as run:
            with self.assertRaises(ValueError):
                scan(root + '/missing', Store(root))
            run.assert_not_called()


class ProfileTests(CoreTestCase):
    def test_multiple_profiles_persist_and_edit_independently(self):
        from clam_gui.core import Plan
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            first = store.save_profile(Plan('ホーム', [root], [], True, [root+'/cache']))
            second = store.save_profile(Plan('資料', [root], [root+'/secret.txt']))
            store.save_profile(Plan('ホーム改訂', [root], [], True, [root+'/cache']), first)
            rows = Store(root).profiles()
            self.assertEqual([key for key, _ in rows], [first, second])
            self.assertEqual(rows[0][1].label, 'ホーム改訂')
            self.assertEqual(rows[0][1].excluded_dirs, [str(Path(root)/'cache')])
            self.assertEqual(rows[1][1].excludes, [str(Path(root)/'secret.txt')])

    def test_reject_invalid_exclusions(self):
        from clam_gui.core import Plan
        with tempfile.TemporaryDirectory() as root:
            for excluded in [root, '/elsewhere', root+'/../outside', 'relative']:
                with self.subTest(excluded=excluded), self.assertRaises(ValueError):
                    Store(root).save_profile(Plan('試験', [root], [], True, [excluded]))

    def test_directory_exclusion_arguments_have_boundaries(self):
        from clam_gui.core import Plan
        with tempfile.TemporaryDirectory() as root:
            with patch('clam_gui.core.executable', return_value='/usr/bin/clamscan'), \
                 patch.object(type(get_backend()), 'spawn_scanner') as popen:
                popen.return_value.wait.return_value = 0
                scan(Plan('試験', [root], [root+'/a.txt'], True, [root+'/cache']),
                     Store(root), lock_path=Path(root)/'lock')
                args = popen.call_args.args[0]
                import re
                pattern = next(arg.split('=', 1)[1] for arg in args if arg.startswith('--exclude-dir='))
                self.assertIsNotNone(re.search(pattern, root+'/cache/child'))
                self.assertIsNone(re.search(pattern, root+'/cache-other'))




class ProfileHistoryTests(CoreTestCase):
    def test_logs_follow_profile_identity_and_keep_legacy_records(self):
        from clam_gui.core import Plan
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            first = Plan('first', [root], [])
            second = Plan('second', [root], [])
            a = store.save_profile(first)
            b = store.save_profile(second)
            store.save('1', first.describe(), 'first log', '', a)
            store.save('2', second.describe(), 'second log', '', b)
            store.save('3', first.describe(), 'legacy matching log', '')
            store.save('4', '/old-unidentified', 'legacy unmatched log', '')
            self.assertEqual(len(store.profile_history(a, first)), 2)
            first.label = 'renamed'
            store.save_profile(first, a)
            rows = store.profile_history(a, first)
            self.assertEqual([row[2] for row in rows], ['first log'])
            self.assertEqual(len(store.recent()), 4)

    def test_existing_database_is_migrated_without_losing_history(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as root:
            with sqlite3.connect(Path(root)/'history.sqlite3') as db:
                db.execute('CREATE TABLE scans (started TEXT, target TEXT, status TEXT, output TEXT)')
                db.execute("INSERT INTO scans VALUES ('date', 'target', 'status', 'log')")
            db.close()
            store = Store(root)
            self.assertEqual(store.recent(), [('date', 'target', 'status', 'log')])


class HistoryEntryTests(CoreTestCase):
    def test_completion_and_detection_are_stored(self):
        from clam_gui.core import Plan
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            plan = Plan('test', [root], [])
            key = store.save_profile(plan)
            store.save('2026-01-01T10:00:00+09:00', plan.describe(), '検出あり', 'file: Test FOUND', key)
            entry = store.history_entries(key, plan)[0]
            self.assertIsNotNone(entry.completed_at)
            self.assertNotIn('旧記録', entry.date_label())
            self.assertTrue(entry.is_detected())
            store.save('start', plan.describe(), '検査不完了',
                       'file: Heuristics.Limits.Exceeded.MaxFileSize FOUND', key)
            self.assertFalse(store.history_entries(key, plan)[0].is_detected())
            from clam_gui.core import has_detection
            self.assertFalse(has_detection('検出あり（制限の警告を含む）',
                'file: Heuristics.Limits.Exceeded.MaxFileSize FOUND\nInfected files: 0'))

    def test_legacy_dates_are_not_misrepresented(self):
        from clam_gui.core import HistoryEntry
        entry = HistoryEntry(1, '2026-11-16T10:00:00+09:00', None, None, '', '', '')
        self.assertIn('開始・旧記録', entry.date_label())
        entry.output = 'End Date:   2026:11:16 10:05:12'
        self.assertEqual(entry.date_label(), '2026/11/16 10:05:12')


class SingleFolderTests(CoreTestCase):
    def test_file_or_multiple_targets_cannot_be_saved_or_scanned(self):
        from clam_gui.core import Plan
        with tempfile.TemporaryDirectory() as root:
            file = Path(root)/'file.txt'
            file.write_text('test')
            store = Store(root)
            for targets in [[str(file)], [root, root], []]:
                plan = Plan('invalid', targets, [])
                with self.subTest(targets=targets):
                    with self.assertRaises(ValueError):
                        store.save_profile(plan)
                    with patch.object(type(get_backend()), 'spawn_scanner') as process:
                        with self.assertRaises(ValueError):
                            scan(plan, store, lock_path=Path(root)/'lock')
                        process.assert_not_called()

    def test_stored_old_multiple_targets_are_preserved_but_not_duplicated_or_scheduled(self):
        import json
        from clam_gui.schedule import Schedule, Scheduler
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            with store.connect() as db:
                db.execute('INSERT INTO profiles VALUES (?, ?, ?)',
                           ('old', 'old profile', json.dumps({'targets': [root, root], 'excludes': []})))
            with self.assertRaises(ValueError):
                store.duplicate_profile('old')
            with self.assertRaises(ValueError):
                Scheduler(store, Path(root)/'units', lambda *_: None).apply('old', Schedule(True))
            self.assertIn('old', dict(store.profiles()))
            self.assertFalse((Path(root)/'units').exists())


class PhishingPreferenceTests(CoreTestCase):
    def test_default_and_persistence_do_not_overwrite_profiles(self):
        from clam_gui.core import Plan
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            plan = Plan('test', [root], [], False)
            key = store.save_profile(plan)
            self.assertFalse(store.phishing_enabled())
            store.set_phishing(True)
            reopened = Store(root)
            self.assertTrue(reopened.phishing_enabled())
            self.assertTrue(reopened.effective_plan(plan).phishing)
            self.assertFalse(dict(reopened.profiles())[key].phishing)
            reopened.set_phishing(False)
            self.assertFalse(reopened.effective_plan(plan).phishing)

    def test_scanner_passes_both_flags_for_global_preference(self):
        from clam_gui.core import Plan
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            for enabled in (False, True):
                store.set_phishing(enabled)
                with patch('clam_gui.core.executable', return_value='/usr/bin/clamscan'), \
                     patch.object(type(get_backend()), 'spawn_scanner') as popen:
                    popen.return_value.wait.return_value = 0
                    scan(Plan('test', [root], [], not enabled), store, lock_path=Path(root)/'lock')
                    args = popen.call_args.args[0]
                    value = 'yes' if enabled else 'no'
                    self.assertIn('--phishing-sigs='+value, args)
                    self.assertIn('--phishing-scan-urls='+value, args)


class ScannerLimitTests(CoreTestCase):
    def test_custom_limits_reach_scanner_and_history(self):
        from clam_gui.core import Plan
        from clam_gui.options import DEFAULTS, LIMITS
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            store.set_scan_preferences(store.scan_options(), {'max-filesize': 256, 'max-scansize': 512})
            with patch('clam_gui.core.executable', return_value='/usr/bin/clamscan'), \
                 patch('clam_gui.core.supported_options', return_value=set(DEFAULTS) | set(LIMITS)), \
                 patch.object(type(get_backend()), 'spawn_scanner') as process:
                process.return_value.wait.return_value = 0
                _, output = scan(Plan('test', [root], []), store, lock_path=Path(root)/'lock')
                args = process.call_args.args[0]
                self.assertIn('--max-filesize=256M', args)
                self.assertIn('--max-scansize=512M', args)
                self.assertIn('--alert-exceeds-max=yes', args)
            self.assertIn('1ファイルの上限: 256 MiB', output)
            self.assertIn('展開後の合計上限: 512 MiB', store.recent()[0][3])
