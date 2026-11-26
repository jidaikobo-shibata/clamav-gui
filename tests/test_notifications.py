import datetime
from pathlib import Path
import tempfile
import unittest
from clam_gui.notifications import definition_status, scan_notification


class NotificationTests(unittest.TestCase):
    def test_home_completion_is_concise(self):
        started = datetime.datetime(2026, 11, 16, 2, 0, 0)
        finished = started + datetime.timedelta(hours=1, minutes=23, seconds=4)
        title, body, urgency = scan_notification('ホーム全体', '検査完了：168ファイル',
            'Scanned files: 168\nEngine version: 1.5.4', started, finished, '定義ファイル：正常')
        self.assertIn('完了', title)
        self.assertEqual(body, '検査プラン：ホーム全体\n検出なし。')
        self.assertEqual(urgency, 'normal')

    def test_errors_and_detection_are_critical(self):
        now = datetime.datetime.now()
        for status in ['検出あり', '検出あり・検査不完了', '検査不完了']:
            title, body, urgency = scan_notification('対象', status, '', now, now, '定義不明')
            self.assertEqual(urgency, 'critical')
            self.assertIn(status, body)

    def test_definition_age(self):
        import os
        now = datetime.datetime.now().astimezone()
        with tempfile.TemporaryDirectory() as root:
            self.assertIn('確認できません', definition_status(root, now))
            path = Path(root)/'daily.cld'
            path.write_text(f"ClamAV-VDB:date:1:1:1:md5:signature:builder:{int(now.timestamp())}")
            self.assertIn('正常', definition_status(root, now))
            old = now.timestamp() - 5*86400
            path.write_text(f"ClamAV-VDB:date:1:1:1:md5:signature:builder:{int(old)}")
            self.assertIn('古い可能性', definition_status(root, now))

    def test_mailbox_notification_explains_unit_without_private_details(self):
        from clam_gui.mailboxes import MAILBOX_NOTE
        now = datetime.datetime.now()
        title, body, urgency = scan_notification('home', '検出あり\n' + MAILBOX_NOTE +
            '（保存ファイル：1件）\n保存ファイル自体は削除せず、メールアプリで確認してください。',
            '', now, now, '定義正常')
        self.assertIn('メール保存ファイル', title)
        self.assertIn('メールアプリで確認', body)
        self.assertEqual(urgency, 'critical')


class WarningComparisonTests(unittest.TestCase):
    def test_safe_comparison_preserves_critical_alerts_and_distinguishes_paths(self):
        from clam_gui.core import Store, Plan
        from clam_gui.results import summarize
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            plan = Plan('npm', [root], [])
            key = store.save_profile(plan)
            def report(paths, *, code=1, errors=0, extra='', target=None, settings='same'):
                output = '\n'.join(p + ': Heuristics.Limits.Exceeded.MaxScanSize FOUND' for p in paths)
                output += f'\nScanned files: 10\nInfected files: {len(paths)}\nTotal errors: {errors}\n{extra}'
                output += f'\n--- 検査終了情報 ---\nClamAV exit code: {code}\n\n--- この検査で使用した設定 ---\n{settings}'
                status = summarize(code, output)
                store.save('test', target or plan.describe(), status, output, key)
                return scan_notification(plan.label, status, output, store=store, key=key)
            self.assertEqual(report(['/a'])[2], 'critical')
            title, body, urgency = report(['/a'])
            self.assertEqual(urgency, 'normal')
            self.assertIn('継続中の検査上限超過：1件', body)
            self.assertIn('検査不完了', body)
            title, body, urgency = report(['/b'])
            self.assertEqual(urgency, 'critical')
            self.assertIn('新規の検査上限超過：1件', body)
            self.assertIn('今回報告されなかった検査上限超過：1件', body)
            self.assertEqual(report(['/b'], code=2)[2], 'critical')
            self.assertEqual(report(['/b'])[2], 'critical')
            self.assertEqual(report(['/b'], errors=1)[2], 'critical')
            self.assertEqual(report(['/b'])[2], 'critical')
            self.assertEqual(report(['/b'], extra='WARNING: 出力の一部を省略しました。')[2], 'critical')
            self.assertEqual(report(['/b'])[2], 'critical')
            self.assertEqual(report(['/b'], settings='changed')[2], 'critical')
            self.assertEqual(report(['/b'], target='changed', settings='changed')[2], 'critical')
            title, body, urgency = report([], code=0, target='changed', settings='changed')
            self.assertEqual(urgency, 'normal')
            self.assertIn('今回報告されなかった検査上限超過：1件', body)
