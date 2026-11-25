from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from clam_gui.macos_packages import AUTH_SCRIPT, MacPackageManager, RemovalPlan
from clam_gui.macos_notifications import request
from clam_gui.core import Store, Plan
from clam_gui.schedule import Schedule, save_schedule, read_schedule


class MacPackageTests(unittest.TestCase):
    def workflow(self, outcome):
        from types import SimpleNamespace
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = Store(temp.name)
        key = store.save_profile(Plan('temporary', [temp.name], []))
        save_schedule(store, key, Schedule(True))
        class Scheduler:
            def apply(self, key, schedule): save_schedule(store, key, schedule)
        enabled = [True]
        class Auto:
            def status(self): return SimpleNamespace(enabled=enabled[0])
            def set_enabled(self, value): enabled[0] = value
        manager = MacPackageManager()
        plan = RemovalPlan('homebrew', str(store.path), '1.5.4')
        with patch.object(manager, 'execute', side_effect=outcome), \
                patch.object(manager, 'version', return_value='1.5.4'):
            try:
                result = manager.remove_with_settings(plan, Auto(), store, Scheduler())
            finally:
                state = (enabled[0], read_schedule(store, key).enabled)
        return result, state

    def test_successful_removal_stops_future_jobs_but_retains_plan_data(self):
        result, state = self.workflow(lambda plan: ('removed', 0))
        self.assertEqual(result, ('removed', 0))
        self.assertEqual(state, (False, False))

    def test_cancelled_auth_restores_auto_update_and_schedules(self):
        result, state = self.workflow(lambda plan: ('cancelled', 1))
        self.assertEqual(result, ('cancelled', 1))
        self.assertEqual(state, (True, True))

    def test_partial_removal_leaves_automation_disabled_and_reports_it(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            key = store.save_profile(Plan('temporary', [root], []))
            save_schedule(store, key, Schedule(True))
            binary = Path(root) / 'scanner'
            binary.write_text('temporary fixture')
            enabled = [True]
            class Auto:
                def status(self): return SimpleNamespace(enabled=enabled[0])
                def set_enabled(self, value): enabled[0] = value
            class Scheduler:
                def apply(self, key, schedule): save_schedule(store, key, schedule)
            manager = MacPackageManager()
            def fail(plan):
                binary.unlink()
                raise RuntimeError('partial failure')
            with patch.object(manager, 'execute', side_effect=fail):
                with self.assertRaisesRegex(RuntimeError, '停止したまま'):
                    manager.remove_with_settings(RemovalPlan('homebrew', str(binary), '1.5.4'), Auto(), store, Scheduler())
            self.assertFalse(enabled[0])
            self.assertFalse(read_schedule(store, key).enabled)
    def test_unknown_installation_cannot_be_removed(self):
        manager = MacPackageManager(locate=lambda name: '/unknown/bin/clamscan')
        with patch.object(manager, 'version', return_value='ClamAV 1.5.4'):
            with self.assertRaisesRegex(ValueError, '導入元'):
                manager.preview()

    def test_missing_receipts_and_parent_traversal_cannot_be_used_for_removal(self):
        def missing(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, '', '')
        with self.assertRaisesRegex(ValueError, '所有元'):
            MacPackageManager(run=missing).receipt_files()
        import plistlib
        def traversal(command, **kwargs):
            output = plistlib.dumps({'pkgid': command[-1], 'volume': '/'}).decode() if '--pkg-info-plist' in command else 'usr/local/clamav/../../another/file'
            return subprocess.CompletedProcess(command, 0, output, '')
        with self.assertRaisesRegex(ValueError, '不正'):
            MacPackageManager(run=traversal).receipt_files()

    def test_symlink_prefix_and_user_owned_files_are_refused(self):
        with tempfile.TemporaryDirectory() as root:
            prefix = Path(root) / 'clamav'
            prefix.mkdir()
            file = prefix / 'scanner'
            file.write_text('owned by user')
            manager = MacPackageManager(prefix=prefix)
            with self.assertRaisesRegex(ValueError, '所有者'):
                manager.validate_leaf(file)
            link = Path(root) / 'link'
            link.symlink_to(prefix)
            with self.assertRaises(ValueError):
                MacPackageManager(prefix=link).validate_leaf(link / 'scanner')

    def test_active_or_unknown_process_state_blocks_removal(self):
        for code in (0, 2):
            run = lambda command, **kwargs: subprocess.CompletedProcess(command, code, '', '')
            with self.assertRaises(ValueError):
                MacPackageManager(run=run).check_idle()

    def test_other_products_daemon_is_not_stopped_or_blocked(self):
        def run(command, **kwargs):
            return subprocess.CompletedProcess(command, 0 if command[-1] == 'clamd' else 1,
                                               '42\n' if command[-1] == 'clamd' else '', '')
        manager = MacPackageManager(run=run)
        plan = RemovalPlan('official', '/usr/local/clamav/bin/clamscan', '1.5.4')
        with patch('clam_gui.macos_packages.process_executable', return_value='/usr/local/ClamXAV3/sbin/clamd'):
            manager.check_idle(plan)
        for executable in (None, '/usr/local/clamav/sbin/clamd'):
            with patch('clam_gui.macos_packages.process_executable', return_value=executable):
                with self.assertRaises(ValueError):
                    manager.check_idle(plan)

    def test_plan_is_rechecked_before_requesting_admin_authentication(self):
        manager = MacPackageManager()
        plan = RemovalPlan('official', '/usr/local/clamav/bin/clamscan', 'old')
        with patch.object(manager, 'check_idle'), patch.object(manager, 'preview', return_value=
                RemovalPlan('official', plan.binary, 'new')), patch.object(manager, 'command') as command:
            with self.assertRaisesRegex(ValueError, '変わりました'):
                manager.execute(plan)
            command.assert_not_called()

    def test_admin_script_is_fixed_and_leaf_paths_are_shell_quoted(self):
        path = '/usr/local/clamav/bin/a space;$(touch injected)'
        plan = RemovalPlan('official', '/nonexistent/validation-scanner', '1.5.4',
                           (path,), ('com.cisco.ClamAV.programs',))
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 1, '', 'User canceled (-128)')
        manager = MacPackageManager(run=run)
        with patch.object(manager, 'check_idle'), patch.object(manager, 'preview', return_value=plan):
            message, code = manager.execute(plan)
        self.assertIn('キャンセル', message)
        self.assertEqual(calls[0][:3], ['/usr/bin/osascript', '-e', AUTH_SCRIPT])
        rm = calls[0][-1].split(' && ')[0]
        self.assertEqual(shlex.split(rm), ['/bin/rm', '-f', '--', path])
        self.assertNotIn('-r', shlex.split(rm))
        self.assertNotIn('injected', calls[0][2])

    def test_nonstandard_prefix_cannot_be_removed_as_administrator(self):
        plan = RemovalPlan('official', '/nonexistent/scanner', '1', ('/tmp/scanner',), ())
        manager = MacPackageManager(prefix=Path('/tmp/clamav'))
        with patch.object(manager, 'check_idle'), patch.object(manager, 'preview', return_value=plan), \
                patch.object(manager, 'command') as command:
            with self.assertRaises(ValueError):
                manager.execute(plan)
            command.assert_not_called()

    def test_notification_text_is_json_input_and_never_program_code(self):
        import json
        with patch('clam_gui.macos_notifications.notifier_path') as path, \
                patch('clam_gui.macos_notifications.subprocess.run') as run:
            path.return_value.is_file.return_value = True
            path.return_value.__str__.return_value = '/app/ClamAV Notify'
            run.return_value = subprocess.CompletedProcess([], 0, '{"authorization": 2, "delivered": true}', '')
            text = '日本語 "quoted"; $(command)\nnext line'
            self.assertTrue(request('send', text, text)['delivered'])
            self.assertEqual(json.loads(run.call_args.kwargs['input'])['body'], text)
            self.assertEqual(run.call_args.args[0], ['/app/ClamAV Notify'])


if __name__ == '__main__':
    unittest.main()
