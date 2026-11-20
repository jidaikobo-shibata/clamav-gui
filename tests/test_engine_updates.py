import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from clam_gui.packages import AptManager


class EngineUpdateTests(unittest.TestCase):
    def manager(self, installed='1:1.5.4-1', candidate='1:1.5.5-1', code=0):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        release = Path(temp.name)/'os-release'
        release.write_text('ID=ubuntu\n')
        run = Mock(side_effect=[
            subprocess.CompletedProcess([], 0, f'clamav\tinstall ok installed\t{installed}\n' if installed else '', ''),
            subprocess.CompletedProcess([], 0, f'clamav:\n  Candidate: {candidate}\n', ''),
            subprocess.CompletedProcess([], code, '', '')])
        return AptManager(run=run, release=release), run

    def test_update_and_debian_version_comparison(self):
        manager, run = self.manager()
        label, detail = manager.engine_update_status()
        self.assertIn('1:1.5.4-1', label)
        self.assertIn('更新あり → 1:1.5.5-1', label)
        self.assertIn('古い場合', detail)
        self.assertEqual(run.call_args_list[-1].args[0],
                         ['/usr/bin/dpkg', '--compare-versions', '1:1.5.4-1', 'lt', '1:1.5.5-1'])
        self.assertTrue(all(call.kwargs['timeout'] <= 15 for call in run.call_args_list))

    def test_no_update_does_not_claim_latest(self):
        manager, _ = self.manager(candidate='1:1.5.4-1', code=1)
        label, _ = manager.engine_update_status()
        self.assertIn('更新なし（APT情報）', label)
        self.assertNotIn('最新', label)

    def test_uninstalled_does_not_query_candidate(self):
        manager, run = self.manager(installed=None)
        self.assertIn('APT未導入', manager.engine_update_status()[0])
        self.assertEqual(run.call_count, 1)

    def test_no_candidate_is_unknown(self):
        manager, run = self.manager(candidate='(none)')
        self.assertIn('更新状態不明', manager.engine_update_status()[0])
        self.assertEqual(run.call_count, 2)

    def test_failed_or_malformed_candidate_is_unknown(self):
        for code, text in [(100, ''), (0, ''), (0, 'Candidate: 1\nCandidate: 2\n')]:
            with self.subTest(code=code, text=text):
                manager, run = self.manager()
                run.side_effect = [subprocess.CompletedProcess([], 0, 'clamav\tinstall ok installed\t1\n', ''),
                                   subprocess.CompletedProcess([], code, text, '')]
                self.assertIn('更新状態不明', manager.engine_update_status()[0])

    def test_comparison_failure_is_unknown(self):
        manager, _ = self.manager(code=2)
        self.assertIn('更新状態不明', manager.engine_update_status()[0])

    def test_timeout_preserves_installed_version(self):
        manager, run = self.manager()
        run.side_effect = [subprocess.CompletedProcess([], 0, 'clamav\tinstall ok installed\t1:1.5.4-1\n', ''),
                           subprocess.TimeoutExpired('apt-cache', 15)]
        label, _ = manager.engine_update_status()
        self.assertIn('1:1.5.4-1', label)
        self.assertIn('更新状態不明', label)

    def test_footer_checks_are_single_flight_throttled_and_forceable(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from clam_gui.ui.packages import PackageWindow
        manager = Mock()
        manager.engine_update_status.return_value = ('version label', 'detail')
        host = SimpleNamespace(engine_checking=False, engine_checked_at=None,
                               engine_check_pending=False, engine_label=Mock(),
                               engine_refresh=Mock(), backend=Mock())
        host.backend.packages.return_value = manager
        host.refresh_engine_status = lambda force=False: PackageWindow.refresh_engine_status(host, force)
        deliveries = []
        with patch('clam_gui.ui.packages.start_worker', side_effect=lambda worker, failed: worker()), \
             patch('clam_gui.ui.packages.GLib.idle_add', side_effect=lambda fn, *args: deliveries.append((fn, args))):
            host.refresh_engine_status()
            host.refresh_engine_status()
            host.refresh_engine_status(force=True)
            self.assertEqual(manager.engine_update_status.call_count, 1)
            fn, args = deliveries.pop(0)
            fn(*args)
            self.assertEqual(manager.engine_update_status.call_count, 2)
            fn, args = deliveries.pop(0)
            fn(*args)
            host.refresh_engine_status()
            self.assertEqual(manager.engine_update_status.call_count, 2)
            self.assertFalse(host.engine_checking)
            host.engine_label.set_text.assert_called_with('version label')
            host.engine_refresh.set_sensitive.assert_called_with(True)

    def test_footer_start_failure_can_be_retried(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from clam_gui.ui.packages import PackageWindow
        host = SimpleNamespace(engine_checking=False, engine_checked_at=None,
                               engine_check_pending=False, engine_label=Mock(), engine_refresh=Mock())
        with patch('clam_gui.ui.packages.start_worker', side_effect=lambda worker, failed: failed(RuntimeError('cannot start'))):
            PackageWindow.refresh_engine_status(host)
        self.assertFalse(host.engine_checking)
        host.engine_label.set_text.assert_called_with('ClamAV：確認不可')
        host.engine_refresh.set_sensitive.assert_called_with(True)
