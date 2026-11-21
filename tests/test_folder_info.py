from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from clam_gui.folder_info import count_children, local_folder_path


class FolderInfoTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_immediate_counts_include_hidden_files_but_do_not_recurse(self):
        (self.root/'a').write_text('data')
        (self.root/'.hidden').write_text('data')
        (self.root/'sub').mkdir()
        (self.root/'sub'/'nested').write_text('nested')
        result = count_children(self.root)
        self.assertEqual((result.files, result.directories), (2, 1))
        self.assertTrue(result.complete)
        self.assertEqual(result.label(), '直下：ファイル2件・フォルダー1件')

    def test_symlinks_are_counted_without_following(self):
        (self.root/'sub').mkdir()
        (self.root/'link').symlink_to(self.root/'sub', target_is_directory=True)
        result = count_children(self.root)
        self.assertEqual((result.directories, result.links), (1, 1))
        self.assertEqual(count_children(self.root/'link').reason, 'symlink')

    def test_empty_directory_and_unavailable_are_distinct(self):
        self.assertTrue(count_children(self.root).complete)
        self.assertEqual(count_children(self.root).files, 0)
        self.assertEqual(count_children(self.root/'missing').reason, 'unavailable')
        with patch('clam_gui.folder_info.os.scandir', side_effect=PermissionError()):
            self.assertIn('確認できません', count_children(self.root).label())

    def test_count_limit_is_explicit(self):
        for index in range(3):
            (self.root/str(index)).touch()
        result = count_children(self.root, limit=2)
        self.assertEqual(result.files, 2)
        self.assertFalse(result.complete)
        self.assertIn('集計上限', result.label())

    def test_cancelled_inspection_returns_no_result(self):
        cancel = threading.Event()
        cancel.set()
        with patch('clam_gui.folder_info.os.scandir') as scan:
            self.assertIsNone(count_children(self.root, cancel=cancel))
            scan.assert_not_called()

    def test_vanishing_entry_is_not_reported_as_successful_zero(self):
        item = Mock()
        item.is_symlink.return_value = False
        item.is_file.side_effect = FileNotFoundError()
        context = Mock()
        context.__enter__ = Mock(return_value=iter([item]))
        context.__exit__ = Mock(return_value=False)
        with patch('clam_gui.folder_info.os.scandir', return_value=context):
            result = count_children(self.root)
        self.assertFalse(result.complete)
        self.assertEqual(result.unknown, 1)

    def test_launcher_validation_accepts_only_existing_local_directories(self):
        self.assertEqual(local_folder_path(str(self.root)), str(self.root))
        (self.root/'file').touch()
        for value in ('relative', 'https://example.com', str(self.root/'file'), str(self.root/'missing')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                local_folder_path(value)
