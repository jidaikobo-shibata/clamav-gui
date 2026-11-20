import unittest
from clam_gui.core import HistoryEntry
from clam_gui.report import build_report, PREVIEW_LIMIT


def entry(output, status='検査不完了', detected=0, kind='incomplete'):
    return HistoryEntry(1, '2026-11-20T02:00:00', '2026-11-20T05:52:20',
                        detected, 'synthetic plan', status, output, kind)


class ReportTests(unittest.TestCase):
    def test_truncated_old_log_does_not_reclassify_warning_as_threat(self):
        output = '出力が大きいため末尾のみ保存しています。\n'
        output += '/example/large: Heuristics.Limits.Exceeded.MaxFileSize FOUND\n' * 123
        output += '/example/encrypted: Heuristics.Encrypted.OLE2 FOUND\n' * 2
        output += '/example/large: Virus(es) detected ERROR\n'
        output += 'Infected files: 57\nTotal errors: 303\nScanned files: 3302710\nTime: 13940.468 sec\n'
        report = build_report(entry(output))
        self.assertEqual(report.title, '要確認・内訳不明')
        self.assertFalse(report.detections)
        self.assertEqual([group.count for group in report.groups], [123, 2, 1])
        self.assertIn('全体の内訳ではありません', report.coverage)
        self.assertIn('303件', report.coverage)
        self.assertIn('明細は1件', report.coverage)
        self.assertIn(('所要時間', '3時間52分20秒'), report.metrics)
        self.assertEqual(len(report.groups[0].examples), PREVIEW_LIMIT)

    def test_detection_and_errors_are_independent(self):
        report = build_report(entry('/example/a: b: Eicar-Test-Signature FOUND\n'
                                    '/example/secret: Access denied ERROR\n'
                                    '/example/gone: No such file or directory ERROR\n', detected=1))
        self.assertEqual(report.title, '検出あり・検査不完了')
        self.assertEqual(report.detections[0].examples[0].path, '/example/a: b')
        self.assertEqual([group.title for group in report.groups], ['権限不足', '検査中に消失した対象'])

    def test_clean_skips_and_conditions(self):
        output = ('/example/a: Empty file\n/example/link: Symbolic link\n'
                  '空ファイルのため検査省略：12件\nScanned files: 20\nInfected files: 0\n'
                  '-- この検査で使用した設定 ---\nPDF: 有効\n')
        report = build_report(entry(output, '検出なし', kind='clean'))
        self.assertEqual(report.title, '検査完了・検出なし')
        self.assertFalse(report.groups)
        self.assertIn(('空ファイルのため検査省略', '12件'), report.skips)
        self.assertIn('PDF: 有効', report.conditions)

    def test_missing_summary_and_cancelled_run(self):
        report = build_report(entry('ERROR: ClamAVが見つかりません。', '検査不完了：ClamAVが見つかりません。'))
        self.assertIn(('検査ファイル数', '不明（記録なし）'), report.metrics)
        report = build_report(entry('', '検査を中止しました', kind='cancelled'))
        self.assertEqual(report.title, '検査を中止しました')

    def test_large_previews_bounded_and_text_not_interpreted(self):
        report = build_report(entry('/example/<markup>: Test FOUND\n' * 1000, detected=1))
        self.assertEqual(report.detections[0].count, 1000)
        self.assertEqual(len(report.detections[0].examples), PREVIEW_LIMIT)
        self.assertEqual(report.detections[0].examples[0].path, '/example/<markup>')

    def test_corrupt_numeric_fields_do_not_break_report(self):
        output = 'Time: ' + '9' * 5000 + ' sec\nScanned files: ' + '9' * 5000 + '\n'
        report = build_report(entry(output))
        self.assertIn(('検査ファイル数', '不明（記録なし）'), report.metrics)
