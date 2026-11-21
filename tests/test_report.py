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

    def test_detection_retention_loss_is_explicit_without_opening_raw_log(self):
        output = ('WARNING: 脅威の報告200件に対し、検出明細は1件のみ保持できました。'
                  '検出明細の保存上限に達したため、全件を特定できません。検出なしではありません。\n'
                  '/example/eicar: Eicar-Test-Signature FOUND\nInfected files: 200\n')
        report = build_report(entry(output, '検出あり', 1, 'detected'))
        self.assertEqual(report.title, '検出あり・検査不完了')
        self.assertIn('特定できない検出対象', report.description)
        self.assertIn('検査範囲を分けて再検査', report.action)
        self.assertIn('200件', report.cautions[0])
        self.assertIn('1件のみ', report.cautions[0])
        self.assertEqual(report.detections[0].count, 1)
        self.assertFalse(report.groups)

    def test_engine_error_preserves_function_and_diagnostic(self):
        output = 'LibClamAV Error: cli_loaddbdir(): No supported database files found\n'
        report = build_report(entry(output))
        self.assertEqual(report.title, '検査不完了')
        self.assertEqual(report.groups[0].title, 'ClamAV本体のエラー')
        self.assertEqual(report.groups[0].examples[0].path, '')
        self.assertEqual(report.groups[0].examples[0].detail,
                         'cli_loaddbdir(): No supported database files found')
        self.assertIn('定義ファイル', report.groups[0].action)

    def test_cancelled_with_detection_preserves_both_outcomes(self):
        report = build_report(entry('/example/eicar: Test FOUND\n',
                                    '検査を中止しました', 1, 'cancelled'))
        self.assertEqual(report.title, '検出あり・検査中止')
        self.assertIn('未検査', report.description)
        self.assertIn('再検査', report.action)
        self.assertEqual(report.detections[0].count, 1)

    def test_cancelled_uncertain_log_keeps_uncertainty_and_cancellation(self):
        report = build_report(entry('出力が大きいため末尾のみ保存しています。\nInfected files: 57\n',
                                    '検査を中止しました', 0, 'cancelled'))
        self.assertEqual(report.title, '要確認・内訳不明')
        self.assertIn('中止', report.description)
        self.assertIn('未検査', report.action)

    def test_application_execution_error_preserves_full_reason(self):
        reason = '[Errno 13] Permission denied: /example/clamscan'
        report = build_report(entry('ClamAVの実行エラー：' + reason,
                                    '検査不完了：ClamAVを実行できませんでした'))
        self.assertEqual(report.groups[0].title, 'ClamAVの実行エラー')
        self.assertEqual(report.groups[0].examples[0].detail, reason)
        self.assertEqual(report.groups[0].examples[0].path, '')
        self.assertIn('実行権限', report.action)
        self.assertFalse(report.detections)

    def test_storage_error_with_detection_keeps_detection_and_distinct_count(self):
        report = build_report(entry('/example/eicar: Test FOUND\nTotal errors: 0\n'
                                    '履歴保存エラー：database is locked: /example/history.sqlite3', detected=1))
        self.assertEqual(report.title, '検出あり・検査不完了')
        self.assertEqual(report.detections[0].count, 1)
        group = report.groups[0]
        self.assertEqual(group.title, '履歴保存エラー')
        self.assertEqual(group.examples[0].detail, 'database is locked: /example/history.sqlite3')
        self.assertIn('空き容量', group.action)
        self.assertIn('コピー', group.action)
        self.assertIn('エラー明細は0件', report.coverage)

    def test_storage_error_alone_does_not_require_rescan(self):
        report = build_report(entry('履歴保存エラー：No space left on device'))
        self.assertIn('保存先', report.action)
        self.assertNotIn('再検査', report.action)

    def test_application_errors_are_bounded_and_not_signature_or_file_paths(self):
        report = build_report(entry('ClamAVの実行エラー：message FOUND\n' * 150 +
                                    '/example/履歴保存エラー：file: Permission denied ERROR\n'))
        self.assertFalse(report.detections)
        self.assertEqual(report.groups[0].count, 150)
        self.assertEqual(len(report.groups[0].examples), PREVIEW_LIMIT)
        self.assertEqual(report.groups[1].title, '権限不足')

    def test_cancelled_with_storage_failure_keeps_rescan_and_storage_advice(self):
        from clam_gui.core import storage_warning
        from clam_gui.results import ScanStatus, ResultKind
        status, output = storage_warning(ScanStatus('検査を中止しました', ResultKind.CANCELLED),
                                         '', OSError('No space left on device'))
        report = build_report(entry(output, str(status), 0, status.assessment.kind))
        self.assertEqual(report.title, '検査を中止しました')
        self.assertIn('未検査', report.description)
        self.assertIn('再検査', report.action)
        self.assertIn('空き容量', report.action)
        self.assertIn('コピー', report.action)

    def test_preflight_and_storage_failure_keep_original_reason(self):
        from clam_gui.core import record_scan_failure
        from unittest.mock import Mock
        store = Mock()
        store.save.side_effect = OSError('database is locked')
        status, output = record_scan_failure(store, Mock(), ValueError('ClamAVが見つかりません。'))
        report = build_report(entry(output, str(status), 0, status.assessment.kind))
        self.assertIn('ClamAVが見つかりません', report.description)
        self.assertIn('database is locked', report.description)
        self.assertIn('再検査', report.action)
        self.assertIn('保存先', report.action)
        self.assertEqual(report.groups[0].title, '履歴保存エラー')

    def test_detected_cancelled_and_storage_failure_preserve_all_actions(self):
        report = build_report(entry('/example/a: Test FOUND\n履歴保存エラー：disk full',
                                    '検査を中止しました', 1, 'cancelled'))
        self.assertEqual(report.title, '検出あり・検査中止')
        for text in ('入手元', '再検査', '保存先', 'コピー'):
            self.assertIn(text, report.action)

    def test_clean_scan_with_storage_failure_does_not_force_rescan(self):
        from clam_gui.core import storage_warning
        from clam_gui.results import ScanStatus, ResultKind
        status, output = storage_warning(ScanStatus('検査完了', ResultKind.CLEAN),
                                         'Scanned files: 1\nInfected files: 0\n', OSError('disk full'))
        report = build_report(entry(output, str(status), 0, status.assessment.kind))
        self.assertIn('保存先', report.action)
        self.assertNotIn('再検査', report.action)

    def test_unsuffixed_access_denied_and_directory_failures_complete_error_counts(self):
        output = ''.join(f'/example/data/{i}: Access denied\n' for i in range(110))
        output += ''.join(f'/example/directory/{i}: Can\'t open directory.\n' for i in range(37))
        output += '/example/large: Virus(es) detected ERROR\n' * 157
        output += 'Total errors: 304\n'
        report = build_report(entry(output))
        groups = {group.title: group for group in report.groups}
        self.assertEqual(groups['権限不足'].count, 110)
        self.assertEqual(len(groups['権限不足'].examples), PREVIEW_LIMIT)
        self.assertEqual(groups['権限不足'].examples[0].path, '/example/data/0')
        self.assertEqual(groups['ディレクトリを開けない'].count, 37)
        self.assertIn('参照権限', groups['ディレクトリを開けない'].action)
        self.assertEqual(groups['その他の検査エラー'].count, 157)
        self.assertIn('エラー明細は304件', report.coverage)
        self.assertFalse(report.detections)

    def test_unsuffixed_errors_support_colons_and_windows_paths(self):
        paths = ['/example/a: b', r'C:\Users\Example\data', r'\\server\share\folder']
        report = build_report(entry(''.join(path + ': Permission denied\n' for path in paths)))
        self.assertEqual(report.groups[0].count, 3)
        self.assertEqual([finding.path for finding in report.groups[0].examples], paths)

    def test_plain_diagnostic_text_is_not_mistaken_for_file_error(self):
        report = build_report(entry('note: Access denied\nExcluded\n/example/Access denied: Excluded\n'))
        self.assertFalse(report.groups)
        self.assertFalse(report.detections)

    def test_directory_errors_with_and_without_suffix_share_one_group(self):
        report = build_report(entry('/example/a: Can\'t open directory.\n'
                                    '/example/b: Can\'t open directory. ERROR\n'))
        self.assertEqual(len(report.groups), 1)
        self.assertEqual(report.groups[0].title, 'ディレクトリを開けない')
        self.assertEqual(report.groups[0].count, 2)
