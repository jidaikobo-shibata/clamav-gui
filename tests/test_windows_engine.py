import os
import subprocess
import unittest
from unittest.mock import patch

from clam_gui.platforms import windows_engine as engine


@unittest.skipUnless(os.name == 'nt', 'Windows scanner startup')
class EngineSetupTests(unittest.TestCase):
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
