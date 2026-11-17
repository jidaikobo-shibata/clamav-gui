import datetime
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from clam_gui.definitions import definition_update_label


class DefinitionInfoTests(unittest.TestCase):
    def test_displays_newest_local_database_modification_time(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            for name, stamp in [('daily.cvd', 1760000000), ('main.cld', 1760000120),
                                ('custom.cud', 1760000060), ('unrelated.txt', 1900000000)]:
                file = root/name
                file.write_bytes(b'test database')
                os.utime(file, (stamp, stamp))
            (root/'empty.cvd').touch()
            (root/'directory.cld').mkdir()
            expected = datetime.datetime.fromtimestamp(1760000120).astimezone().strftime('%Y/%m/%d %H:%M')
            self.assertEqual(definition_update_label(root), '定義ファイル更新：' + expected)

    def test_no_database_is_not_reported_as_a_known_update(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertIn('未取得または確認できません', definition_update_label(root))
            self.assertIn('未取得または確認できません', definition_update_label(Path(root)/'missing'))

    def test_permission_error_is_visible(self):
        with patch('clam_gui.definitions.Path.glob', side_effect=PermissionError('denied')):
            self.assertEqual(definition_update_label(), '定義ファイル更新：日時を確認できません')
