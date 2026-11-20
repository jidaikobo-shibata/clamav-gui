import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from clam_gui.core import Store


class LegacyDataTests(unittest.TestCase):
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
