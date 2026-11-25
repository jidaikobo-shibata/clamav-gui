import datetime
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from clam_gui.core import Store, Plan
from clam_gui.schedule import Schedule, save_schedule
from clam_gui.runtime import activities, track_scan, request_stop, process_identity


class RuntimeTests(unittest.TestCase):
    def test_state_persists_across_store_instances_and_is_removed_on_error(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            with self.assertRaisesRegex(RuntimeError, 'failure'):
                with track_scan(store, 'plan', '2026-11-17T10:00:00+09:00') as running:
                    other = Store(directory)
                    state = activities(other)
                    self.assertEqual(len(state), 1)
                    self.assertEqual(state[0].profile_id, 'plan')
                    self.assertTrue(state[0].known)
                    self.assertTrue(request_stop(other, state[0].token))
                    self.assertTrue(running.cancelled())
                    self.assertTrue(activities(other)[0].stopping)
                    raise RuntimeError('failure')
            self.assertEqual(activities(store), [])
            self.assertFalse(request_stop(store, state[0].token))

    def test_dead_or_reused_process_is_not_reported_or_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            with track_scan(store, 'plan', '2026-11-17T10:00:00+09:00'):
                for identity in ('', 'different-boot:42'):
                    with patch('clam_gui.runtime.process_identity', return_value=identity):
                        self.assertEqual(activities(store), [])
                        self.assertFalse(request_stop(store, 'unknown'))

    def test_unreadable_identity_is_unknown_and_cannot_be_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            with track_scan(store, 'plan', '2026-11-17T10:00:00+09:00'):
                with patch('clam_gui.runtime.process_identity', return_value=None):
                    state = activities(store)[0]
                    self.assertFalse(state.known)
                    self.assertEqual(state.describe(), '実行状態を確認できません')
                    self.assertFalse(request_stop(store, state.token))

    def test_elapsed_and_stopping(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            started = '2026-11-17T10:00:00+09:00'
            with track_scan(store, 'plan', started):
                state = activities(store)[0]
                start = datetime.datetime.fromisoformat(started)
                for seconds, expected in ((59, '59秒'), (60, '1分00秒'), (61, '1分01秒'), (750, '12分30秒')):
                    with self.subTest(seconds=seconds):
                        now = start + datetime.timedelta(seconds=seconds)
                        self.assertEqual(state.describe(now), '検査中・開始から' + expected)
                request_stop(store, state.token)
                self.assertEqual(activities(store)[0].describe(), '検査を中止しています…')

    def test_unassociated_scan_does_not_create_state(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            with track_scan(store, None, '') as running:
                self.assertEqual(activities(store), [])
                self.assertFalse(running.cancelled())

    def test_live_identity(self):
        self.assertTrue(process_identity(os.getpid()))

    def test_crashed_runner_records_are_not_live(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            code = """import os, sys
from clam_gui.core import Store
from clam_gui.runtime import track_scan
with track_scan(Store(sys.argv[1]), 'plan', '2026-11-17T10:00:00+09:00'):
    os._exit(0)
"""
            subprocess.run([sys.executable, '-c', code, directory], check=True, timeout=5)
            with store.connect() as db:
                token = db.execute('SELECT token FROM scan_runs').fetchone()[0]
            self.assertEqual(activities(store), [])
            self.assertFalse(request_stop(store, token))

    @unittest.skipUnless(sys.platform.startswith("linux"), "uses a Unix executable script")
    def test_scan_in_separate_process_can_be_seen_and_cancelled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root/'data')
            key = store.save_profile(Plan('scheduled fake scan', [directory], []))
            save_schedule(store, key, Schedule(enabled=True))
            scanner = root/'scanner'
            scanner.write_text('#!/usr/bin/python3\nimport time\ntime.sleep(20)\n')
            scanner.chmod(0o700)
            code = """import os, sys
from unittest.mock import patch
from types import SimpleNamespace
from clam_gui.options import DEFAULTS, LIMITS
from scripts.run_scheduled import main
root, scanner, key = sys.argv[1:]
os.environ['HOME'] = root
sys.argv = ['runner', '--data-dir', root+'/data', '--profile', key]
with patch('clam_gui.core.executable', return_value=scanner), patch('clam_gui.core.supported_options', return_value=set(DEFAULTS)|set(LIMITS)), patch('scripts.run_scheduled.subprocess.run', return_value=SimpleNamespace(returncode=0)), patch('scripts.run_scheduled.definition_status', return_value='test definitions'), patch('clam_gui.definitions.DefinitionUpdater.update', return_value=('test update', '')), patch('clam_gui.definitions.daily_definition_age', return_value=0):
    raise SystemExit(main())
"""
            child = subprocess.Popen([sys.executable, '-c', code, directory, str(scanner), key],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                # First-time Rosetta interpreter startup can exceed five seconds.
                deadline = time.monotonic() + 15
                state = []
                while time.monotonic() < deadline:
                    state = activities(store)
                    if state:
                        break
                    if child.poll() is not None:
                        output, error = child.communicate(timeout=2)
                        self.fail('Runner exited before publishing state: ' + output + error)
                    time.sleep(.02)
                self.assertEqual(len(state), 1)
                self.assertNotEqual(child.pid, os.getpid())
                self.assertTrue(request_stop(Store(root/'data'), state[0].token))
                output, error = child.communicate(timeout=6)
                self.assertEqual(child.returncode, 2, error)
                self.assertEqual(activities(store), [])
                self.assertIn('中止', store.recent()[0][2])
            finally:
                if child.poll() is None:
                    child.terminate()
                    child.communicate(timeout=5)


if __name__ == '__main__':
    unittest.main()
