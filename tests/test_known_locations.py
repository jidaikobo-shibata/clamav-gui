from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from clam_gui.known_locations import trash_folder, resolve_location, location_label, location_confirmation
from clam_gui.core import Store, Plan


class KnownLocationsTests(unittest.TestCase):
    def test_xdg_default_and_relative_fallback(self):
        expected = Path('/example/home/.local/share/Trash/files')
        for env in ({}, {'XDG_DATA_HOME': ''}, {'XDG_DATA_HOME': 'relative'}):
            self.assertEqual(trash_folder(system='linux', env=env, home='/example/home'), expected)

    def test_custom_absolute_xdg(self):
        self.assertEqual(trash_folder(system='linux', env={'XDG_DATA_HOME': '/example/data'}, home='/example/home'),
                         Path('/example/data/Trash/files'))
        self.assertIsNone(trash_folder(system='darwin'))
        self.assertIsNone(trash_folder(system='win32'))

    def test_friendly_names_and_real_path_confirmation(self):
        with patch('clam_gui.known_locations.sys.platform', 'linux'), \
             patch.dict('os.environ', {'XDG_DATA_HOME': '/example/data'}):
            path = '/example/data/Trash/files'
            self.assertEqual(resolve_location('ごみ箱'), path)
            self.assertEqual(location_label(path + '/'), 'ごみ箱')
            self.assertEqual(location_label(path + '/child'), 'ごみ箱 / child')
            self.assertEqual(location_label('/unrelated/Trash/files'), '/unrelated/Trash/files')
            self.assertEqual(location_confirmation(path), 'ごみ箱\n' + path)
            self.assertEqual(resolve_location('/ordinary/path'), '/ordinary/path')

    def test_future_trash_can_be_registered_without_creating_or_duplicating_it(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch('clam_gui.known_locations.sys.platform', 'linux'), \
             patch.dict('os.environ', {'XDG_DATA_HOME': str(Path(directory)/'data')}):
            store = Store(Path(directory)/'app')
            plan = Plan('Home', [directory], [])
            key = store.save_profile(plan)
            path = str(trash_folder())
            self.assertFalse(Path(path).exists())
            self.assertEqual(store.add_excluded_folders(key, [path], expected_target=plan.targets), [path])
            self.assertEqual(store.add_excluded_folders(key, [path], expected_target=plan.targets), [])
            self.assertFalse(Path(path).exists())
            self.assertEqual(dict(store.profiles())[key].excluded_dirs, [path])

    def test_known_trash_does_not_bypass_threat_or_scope_checks(self):
        from clam_gui.plan_exclusions import exclusion_state
        with tempfile.TemporaryDirectory() as directory, \
             patch('clam_gui.known_locations.sys.platform', 'linux'), \
             patch.dict('os.environ', {'XDG_DATA_HOME': str(Path(directory)/'data')}):
            path = str(trash_folder())
            plan = Plan('Home', [directory], [])
            self.assertIn('脅威', exclusion_state(plan, path, [path+'/test']))
            self.assertTrue(exclusion_state(Plan('Other', [str(Path(directory)/'other')], []), path))
