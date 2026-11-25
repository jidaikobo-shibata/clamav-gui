import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from clam_gui.definitions import DefinitionUpdater, UpdateTracker


class FakeSystem:
    def __init__(self, batches, active=True, auth_code=0):
        self.batches = list(batches)
        self.active = active
        self.auth_code = auth_code
        self.calls = []
        self.time = 0

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        if command[0] == '/usr/bin/journalctl':
            rows = self.batches.pop(0) if self.batches else []
            return subprocess.CompletedProcess(command, 0, '\n'.join(json.dumps(row) for row in rows), '')
        if command[0] == '/usr/bin/pkexec':
            if not self.auth_code:
                self.active = True
            return subprocess.CompletedProcess(command, self.auth_code, '', '')
        return subprocess.CompletedProcess(command, 0 if self.active else 3,
                                           'active' if self.active else 'inactive', '')

    def sleep(self, seconds):
        self.time += seconds


def rows(messages, prefix):
    return [{'MESSAGE': message, '__CURSOR': f'{prefix}-{i}', '__REALTIME_TIMESTAMP': '1'}
            for i, message in enumerate(messages)]


COMPLETE = ['ClamAV update process started at test', 'daily.cld database is up-to-date (version: 1)',
            'main.cvd updated (version: 1)', 'bytecode.cld database is up-to-date (version: 1)']


class DefinitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root/'freshclam.conf'
        self.config.write_text('Bytecode yes\n')
        self.binary = self.root/'freshclam'
        self.binary.touch()
        for name in ('daily', 'main', 'bytecode'):
            (self.root/(name+'.cld')).write_bytes(b'database fixture')

    def updater(self, system):
        return DefinitionUpdater(system, self.root, self.config, system.sleep, lambda: system.time, self.binary)

    def test_active_service_is_woken_and_only_new_logs_confirm_success(self):
        system = FakeSystem([rows(COMPLETE, 'old'), rows(COMPLETE, 'new')])
        status, log = self.updater(system).update(timeout=3)
        self.assertIn('確認しました', status)
        elevated = [cmd for cmd in system.calls if cmd[0] == '/usr/bin/pkexec']
        self.assertEqual(elevated, [['/usr/bin/pkexec', '/usr/bin/systemctl', 'kill',
            '--kill-whom=main', '--signal=SIGUSR1', 'clamav-freshclam.service']])
        self.assertIn('--after-cursor=old-3', system.calls[3])

    def test_scheduled_check_reuses_recent_completed_service_cycle(self):
        recent = rows(COMPLETE, 'recent')
        for row in recent:
            row['__REALTIME_TIMESTAMP'] = str(int(time.time() * 1_000_000))
        system = FakeSystem([recent])
        message, _ = self.updater(system).update(scheduled=True)
        self.assertIn('1時間以内', message)
        self.assertFalse(any('kill' in cmd for cmd in system.calls))

    def test_scheduled_check_after_long_shutdown_requests_new_cycle(self):
        system = FakeSystem([rows(COMPLETE, 'old'), rows(COMPLETE, 'new')])
        message, _ = self.updater(system).update(scheduled=True, timeout=3)
        self.assertIn('取得・更新を確認', message)
        self.assertTrue(any('kill' in cmd and '--no-ask-password' in cmd for cmd in system.calls))

    def test_running_download_is_observed_without_starting_another_updater(self):
        system = FakeSystem([rows(COMPLETE[:2], 'old'), rows(COMPLETE[2:], 'new')])
        status, _ = self.updater(system).update(timeout=3)
        self.assertIn('確認しました', status)
        self.assertFalse(any(cmd[0] == '/usr/bin/pkexec' for cmd in system.calls))

    def test_inactive_service_is_started(self):
        system = FakeSystem([rows(COMPLETE, 'old'), rows(COMPLETE, 'new')], active=False)
        status, _ = self.updater(system).update(timeout=3)
        self.assertIn('確認しました', status)
        self.assertIn(['/usr/bin/pkexec', '/usr/bin/systemctl', 'start', 'clamav-freshclam.service'], system.calls)

    def test_old_success_or_missing_files_cannot_report_completion(self):
        system = FakeSystem([rows(COMPLETE, 'old')])
        status, _ = self.updater(system).update(timeout=2)
        self.assertIn('待機時間', status)
        (self.root/'main.cld').unlink()
        system = FakeSystem([rows(COMPLETE, 'old'), rows(COMPLETE, 'new')])
        status, _ = self.updater(system).update(timeout=2)
        self.assertIn('確認できません', status)

    def test_failure_and_auth_cancel_are_reported(self):
        system = FakeSystem([rows(COMPLETE, 'old'), rows(['ERROR: HTTP 429'], 'new')])
        status, _ = self.updater(system).update(timeout=2)
        self.assertIn('失敗', status)
        system = FakeSystem([rows(COMPLETE, 'old')], auth_code=126)
        status, _ = self.updater(system).update(timeout=2)
        self.assertIn('キャンセル', status)

    def test_monitor_cancel_never_stops_service(self):
        system = FakeSystem([rows(COMPLETE, 'old')])
        cancel = threading.Event()
        cancel.set()
        status, _ = self.updater(system).update(cancel=cancel)
        self.assertIn('監視を終了', status)
        self.assertFalse(any('stop' in cmd or 'restart' in cmd or cmd[0] == '/usr/bin/pkexec'
                             for cmd in system.calls))

    def test_journal_permission_error_prevents_request(self):
        system = FakeSystem([])
        updater = self.updater(system)
        def denied(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, '', 'permission denied')
        updater.run = denied
        with self.assertRaisesRegex(ValueError, '読み取れません'):
            updater.update()

    def test_tracker_resets_between_cycles(self):
        tracker = UpdateTracker()
        for message in COMPLETE:
            tracker.feed(message)
        self.assertTrue(tracker.complete())
        tracker.feed('ClamAV update process started at next')
        self.assertFalse(tracker.complete())


class DailyDefinitionFreshnessTests(unittest.TestCase):
    def test_build_epoch_and_version_not_modification_time(self):
        from clam_gui.definitions import daily_definition_age, freshness_warning
        with tempfile.TemporaryDirectory() as root:
            now = 1800000000
            Path(root, 'daily.cvd').write_text(f'ClamAV-VDB:date:1:1:1:md5:sig:builder:{now-86400}')
            Path(root, 'daily.cld').write_text(f'ClamAV-VDB:date:2:1:1:md5:sig:builder:{now-5*86400}')
            self.assertEqual(daily_definition_age(root, now), 5*86400)
            self.assertIn('古い定義', freshness_warning(daily_definition_age(root, now)))
            self.assertFalse(freshness_warning(3*86400))
            self.assertTrue(freshness_warning(3*86400+1))
            Path(root, 'daily.cld').write_text('invalid')
            self.assertEqual(daily_definition_age(root, now), 86400)
            Path(root, 'daily.cvd').unlink()
            self.assertIsNone(daily_definition_age(root, now))
            self.assertIn('確認できません', freshness_warning(None))
