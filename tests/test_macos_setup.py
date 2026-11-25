import hashlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from clam_gui.macos_setup import (CONFIG_MARKER, DATABASES, INSTALLER_NAME,
                                  MacSetup, SetupCancelled, SetupError)


class Response(io.BytesIO):
    def geturl(self):
        return 'https://release-assets.githubusercontent.com/package.pkg'


class MacSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / '日本語と space' / 'data'
        self.backend = MacSetup(self.root, locate=lambda name: '/fake/bin/' + name)

    def databases(self):
        self.backend.database.mkdir(parents=True, exist_ok=True)
        for name in DATABASES:
            (self.backend.database / (name + '.cvd')).write_bytes(b'database')

    def test_inspection_does_not_write_or_access_network_or_run_commands(self):
        with patch('clam_gui.macos_setup.subprocess.run') as run, \
                patch('clam_gui.macos_setup.urlopen') as network:
            state = self.backend.inspect()
        self.assertTrue(state.engine)
        self.assertFalse(state.ready)
        self.assertFalse(self.root.exists())
        run.assert_not_called()
        network.assert_not_called()

    def test_existing_scanner_alone_is_not_ready(self):
        self.backend.locate = lambda name: '/fake/clamscan' if name == 'clamscan' else None
        self.assertFalse(self.backend.inspect().engine)

    def test_configuration_is_private_and_does_not_overwrite_other_files(self):
        self.backend.configure()
        text = self.backend.config.read_text()
        self.assertIn('DatabaseDirectory "' + str(self.backend.database) + '"', text)
        self.assertEqual(self.backend.config.stat().st_mode & 0o777, 0o600)
        self.assertIn(CONFIG_MARKER, text)
        self.backend.config.write_text('existing custom configuration')
        with self.assertRaisesRegex(SetupError, '既存の設定'):
            self.backend.configure()
        self.assertEqual(self.backend.config.read_text(), 'existing custom configuration')

    def test_symlink_configuration_and_database_are_not_followed(self):
        self.root.mkdir(parents=True)
        target = Path(self.temp.name) / 'other'
        target.write_text('preserve this')
        self.backend.config.symlink_to(target)
        with self.assertRaises(SetupError):
            self.backend.configure()
        self.assertEqual(target.read_text(), 'preserve this')
        self.backend.config.unlink()
        self.backend.database.rmdir()
        self.backend.database.symlink_to(self.temp.name)
        with self.assertRaises(SetupError):
            self.backend.configure()

    def test_missing_empty_or_old_daily_data_need_preparation(self):
        self.backend.configure()
        self.databases()
        self.assertTrue(self.backend.inspect().ready)
        daily = self.backend.database / 'daily.cvd'
        stamp = time.time() - 4 * 86400
        os.utime(daily, (stamp, stamp))
        self.assertFalse(self.backend.inspect().fresh)
        daily.write_bytes(b'')
        self.assertFalse(self.backend.inspect().definitions)

    def test_old_main_database_does_not_make_updated_daily_database_stale(self):
        self.backend.configure()
        self.databases()
        stamp = time.time() - 365 * 86400
        os.utime(self.backend.database / 'main.cvd', (stamp, stamp))
        self.assertTrue(self.backend.inspect().ready)

    def download(self, content=b'official package', expected=None):
        self.root.mkdir(parents=True, exist_ok=True)
        self.backend.opener = lambda request, **kwargs: Response(content)
        expected = expected if expected is not None else content
        with patch('clam_gui.macos_setup.INSTALLER_SIZE', len(expected)), \
                patch('clam_gui.macos_setup.INSTALLER_SHA256', hashlib.sha256(expected).hexdigest()):
            return self.backend.download(threading.Event(), lambda *args: None)

    def test_download_is_verified_and_corrupted_cache_is_replaced(self):
        package = self.download()
        self.assertEqual(package.read_bytes(), b'official package')
        package.write_bytes(b'tampered')
        package = self.download()
        self.assertEqual(package.read_bytes(), b'official package')
        self.assertEqual(list(package.parent.glob('*.partial')), [])

    def test_bad_or_truncated_download_cannot_be_opened(self):
        for body in (b'tampered package', b'partial', b'longer-than-the-expected-content'):
            with self.assertRaises(SetupError):
                self.download(body, expected=b'official package')
            self.assertFalse((self.root / 'downloads' / INSTALLER_NAME).exists())
            self.assertEqual(list((self.root / 'downloads').glob('*.partial')), [])

    def test_network_user_agent_identifies_only_the_application(self):
        requests = []
        def opener(request, **kwargs):
            requests.append(request)
            return Response(b'data')
        self.backend.opener = opener
        self.root.mkdir(parents=True)
        with patch('clam_gui.macos_setup.INSTALLER_SIZE', 4), \
                patch('clam_gui.macos_setup.INSTALLER_SHA256', hashlib.sha256(b'data').hexdigest()):
            self.backend.download(threading.Event(), lambda *args: None)
        self.assertEqual(requests[0].get_header('User-agent'), 'ClamAV-GUI/0.1')

    def test_invalid_or_different_publisher_signature_never_opens_installer(self):
        for code, publisher in ((1, 'Developer ID Installer: Cisco (DE8Y96K9QP)'),
                                (0, 'Developer ID Installer: Someone Else'),
                                (0, 'Developer ID Installer: Cisco Fake (FAKE)')):
            calls = []
            def run(command, **kwargs):
                calls.append(command)
                return subprocess.CompletedProcess(command, code, publisher, '')
            self.backend.run = run
            with patch.object(self.backend, 'download', return_value=Path('/tmp/package.pkg')):
                with self.assertRaisesRegex(SetupError, '署名'):
                    self.backend.install_engine(threading.Event(), lambda *args: None)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], '/usr/sbin/pkgutil')

    def test_installer_cancellation_cannot_claim_success(self):
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, 'Developer ID Installer: Cisco (DE8Y96K9QP)', '')
        self.backend.run = run
        self.backend.locate = lambda name: None
        with patch.object(self.backend, 'download', return_value=Path('/tmp/a space.pkg')):
            with self.assertRaisesRegex(SetupError, '取り消した場合'):
                self.backend.install_engine(threading.Event(), lambda *args: None)
        self.assertEqual(calls[1], ['/usr/bin/open', '-W', '-n', '/tmp/a space.pkg'])
        self.assertTrue(all('sudo' not in command and '/bin/sh' not in command for command in calls))

    def test_parallel_setup_is_rejected(self):
        with self.backend.locked():
            with self.assertRaisesRegex(SetupError, '進行中'):
                with MacSetup(self.root).locked():
                    self.fail('second setup must not enter its critical section')

    def test_cancel_before_start_does_not_install_or_configure(self):
        cancel = threading.Event()
        cancel.set()
        with patch.object(self.backend, 'install_engine') as install:
            with self.assertRaises(SetupCancelled):
                self.backend.prepare(cancel)
        install.assert_not_called()
        self.assertFalse(self.backend.config.exists())

    def test_retry_reuses_successful_install_and_configuration(self):
        def update(cancel, report):
            self.databases()
        with patch.object(self.backend, 'install_engine') as install, \
                patch.object(self.backend, 'update_definitions', side_effect=SetupError('通信失敗')):
            with self.assertRaises(SetupError):
                self.backend.prepare()
            self.assertTrue(self.backend.inspect().configuration)
            install.assert_not_called()
        with patch.object(self.backend, 'install_engine') as install, \
                patch.object(self.backend, 'update_definitions', side_effect=update):
            self.assertTrue(self.backend.prepare().ready)
            install.assert_not_called()

    def test_existing_freshclam_prevents_parallel_download(self):
        self.backend.run = lambda command, **kwargs: subprocess.CompletedProcess(command, 0, '123', '')
        with patch('clam_gui.macos_setup.subprocess.Popen') as process:
            with self.assertRaisesRegex(SetupError, '進行中'):
                self.backend.update_definitions(threading.Event(), lambda *args: None)
        process.assert_not_called()

    def test_modified_managed_configuration_cannot_run_custom_hooks(self):
        self.backend.configure()
        with self.backend.config.open('a') as stream:
            stream.write('OnUpdateExecute custom-command\n')
        self.assertFalse(self.backend.inspect().configuration)
        with patch('clam_gui.macos_setup.subprocess.Popen') as process:
            with self.assertRaisesRegex(SetupError, '既存の設定'):
                self.backend.prepare()
        process.assert_not_called()

    def test_cancelled_updater_stops_only_its_own_child(self):
        self.backend.configure()
        cancel = threading.Event()
        self.backend.run = lambda command, **kwargs: subprocess.CompletedProcess(command, 1, '', '')
        with patch('clam_gui.macos_setup.subprocess.Popen') as popen:
            process = popen.return_value
            process.poll.return_value = None
            def wait(timeout):
                cancel.set()
                process.wait.side_effect = None
                raise subprocess.TimeoutExpired('freshclam', timeout)
            process.wait.side_effect = wait
            with self.assertRaises(SetupCancelled):
                self.backend.update_definitions(cancel, lambda *args: None)
            process.terminate.assert_called_once_with()

    def test_updater_exit_zero_without_required_databases_is_not_success(self):
        self.backend.configure()
        self.backend.run = lambda command, **kwargs: subprocess.CompletedProcess(command, 1, '', '')
        with patch('clam_gui.macos_setup.subprocess.Popen') as popen:
            popen.return_value.wait.return_value = 0
            popen.return_value.poll.return_value = 0
            with self.assertRaisesRegex(SetupError, '揃っていません'):
                self.backend.update_definitions(threading.Event(), lambda *args: None)

    def test_update_success_records_check_without_changing_database_dates(self):
        self.backend.configure()
        self.databases()
        stamp = time.time() - 4 * 86400
        for name in DATABASES:
            os.utime(self.backend.database / (name + '.cvd'), (stamp, stamp))
        self.backend.run = lambda command, **kwargs: subprocess.CompletedProcess(command, 1, '', '')
        with patch('clam_gui.macos_setup.subprocess.Popen') as popen:
            popen.return_value.wait.return_value = 0
            popen.return_value.poll.return_value = 0
            self.backend.update_definitions(threading.Event(), lambda *args: None)
            command = popen.call_args.args[0]
        self.assertIn('--config-file=' + str(self.backend.config), command)
        self.assertIn('--datadir=' + str(self.backend.database), command)
        self.assertTrue(self.backend.inspect().ready)
        self.assertEqual((self.backend.database / 'daily.cvd').stat().st_mtime, stamp)


if __name__ == '__main__':
    unittest.main()
