import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from clam_gui.platforms import windows_engine as engine


@unittest.skipUnless(os.name == 'nt', 'Windows scanner startup')
class EngineSetupTests(unittest.TestCase):
    def test_scheduled_check_shares_only_successful_checks(self):
        from clam_gui.platforms.windows import Updates
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, LOCALAPPDATA=root):
            folder = Path(root)/'ClamAV'/'database'
            folder.mkdir(parents=True)
            for name in ('daily', 'main', 'bytecode'):
                (folder/(name+'.cvd')).write_bytes(b'fixture')
            with patch('clam_gui.platforms.windows.executable', return_value='freshclam'), \
                 patch('clam_gui.platforms.windows_process.JobProcess') as job:
                job.return_value.wait.return_value = 0
                Updates().update()
                Updates().update(scheduled=True)
                self.assertEqual(job.call_count, 2)  # FreshClam and initial load check
                marker = folder.parent/'freshclam-success'
                old = time.time() - 3 * 86400
                os.utime(marker, (old, old))
                job.return_value.wait.return_value = 2
                with self.assertRaises(RuntimeError):
                    Updates().update(scheduled=True)
                self.assertAlmostEqual(marker.stat().st_mtime, old, places=4)
                job.return_value.wait.return_value = 0
                Updates().update(scheduled=True)
                self.assertEqual(job.call_count, 4)

    def test_failed_migration_keeps_legacy_until_verified_retry(self):
        from clam_gui.engine import database, legacy_database, shared_database
        from clam_gui.platforms.windows import Updates
        from clam_gui.platforms.windows_process import DefinitionLock
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, LOCALAPPDATA=root), \
             patch('clam_gui.platforms.windows.executable', side_effect=lambda name: name), \
             patch('clam_gui.platforms.windows_process.JobProcess') as job:
            legacy = legacy_database()
            legacy.mkdir(parents=True)
            for name in ('daily', 'main', 'bytecode'):
                (legacy/(name+'.cld')).write_bytes(b'fixture')
            shared = shared_database()
            reader = DefinitionLock(shared, shared=True)
            self.assertTrue(reader.acquire())
            try:
                with self.assertRaises(ValueError):
                    Updates().update()
                job.assert_not_called()
            finally:
                reader.close()
            job.return_value.wait.side_effect = [0, 0, 2, 2]
            with self.assertRaises(RuntimeError):
                Updates().update()
            self.assertEqual(database(), legacy)
            self.assertFalse(shared.exists())
            job.return_value.wait.side_effect = None
            job.return_value.wait.return_value = 0
            Updates().update(scheduled=True)
            self.assertEqual(database(), shared)
            self.assertEqual((shared/'daily.cld').read_bytes(), (legacy/'daily.cld').read_bytes())

    def test_setup_checks_both_programs(self):
        with patch.object(engine, 'executable', side_effect=lambda name: name + '.exe'), \
             patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, b'help')) as run:
            engine.prepare_install()
        self.assertEqual([call.args[0] for call in run.call_args_list],
                         [['clamscan.exe', '--help'], ['freshclam.exe', '--help']])
        self.assertTrue(all('env' not in call.kwargs for call in run.call_args_list))

    def test_missing_dll_reports_loader_error(self):
        with patch.object(engine, 'executable', return_value='clamscan.exe'), \
             patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 0xc0000135)):
            with self.assertRaisesRegex(ValueError, 'DLL.*0xC0000135'):
                engine.prepare_install()
