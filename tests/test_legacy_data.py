import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from clam_gui.core import Store


class LegacyDataTests(unittest.TestCase):
    def test_macos_retains_previous_store_and_respects_xdg_isolation(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch('clam_gui.core.sys.platform', 'darwin'), \
                patch('clam_gui.core.Path.home', return_value=Path(directory)), \
                patch.dict(os.environ):
            os.environ.pop('XDG_DATA_HOME', None)
            previous = Store(Path(directory) / '.local/share/clamav-gui')
            previous.save('previous', 'target', '検出なし', 'retained history')
            current = Store()
            self.assertEqual(current.path, previous.path)
            self.assertEqual(current.recent()[0][3], 'retained history')
            self.assertFalse((Path(directory) / 'Library/Application Support/ClamAV GUI').exists())
            os.environ['XDG_DATA_HOME'] = str(Path(directory) / 'isolated')
            isolated = Store()
            self.assertEqual(isolated.path, Path(directory) / 'isolated/clamav-gui/history.sqlite3')
            self.assertEqual(isolated.recent(), [])

    def test_existing_legacy_database_is_reused_without_copying(self):
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'XDG_DATA_HOME':directory}):
            old=Store(Path(directory)/'clam-gui')
            old.save('old','target','検出なし','existing history')
            current=Store()
            self.assertEqual(current.path,old.path)
            self.assertEqual(len(current.recent()),1)
            self.assertFalse((Path(directory)/'clamav-gui').exists())

    def test_existing_new_database_and_explicit_directory_take_precedence(self):
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'XDG_DATA_HOME':directory}):
            Store(Path(directory)/'clam-gui')
            new=Store(Path(directory)/'clamav-gui')
            self.assertEqual(Store().path,new.path)
            explicit=Path(directory)/'explicit'
            self.assertEqual(Store(explicit).path,explicit/'history.sqlite3')


    def test_old_policy_description_still_finds_history(self):
        from clam_gui.core import Plan
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            plan = Plan('test', [directory], [])
            key = store.save_profile(plan)
            store.save('old', plan.history_descriptions()[1], '検出なし', 'old history')
            entries = store.history_headers(key, plan)
            self.assertEqual(len(entries), 1)
            self.assertEqual(store.history_entry(entries[0].id, key, plan).output, 'old history')
