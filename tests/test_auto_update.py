import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from clam_gui.definitions import AutoUpdateService, DefinitionUpdater, SERVICE

COMPLETE = ['ClamAV update process started at test', 'daily.cld database is up-to-date (version: 1)',
            'main.cvd updated (version: 1)', 'bytecode.cld database is up-to-date (version: 1)']


class ServiceSystem:
    def __init__(self):
        self.enabled = False
        self.active = False
        self.masked = False
        self.auth = 0
        self.fail_start = False
        self.fail_stop = False
        self.calls = []
        self.batches = [COMPLETE, COMPLETE]
        self.after_batch = None
        self.time = 0

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        if command[0] == '/usr/bin/journalctl':
            messages = self.batches.pop(0) if self.batches else []
            data = [{'MESSAGE': msg, '__CURSOR': f'{len(self.calls)}-{i}', '__REALTIME_TIMESTAMP': '1'}
                    for i, msg in enumerate(messages)]
            if self.after_batch and len(self.calls) > 1:
                self.after_batch()
            return subprocess.CompletedProcess(command, 0, '\n'.join(json.dumps(row) for row in data), '')
        if command[1] == 'show':
            unit = 'masked' if self.masked else 'enabled' if self.enabled else 'disabled'
            return subprocess.CompletedProcess(command, 0, f'LoadState=loaded\nUnitFileState={unit}\nActiveState={"active" if self.active else "inactive"}', '')
        if command[0] == '/usr/bin/pkexec':
            action = command[2]
            if self.auth or (self.fail_stop and action == 'stop'):
                return subprocess.CompletedProcess(command, self.auth or 1, '', 'authorization denied')
            if action in ('enable', 'disable'):
                self.enabled = action == 'enable'
                self.active = self.enabled and not self.fail_start
            elif action == 'start':
                self.active = True
            elif action == 'stop':
                self.active = False
            return subprocess.CompletedProcess(command, 0, '', '')
        return subprocess.CompletedProcess(command, 0 if self.active else 3, 'active' if self.active else 'inactive', '')

    def sleep(self, seconds):
        self.time += seconds


class AutoUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root/'freshclam.conf'
        self.config.write_text('Checks 24\nBytecode yes\n')
        self.system = ServiceSystem()
        self.manager = AutoUpdateService(self.system, self.config)

    def test_setting_and_running_states_are_distinct(self):
        self.system.active = True
        state = self.manager.status()
        self.assertIn('無効（一時稼働中）', state.label())
        self.assertIn('約1時間ごと', state.frequency())
        self.assertTrue(state.manageable)
        self.assertFalse(any(cmd[0] == '/usr/bin/pkexec' for cmd in self.system.calls))

    def test_fixed_enable_and_disable_commands_and_verification(self):
        _, state = self.manager.set_enabled(True)
        self.assertTrue(state.enabled and state.running)
        self.assertIn(['/usr/bin/pkexec', '/usr/bin/systemctl', 'enable', '--now', SERVICE], self.system.calls)
        _, state = self.manager.set_enabled(False)
        self.assertFalse(state.enabled or state.running)
        self.assertIn(['/usr/bin/pkexec', '/usr/bin/systemctl', 'disable', '--now', SERVICE], self.system.calls)

    def test_cancel_and_masked_service_do_not_change_setting(self):
        self.system.auth = 126
        text, state = self.manager.set_enabled(True)
        self.assertIn('キャンセル', text)
        self.assertFalse(state.enabled)
        self.system.masked = True
        previous = len(self.system.calls)
        with self.assertRaises(ValueError):
            self.manager.set_enabled(True)
        self.assertFalse(any(cmd[0] == '/usr/bin/pkexec' for cmd in self.system.calls[previous:]))

    def test_partial_failure_is_not_reported_as_success(self):
        self.system.fail_start = True
        with self.assertRaisesRegex(ValueError, '一致しません'):
            self.manager.set_enabled(True)
        state = self.manager.status()
        self.assertTrue(state.enabled)
        self.assertFalse(state.running)

    def updater(self):
        binary = self.root/'freshclam'
        binary.touch()
        for name in ('daily', 'main', 'bytecode'):
            (self.root/(name+'.cld')).write_bytes(b'test database')
        return DefinitionUpdater(self.system, self.root, self.config, self.system.sleep,
                                 lambda: self.system.time, binary)

    def test_manual_update_with_auto_off_restores_stopped_service(self):
        result, _ = self.updater().update(restore_stopped=True)
        self.assertIn('確認しました', result)
        self.assertFalse(self.system.enabled or self.system.active)
        self.assertIn(['/usr/bin/pkexec', '/usr/bin/systemctl', 'stop', SERVICE], self.system.calls)
        self.assertFalse(any('enable' in cmd or 'disable' in cmd for cmd in self.system.calls))

    def test_auto_off_stops_temporary_service_even_after_update_failure(self):
        self.system.batches = [COMPLETE, ['ClamAV update process started at test', 'ERROR: HTTP 429']]
        result, _ = self.updater().update(restore_stopped=True)
        self.assertIn('失敗', result)
        self.assertFalse(self.system.active)

    def test_auto_off_cancel_stops_temporary_service_and_reports_it(self):
        cancel = threading.Event()
        self.system.batches = [COMPLETE, []]
        updater = self.updater()
        updater.sleep = lambda _: cancel.set()
        result, _ = updater.update(cancel=cancel, restore_stopped=True)
        self.assertIn('監視を終了', result)
        self.assertIn('サービスは停止しました', result)
        self.assertFalse(self.system.active)

    def test_auto_off_timeout_does_not_claim_service_is_still_running(self):
        self.system.batches = [COMPLETE, []]
        result, _ = self.updater().update(timeout=1, restore_stopped=True)
        self.assertIn('待機時間', result)
        self.assertNotIn('停止していません', result)
        self.assertFalse(self.system.active)

    def test_external_enabling_during_update_is_respected(self):
        self.system.after_batch = lambda: setattr(self.system, 'enabled', True)
        self.updater().update(restore_stopped=True)
        self.assertTrue(self.system.active)
        self.assertNotIn(['/usr/bin/pkexec', '/usr/bin/systemctl', 'stop', SERVICE], self.system.calls)

    def test_temporary_service_stop_failure_is_explicit(self):
        self.system.fail_stop = True
        with self.assertRaisesRegex(ValueError, '停止できません'):
            self.updater().update(restore_stopped=True)
        self.assertTrue(self.system.active)
