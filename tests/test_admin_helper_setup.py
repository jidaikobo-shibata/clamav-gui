import io
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch, Mock
from clam_gui.admin_helper_setup import AdminHelperSetup, SetupCancelled, PACKAGE, MAINTAINER


class HelperSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    @unittest.skipUnless(Path('/usr/bin/dpkg-deb').is_file(), 'Requires the Debian package builder')
    def test_data_only_package_has_exact_files_root_ownership_and_no_scripts(self):
        package = AdminHelperSetup().build(self.root)
        data = subprocess.run(['/usr/bin/dpkg-deb', '--fsys-tarfile', str(package)], capture_output=True, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            files = [m for m in archive.getmembers() if m.isfile()]
            self.assertEqual({m.name for m in files}, {'./usr/libexec/clamav-gui-admin-scan', './usr/share/polkit-1/actions/com.jidaikobo.ClamAVGui.admin-scan.policy'})
            self.assertTrue(all(m.uid == m.gid == 0 for m in archive.getmembers()))
            self.assertTrue(all(not m.issym() and not m.islnk() for m in archive.getmembers()))
            self.assertEqual(next(m for m in files if m.name.endswith('admin-scan')).mode, 0o755)
        control = subprocess.run(['/usr/bin/dpkg-deb', '--ctrl-tarfile', str(package)], capture_output=True, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(control)) as archive:
            self.assertEqual([m.name for m in archive.getmembers() if m.isfile()], ['./control'])

    def test_foreign_files_are_not_overwritten_or_removed(self):
        path = self.root/'foreign'
        path.write_text('keep')
        run = Mock(return_value=subprocess.CompletedProcess([], 1, '', ''))
        manager = AdminHelperSetup(run)
        with patch.object(manager, 'check_platform'), patch('clam_gui.admin_helper_setup.HELPER', path):
            for action in ('install', 'remove'):
                with self.assertRaisesRegex(ValueError, '上書き・削除'):
                    manager.execute(action)
        self.assertEqual(path.read_text(), 'keep')
        self.assertTrue(all('/usr/bin/pkexec' not in call.args[0] for call in run.call_args_list))

    def test_install_uses_only_fixed_system_dpkg_and_one_authentication(self):
        run = Mock(return_value=subprocess.CompletedProcess([], 0, '', ''))
        manager = AdminHelperSetup(run)
        with patch.object(manager, 'status', side_effect=['missing', 'ready']), patch.object(manager, 'build', return_value=self.root/'package.deb'):
            manager.execute('install')
        self.assertEqual(run.call_args.args[0], ['/usr/bin/pkexec', '/usr/bin/dpkg', '--install', str(self.root/'package.deb')])
        self.assertNotIn('timeout', run.call_args.kwargs)
        self.assertNotIn('PYTHONPATH', run.call_args.kwargs['env'])

    def test_remove_is_limited_to_our_package(self):
        run = Mock(return_value=subprocess.CompletedProcess([], 0, '', ''))
        manager = AdminHelperSetup(run)
        with patch.object(manager, 'status', side_effect=['ready', 'missing']):
            manager.execute('remove')
        self.assertEqual(run.call_args.args[0], ['/usr/bin/pkexec', '/usr/bin/dpkg', '--remove', PACKAGE])

    def test_cancel_and_install_failure_are_not_reported_as_success(self):
        for code, exception in ((126, SetupCancelled), (1, RuntimeError)):
            run = Mock(return_value=subprocess.CompletedProcess([], code, '', 'failed'))
            manager = AdminHelperSetup(run)
            with patch.object(manager, 'status', return_value='missing'), patch.object(manager, 'build', return_value=self.root/'package.deb'), self.assertRaises(exception):
                manager.execute('install')

    def test_managed_package_detects_policy_update_and_rejects_foreign_owner(self):
        helper, policy, source = self.root/'helper', self.root/'policy', self.root/'source'
        helper.write_text('helper')
        policy.write_text('expected policy')
        source.write_text('expected policy')
        def run(command, **kwargs):
            text = 'install ok installed\t'+MAINTAINER if '-W' in command else PACKAGE + ': ' + command[-1]
            return subprocess.CompletedProcess([], 0, text, '')
        manager = AdminHelperSetup(run)
        with patch.object(manager, 'check_platform'), patch('clam_gui.admin_helper_setup.HELPER', helper), patch('clam_gui.admin_helper_setup.POLICY', policy), patch('clam_gui.admin_helper_setup.POLICY_SOURCE', source), patch('clam_gui.admin_helper_setup.available', return_value=True):
            self.assertEqual(manager.status(), 'ready')
            policy.write_text('older policy')
            self.assertEqual(manager.status(), 'update')
            manager.run = Mock(side_effect=[subprocess.CompletedProcess([],0,'install ok installed\t'+MAINTAINER,''), subprocess.CompletedProcess([],0,'foreign-package: '+str(helper),'')])
            with self.assertRaisesRegex(ValueError, '上書き・削除'):
                manager.status()

    def test_unsafe_installation_directory_is_refused_before_changes(self):
        manager = AdminHelperSetup()
        with patch('clam_gui.packages.AptManager.check_platform'), \
                patch('clam_gui.admin_helper_setup.Path.is_file', return_value=True), \
                patch('clam_gui.admin_helper_setup.HELPER', self.root/'helper'):
            with self.assertRaisesRegex(ValueError, '安全な設置先'):
                manager.check_platform()

    def test_removed_package_and_half_configured_package_states(self):
        manager = AdminHelperSetup()
        missing = self.root/'missing'
        for state, expected in (('deinstall ok not-installed', 'missing'), ('deinstall ok config-files', 'missing'), ('install ok unpacked', 'update')):
            manager.run = Mock(return_value=subprocess.CompletedProcess([], 0, state+'\t'+MAINTAINER, ''))
            with patch.object(manager, 'check_platform'), patch('clam_gui.admin_helper_setup.HELPER', missing), patch('clam_gui.admin_helper_setup.POLICY', missing), patch('clam_gui.admin_helper_setup.available', return_value=False):
                self.assertEqual(manager.status(), expected)
