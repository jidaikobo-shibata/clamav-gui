import subprocess
import tempfile
import unittest
from unittest.mock import patch
from clam_gui.core import Store, summarize, has_detection
from clam_gui.options import DEFAULTS, OPTIONS, option_arguments, supported_options


class OptionTests(unittest.TestCase):
    def test_defaults_and_old_phishing_migration(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            with store.connect() as db:
                db.execute('INSERT INTO preferences VALUES (?, ?)', ('phishing', 'true'))
            settings = store.scan_options()
            self.assertTrue(settings['phishing-sigs'])
            self.assertTrue(settings['phishing-scan-urls'])
            self.assertTrue(settings['scan-pdf'])
            self.assertTrue(settings['alert-encrypted'])
            self.assertFalse(settings['detect-pua'])
            settings['phishing-sigs'] = False
            settings['scan-pdf'] = False
            store.set_scan_options(settings)
            self.assertEqual(Store(root).scan_options(), settings)

    def test_only_known_boolean_options_can_be_saved(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            for settings in [dict(DEFAULTS, remove=True), dict(DEFAULTS, **{'scan-pdf': 'yes'}), {}]:
                with self.assertRaises(ValueError):
                    store.set_scan_options(settings)

    def test_capabilities_and_unsupported_custom_value(self):
        result = subprocess.CompletedProcess([], 0, '--scan-pdf[=yes/no] --scan-mail')
        with patch('clam_gui.options.subprocess.run', return_value=result):
            self.assertEqual(supported_options('/clam'), {'scan-pdf', 'scan-mail'})
        arguments = option_arguments(DEFAULTS, {'scan-pdf'})
        self.assertEqual(arguments, ['--scan-pdf=yes'])
        with self.assertRaises(ValueError):
            option_arguments(dict(DEFAULTS, **{'scan-pdf': False}), set())
        with patch('clam_gui.options.subprocess.run', return_value=subprocess.CompletedProcess([], 1, '')):
            with self.assertRaises(ValueError):
                supported_options('/clam')

    def test_warning_only_is_not_a_threat(self):
        for signature in ['Heuristics.OLE2.ContainsMacros', 'Heuristics.Broken.Executable']:
            output = f'file: {signature} FOUND\nInfected files: 1'
            self.assertFalse(has_detection('', output))
            self.assertIn('確認が必要', summarize(1, output))
            mixed = output + '\nother: Win.Test FOUND'
            self.assertTrue(has_detection('', mixed))
            self.assertTrue(summarize(1, mixed).startswith('検出あり'))


class StandardDefaultTests(unittest.TestCase):
    def test_defaults_are_read_from_engine_help(self):
        from clam_gui.options import standard_defaults
        text = ('--phishing-sigs[=yes(*)/no]\n'
                '--phishing-scan-urls[=yes(*)/no]\n'
                '--alert-encrypted[=yes/no(*)]\n'
                '--scan-pdf[=yes(*)/no]\n')
        with patch('clam_gui.options.subprocess.run', return_value=subprocess.CompletedProcess([], 0, text)):
            values = standard_defaults('/clam')
        self.assertTrue(values['phishing-sigs'])
        self.assertTrue(values['phishing-scan-urls'])
        self.assertFalse(values['alert-encrypted'])
        self.assertNotIn('detect-pua', values)
        # The app's first-run policy remains unchanged for existing users.
        self.assertFalse(DEFAULTS['phishing-sigs'])
        self.assertTrue(DEFAULTS['alert-encrypted'])


class SizeLimitTests(unittest.TestCase):
    def test_defaults_and_atomic_save(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            self.assertEqual(store.scan_limits(), {'max-filesize': None, 'max-scansize': None})
            settings = store.scan_options()
            limits = {'max-filesize': 256, 'max-scansize': 512}
            store.set_scan_preferences(settings, limits)
            self.assertEqual(Store(root).scan_limits(), limits)
            changed = dict(settings, **{'scan-pdf': False})
            with self.assertRaises(ValueError):
                store.set_scan_preferences(changed, {'max-filesize': 512, 'max-scansize': 256})
            self.assertEqual(store.scan_options(), settings)
            self.assertEqual(store.scan_limits(), limits)

    def test_invalid_values_and_unsupported_limits(self):
        from clam_gui.options import validate_limits, limit_arguments
        for value in [0, -1, 2048, True, '100', 1.5]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_limits({'max-filesize': value, 'max-scansize': None})
        self.assertEqual(limit_arguments({'max-filesize': None, 'max-scansize': None}, set()), [])
        with self.assertRaises(ValueError):
            limit_arguments({'max-filesize': 256, 'max-scansize': 512}, set())
        self.assertEqual(limit_arguments({'max-filesize': 256, 'max-scansize': 512},
                                        {'max-filesize', 'max-scansize'}),
                         ['--max-filesize=256M', '--max-scansize=512M'])
