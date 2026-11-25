from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from clam_gui.core import Store, Plan, record_scan_failure, scan
from clam_gui.platforms import get_backend


class ScanFailureTests(unittest.TestCase):
    def test_scheduled_scan_continues_with_existing_definitions_and_records_update_failure(self):
        from clam_gui.options import DEFAULTS
        backend = type(get_backend())
        with tempfile.TemporaryDirectory() as root, \
             patch('clam_gui.core.supported_options', return_value=set(DEFAULTS)|{"max-filesize", "max-scansize"}), \
             patch('clam_gui.core.executable', return_value='clamscan'), \
             patch('clam_gui.definitions.daily_definition_age', return_value=0), \
             patch.object(backend, 'updater') as updater, \
             patch.object(backend, 'spawn_scanner') as scanner:
            store = Store(root)
            plan = Plan('test', [root], [])
            key = store.save_profile(plan)
            updater.return_value.update.side_effect = TimeoutError('offline')
            scanner.return_value.wait.return_value = 0
            status, _ = scan(plan, store, profile_id=key, lock_path=Path(root)/'lock',
                             wait=True, check_definitions=True)
            self.assertTrue(status.assessment.clean)
            self.assertNotIn('--fail-if-cvd-older-than=3', scanner.call_args.args[0])
            self.assertIn('offline', store.history_entries(key, plan)[0].output)

    def test_stale_definition_never_reports_clean_and_keeps_detection(self):
        from clam_gui.options import DEFAULTS, LIMITS
        from clam_gui.results import ResultKind
        from clam_gui.notifications import AutomaticNotices
        backend = type(get_backend())
        with tempfile.TemporaryDirectory() as root, \
             patch('clam_gui.core.supported_options', return_value=set(DEFAULTS)|set(LIMITS)), \
             patch('clam_gui.core.executable', return_value='clamscan'), \
             patch('clam_gui.definitions.daily_definition_age', return_value=5*86400), \
             patch.object(backend, 'spawn_scanner') as scanner:
            store = Store(root)
            plan = Plan('test', [root], [])
            key = store.save_profile(plan)
            for code, detected in ((0, False), (1, True), (2, False)):
                def fake(command, **kwargs):
                    if detected:
                        kwargs['stdout'].write(b'file: Test.Signature FOUND\n')
                    process = scanner.return_value
                    process.wait.return_value = code
                    return process
                scanner.side_effect = fake
                status, output = scan(plan, store, profile_id=key, lock_path=Path(root)/'lock')
                self.assertFalse(status.assessment.clean)
                self.assertEqual(status.assessment.detected, detected)
                self.assertIn('定義の鮮度に注意：', status)
                self.assertEqual(status.assessment.kind, {0:ResultKind.WARNING,1:ResultKind.DETECTED,2:ResultKind.INCOMPLETE}[code])
                self.assertTrue(store.history_headers(key, plan)[0].attention_label())
                self.assertEqual(AutomaticNotices().record(key, 'test', status, output).urgency, 'critical')

    def test_reason_and_profile_are_saved_without_replacing_existing_history(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            plan = Plan('test', [root], [])
            key = store.save_profile(plan)
            store.save('2026-11-17T09:00:00+09:00', plan.describe(), '検査完了', 'previous', key)
            reason = '別の検査が実行中です。完了後に再実行してください。'
            status, output = record_scan_failure(store, plan, ValueError(reason), key)
            self.assertIn(reason, status)
            self.assertIn(reason, output)
            history = store.history_entries(key, plan)
            self.assertEqual(len(history), 2)
            self.assertEqual(history[0].status, status)
            self.assertEqual(history[0].output, output)
            self.assertEqual(history[1].output, 'previous')

    def test_history_write_error_still_returns_both_reasons(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            with patch.object(store, 'save', side_effect=OSError('read-only database')):
                status, output = record_scan_failure(store, Plan('test', [root], []), ValueError('ClamAVが見つかりません。'))
            self.assertIn('ClamAVが見つかりません', status)
            self.assertIn('検査履歴も保存できませんでした', status)
            self.assertIn('read-only database', status)
            self.assertIn('履歴保存エラー', output)

    def test_empty_exception_keeps_its_type_and_supplied_start_time(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            started = '2026-11-17T09:00:00+09:00'
            status, _ = record_scan_failure(store, Plan('test', [root], []), TimeoutError(), started=started)
            self.assertIn('TimeoutError', status)
            self.assertEqual(store.recent()[0][0], started)
