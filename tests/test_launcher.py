import tempfile
from pathlib import Path
import unittest
from scripts.install_launcher import render_entry, install, exec_arg


class LauncherTests(unittest.TestCase):
    def test_entry_matches_application_and_does_not_use_a_shell(self):
        root = Path(__file__).resolve().parents[1]
        name, text = render_entry(root)
        self.assertEqual(name, 'com.jidaikobo.ClamAVGui.desktop')
        self.assertIn('Terminal=false', text)
        self.assertIn(exec_arg(str(root/'scripts/launch_gui.py')), text)
        self.assertNotIn('sh -c', text)
        self.assertNotIn('Autostart', text)

    def test_install_never_overwrites_existing_entry(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(__file__).resolve().parents[1]
            path = install(source, root)
            content = path.read_text(encoding='utf-8')
            with self.assertRaises(FileExistsError):
                install(source, root)
            self.assertEqual(path.read_text(encoding='utf-8'), content)

    def test_activation_file_is_installed_and_foreign_file_preserved(self):
        from clam_gui.activation import install_activation
        from clam_gui.desktop_notifications import APP_ID
        with tempfile.TemporaryDirectory() as root:
            source = Path(__file__).resolve().parents[1]
            desktop = install(source, root)
            service = Path(root)/'dbus-1/services'/(APP_ID+'.service')
            self.assertIn('DBusActivatable=true', desktop.read_text())
            self.assertIn('--gapplication-service', service.read_text())
            self.assertTrue(install_activation(source, root))
            service.write_text('foreign')
            with self.assertRaises(ValueError):
                install_activation(source, root)
            self.assertEqual(service.read_text(), 'foreign')

    def test_escaping_and_newlines(self):
        value = exec_arg('/tmp/a b%"$')
        self.assertIn('%%', value)
        self.assertIn('\\"', value)
        self.assertIn('\\$', value)
        with self.assertRaises(ValueError):
            exec_arg('a\nb')

    def test_launch_wrapper_uses_requested_source(self):
        from unittest.mock import patch
        import sys
        from scripts.launch_gui import main
        source = Path(__file__).resolve().parents[1]
        previous = list(sys.path)
        try:
            with patch('sys.argv', ['launcher', '--source-root', str(source)]), \
                 patch('scripts.launch_gui.runpy.run_module') as run:
                main()
                run.assert_called_once_with('clam_gui', run_name='__main__')
                self.assertEqual(sys.path[0], str(source))
        finally:
            sys.path[:] = previous
