import unittest
from clam_gui.core import HistoryEntry
from clam_gui.report import build_report
from clam_gui.report_text import report_text


class ReportTextTests(unittest.TestCase):
    def render(self, output):
        entry = HistoryEntry(1, '2026-11-21T02:00:00', '2026-11-21T05:55:00', 0,
                             'private conditions', '検査不完了', output, 'incomplete')
        return report_text(build_report(entry))

    def test_continuous_document_contains_summary_paths_reasons_and_skips(self):
        text, ranges = self.render('/example/資料: Access denied\n'
                                   '/example/directory: Can\'t open directory.\n'
                                   '空ファイルのため検査省略：12件\n')
        for value in ('検査不完了', '次の対応', '完了日時', '権限不足：1件',
                      '/example/資料\nAccess denied', 'ディレクトリを開けない：1件',
                      "Can't open directory.", '空ファイルのため検査省略：12件'):
            self.assertIn(value, text)
        self.assertNotIn('private conditions', text)
        self.assertTrue(ranges)
        for style, start, end in ranges:
            self.assertIn(style, ('title', 'heading'))
            self.assertTrue(0 <= start < end <= len(text))
        self.assertTrue(any(text[start:end] == 'ディレクトリを開けない：1件' for _, start, end in ranges))

    def test_plain_text_keeps_markup_and_reports_preview_limit(self):
        text, _ = self.render('/example/<b>text</b>: Access denied\n' * 110)
        self.assertEqual(text.count('/example/<b>text</b>'), 100)
        self.assertIn('権限不足：110件', text)
        self.assertIn('先頭100件', text)

    def test_no_findings_has_explanation_and_no_raw_settings(self):
        text, _ = self.render('Scanned files: 1\n-- この検査で使用した設定 ---\nPDF: 有効\n')
        self.assertIn('明細はありません', text)
        self.assertNotIn('PDF: 有効', text)

    def test_full_category_copy_includes_records_beyond_preview_limit(self):
        from clam_gui.report_text import group_text
        output = ''.join(f'/example/file-{i}: Access denied\n' for i in range(110))
        entry = HistoryEntry(1, '2026-11-21T02:00:00', '2026-11-21T05:55:00', 0,
                             '', '検査不完了', output, 'incomplete')
        preview = build_report(entry)
        full = build_report(entry, preview_limit=None)
        self.assertEqual(len(preview.groups[0].examples), 100)
        self.assertEqual(len(full.groups[0].examples), 110)
        copied = group_text(full.groups[0])
        self.assertIn('/example/file-109\nAccess denied', copied)
        self.assertNotIn('先頭100件', copied)
        self.assertIn('権限不足：110件', copied)
        self.assertIn('/example/file-109', report_text(full)[0])

    def test_full_copy_keeps_truncation_notice_and_separates_categories(self):
        output = ('出力が大きいため末尾のみ保存しています。\n'
                  '/example/private: Access denied\n'
                  '/example/folder: Can\'t open directory.\nInfected files: 57\n')
        entry = HistoryEntry(1, '2026-11-21T02:00:00', '2026-11-21T05:55:00', 0,
                             '', '検査不完了', output, 'incomplete')
        full = build_report(entry, preview_limit=None)
        self.assertIn('全体の内訳ではありません', full.coverage)
        self.assertEqual([group.count for group in full.groups], [1, 1])
        self.assertEqual(len(full.groups[0].examples), 1)
