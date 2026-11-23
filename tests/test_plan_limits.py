import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from clam_gui.core import Plan, Store, scan
from clam_gui.options import DEFAULTS, LIMITS


class PlanLimitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root/'data')
        self.plan = Plan('test', [str(self.root)], [])

    def test_old_profiles_inherit_and_follow_global_changes(self):
        key = self.store.save_profile(self.plan)
        with self.store.connect() as db:
            data = json.loads(db.execute('SELECT data FROM profiles WHERE id=?', (key,)).fetchone()[0])
            data.pop('limits')
            db.execute('UPDATE profiles SET data=? WHERE id=?', (json.dumps(data), key))
        plan = dict(self.store.profiles())[key]
        self.assertIsNone(plan.limits)
        self.store.set_scan_preferences(DEFAULTS, {'max-filesize': 128, 'max-scansize': 512})
        self.assertEqual(self.store.effective_limits(plan), self.store.scan_limits())

    def test_override_persists_duplicates_and_can_return_to_inheritance(self):
        self.plan.limits = {'max-filesize': 512, 'max-scansize': 1024}
        key = self.store.save_profile(self.plan)
        duplicate = self.store.duplicate_profile(key)
        self.store.set_scan_preferences(DEFAULTS, {'max-filesize': 128, 'max-scansize': 256})
        self.assertEqual(self.store.effective_limits(dict(self.store.profiles())[duplicate]), self.plan.limits)
        self.plan.limits = None
        self.store.save_profile(self.plan, key)
        self.assertEqual(self.store.effective_limits(dict(self.store.profiles())[key]), self.store.scan_limits())

    def test_invalid_overrides_are_rejected(self):
        for limits in ({}, [], {'max-filesize': 512, 'max-scansize': 128},
                       {'max-filesize': True, 'max-scansize': 512}):
            self.plan.limits = limits
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                self.store.save_profile(self.plan)

    def test_scanner_and_log_use_override_for_full_and_event_scans(self):
        scanner = self.root/'scanner'
        scanner.write_text('#!/usr/bin/python3\nimport sys\nprint("ARGS", *sys.argv[1:])\nprint("Infected files: 0")\n')
        scanner.chmod(0o700)
        target = self.root/'document'
        target.write_text('test')
        self.plan.limits = {'max-filesize': 512, 'max-scansize': 1024}
        with patch('clam_gui.core.shutil.which', return_value=str(scanner)), patch('clam_gui.core.supported_options', return_value=set(DEFAULTS)|set(LIMITS)):
            for event in (False, True):
                self.plan.kind = 'watch' if event else 'scheduled'
                kwargs = dict(files=[str(target)]) if event else {}
                _, output = scan(self.plan, self.store, lock_path=self.root/"scan.lock", **kwargs)
                self.assertIn('--max-filesize=512M', output)
                self.assertIn('--max-scansize=1024M', output)
                self.assertIn('1ファイルの上限: 512 MiB', output)
                self.assertIn('展開後の合計上限: 1024 MiB', output)
