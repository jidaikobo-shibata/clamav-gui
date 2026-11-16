from pathlib import Path
import subprocess
import tempfile
import unittest
from clam_gui.packages import AptManager, PACKAGES


class FakeRun:
    def __init__(self, simulation='', code=0):
        self.simulation = simulation
        self.code = code
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        if command[0] == '/usr/bin/pgrep':
            return subprocess.CompletedProcess(command, 1, '', '')
        if command[0] == '/usr/bin/dpkg-query':
            return subprocess.CompletedProcess(command, 0,
                '\n'.join(f'{p}\tinstall ok installed\t1.5.4' for p in PACKAGES), '')
        if '--simulate' in command:
            return subprocess.CompletedProcess(command, 0, self.simulation, '')
        if command[0] == '/usr/bin/pkexec':
            kwargs['stdout'].write('APT test output')
            return subprocess.CompletedProcess(command, self.code)
        raise AssertionError(command)


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.release = Path(self.temp.name)/'os-release'
        self.release.write_text('ID=ubuntu\n')

    def manager(self, run):
        return AptManager(run=run, release=self.release)

    def test_status_and_read_only_preview(self):
        run = FakeRun('Remv clamav [1.5.4]\nRemv clamav-daemon [1.5.4]\nRemv clamav-freshclam [1.5.4]')
        manager = self.manager(run)
        self.assertEqual(set(manager.status()), set(PACKAGES))
        plan = manager.preview('remove')
        self.assertEqual(len(plan.changes), 3)
        self.assertIn('Thunderbird', plan.describe())
        self.assertIn('再ダウンロード', plan.describe())
        self.assertFalse(any(call[0] == '/usr/bin/pkexec' for call in run.calls))

    def test_install_fixed_system_command_and_no_purge(self):
        run = FakeRun('Inst clamav (1.5.4 Ubuntu)\nInst libclamav12 (1.5.4 Ubuntu)\nConf clamav (1.5.4 Ubuntu)')
        manager = self.manager(run)
        plan = manager.preview('install')
        output, code = manager.execute(plan)
        self.assertEqual(code, 0)
        self.assertIn('APT test output', output)
        command = run.calls[-1]
        self.assertEqual(command[:5], ['/usr/bin/pkexec', '/usr/bin/env',
                         'DEBIAN_FRONTEND=noninteractive', 'UCF_FORCE_CONFFOLD=1', '/usr/bin/apt-get'])
        self.assertIn('--no-remove', command)
        self.assertIn('--no-auto-remove', command)
        self.assertIn('Dpkg::Options::=--force-confold', command)
        self.assertEqual(tuple(command[-3:]), PACKAGES)
        self.assertNotIn('purge', command)
        self.assertNotIn('autoremove', command)

    def test_removing_unrelated_packages_is_blocked(self):
        for changes in ('Remv thunderbird [1]', 'Inst other (1 Ubuntu)', 'Conf other (1 Ubuntu)'):
            run = FakeRun(changes)
            with self.assertRaises(ValueError):
                self.manager(run).preview('remove')
            self.assertFalse(any(call[0] == '/usr/bin/pkexec' for call in run.calls))
        with self.assertRaises(ValueError):
            self.manager(FakeRun('Remv other [1]')).preview('install')

    def test_changed_plan_is_rejected_before_auth(self):
        run = FakeRun('Remv clamav [1]')
        manager = self.manager(run)
        plan = manager.preview('remove')
        run.simulation = 'Remv clamav [2]'
        with self.assertRaisesRegex(ValueError, '変更予定が変わり'):
            manager.execute(plan)
        self.assertFalse(any(call[0] == '/usr/bin/pkexec' for call in run.calls))

    def test_noop_does_not_elevate(self):
        run = FakeRun('0 upgraded, 0 installed')
        manager = self.manager(run)
        self.assertEqual(manager.execute(manager.preview('install'))[1], 0)
        self.assertFalse(any(call[0] == '/usr/bin/pkexec' for call in run.calls))

    def test_auth_cancellation_is_reported(self):
        run = FakeRun('Remv clamav [1]', 126)
        manager = self.manager(run)
        message, code = manager.execute(manager.preview('remove'))
        self.assertEqual(code, 126)
        self.assertIn('キャンセル', message)

    def test_platform_and_active_scan_are_checked(self):
        self.release.write_text('ID=fedora\n')
        with self.assertRaises(ValueError):
            self.manager(FakeRun()).preview('install')
        self.release.write_text('ID=ubuntu\n')
        def active(command, **kwargs):
            self.assertEqual(command[0], '/usr/bin/pgrep')
            return subprocess.CompletedProcess(command, 0, '123', '')
        with self.assertRaisesRegex(ValueError, '実行中'):
            self.manager(active).preview('remove')
