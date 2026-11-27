import tempfile
import unittest
from unittest.mock import Mock
from clam_gui.core import Store, Plan
from clam_gui.results import ScanStatus, ResultKind
from clam_gui.notifications import notification_target, parse_notification_target, AutomaticNotice, AutomaticNotices
from clam_gui.notification_outbox import NotificationOutbox
from clam_gui.notification_retry import NotificationRetry


class NotificationLinks(unittest.TestCase):
    def test_exact_record_and_deleted_record_are_not_confused(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            plan = Plan('test', [root], [])
            key = store.save_profile(plan)
            first = ScanStatus('検査完了', ResultKind.CLEAN)
            second = ScanStatus('検査完了', ResultKind.CLEAN)
            store.save('same', plan.describe(), first, 'first', key)
            store.save('same', plan.describe(), second, 'second', key)
            target = notification_target(key, first)
            self.assertEqual(parse_notification_target(target), (key, first.notification_token))
            self.assertEqual(store.notification_entry(key, first.notification_token).output, 'first')
            self.assertIsNone(store.notification_entry('other', first.notification_token))
            with store.connect() as db:
                db.execute('DELETE FROM scans WHERE notification_token=?', (first.notification_token,))
            self.assertIsNone(store.notification_entry(key, first.notification_token))
            self.assertIsNone(parse_notification_target('["plan","invalid"]'))

    def test_retry_and_outbox_preserve_notification_target(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            deliver = Mock()
            outbox = NotificationOutbox(store, deliver)
            self.addCleanup(outbox.close)
            target = '["plan","0123456789abcdef0123456789abcdef"]'
            retry = NotificationRetry(root)
            retry.add(AutomaticNotice('title', 'body', 'critical', target))
            retry.persist()
            retry = NotificationRetry(root)
            retry.load()
            retry.drain(outbox.enqueue, force=True)
            self.assertTrue(outbox.deliver_one())
            deliver.assert_called_once_with('title', 'body', 'critical', target=target)

    def test_batch_links_to_most_recent_result(self):
        notices = AutomaticNotices()
        first = ScanStatus('検査完了', ResultKind.CLEAN)
        first.notification_token = '1'*32
        second = ScanStatus('検査完了', ResultKind.CLEAN)
        second.notification_token = '2'*32
        notices.record('p', 'plan', first)
        notices.record('p', 'plan', second)
        self.assertEqual(parse_notification_target(notices.flush('p').target), ('p', '2'*32))
