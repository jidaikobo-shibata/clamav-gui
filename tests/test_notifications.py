import datetime
from pathlib import Path
import tempfile
import unittest
from clam_gui.notifications import definition_status, scan_notification


class NotificationTests(unittest.TestCase):
    def test_home_completion_has_existing_script_information(self):
        started = datetime.datetime(2026, 11, 16, 2, 0, 0)
        finished = started + datetime.timedelta(hours=1, minutes=23, seconds=4)
        title, body, urgency = scan_notification('ホーム全体', '検査完了：168ファイル',
            'Scanned files: 168\nEngine version: 1.5.4', started, finished, '定義ファイル：正常')
        self.assertIn('完了', title)
        for expected in ['ホーム全体', '2026/11/16 03:23:04', '168', '1時間23分4秒', '正常', '1.5.4']:
            self.assertIn(expected, body)
        self.assertEqual(urgency, 'normal')

    def test_errors_and_detection_are_critical(self):
        now = datetime.datetime.now()
        for status in ['検出あり', '検出あり・検査不完了', '検査不完了']:
            title, body, urgency = scan_notification('対象', status, '', now, now, '定義不明')
            self.assertEqual(urgency, 'critical')
            self.assertIn('ログを確認', body)

    def test_definition_age(self):
        import os
        now = datetime.datetime.now().astimezone()
        with tempfile.TemporaryDirectory() as root:
            self.assertIn('確認できません', definition_status(root, now))
            path = Path(root)/'daily.cld'
            path.touch()
            self.assertIn('正常', definition_status(root, now))
            old = now.timestamp() - 5*86400
            os.utime(path, (old, old))
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
