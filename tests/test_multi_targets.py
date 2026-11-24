from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from clam_gui.core import Plan, Store, scan
from clam_gui.options import DEFAULTS, LIMITS
from clam_gui.plan_exclusions import exclusion_state
from clam_gui.schedule import Schedule, Scheduler


class MultipleTargetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.a, self.b = self.root/'a', self.root/'b'
        for path in (self.a, self.b, self.a/'skip', self.b/'skip'):
            path.mkdir()
        self.store = Store(self.root/'data')
        self.plan = Plan('two folders', [str(self.a), str(self.b)], [],
                         excluded_dirs=[str(self.b/'skip')])

    def test_save_duplicate_schedule_and_exclusions_in_second_target(self):
        key = self.store.save_profile(self.plan)
        self.assertEqual(dict(Store(self.root/'data').profiles())[key].targets, self.plan.targets)
        copy = self.store.duplicate_profile(key)
        self.assertEqual(dict(self.store.profiles())[copy].targets, self.plan.targets)
        Scheduler(self.store, self.root/'units', lambda *_: None).apply(key, Schedule(enabled=True))
        self.assertFalse(exclusion_state(self.plan, self.a/'skip'))
        self.assertEqual(exclusion_state(self.plan, self.b/'skip'), 'すでに除外されています。')
        self.assertTrue(exclusion_state(self.plan, self.b))
        self.assertTrue(exclusion_state(self.plan, self.root))
        self.store.edit_excluded_folders(key, [str(self.a/'skip')], [], expected_target=self.plan.targets)
        self.assertEqual(len(dict(self.store.profiles())[key].excluded_dirs), 2)

    def test_overlap_missing_and_monitor_multiple_targets_are_rejected(self):
        for targets, kind in (([str(self.a), str(self.a/'skip')], 'scheduled'),
                              ([str(self.a), str(self.a)], 'scheduled'),
                              ([str(self.a), str(self.root/'missing')], 'scheduled'),
                              (self.plan.targets, 'watch'), (self.plan.targets, 'media')):
            with self.subTest(targets=targets, kind=kind), self.assertRaises(ValueError):
                self.store.save_profile(Plan('bad', targets, [], kind=kind))
        for path in (str(self.b), str(self.root), str(self.root/'outside')):
            with self.subTest(exclusion=path), self.assertRaises(ValueError):
                self.store.save_profile(Plan('bad', self.plan.targets, [], excluded_dirs=[path]))

    def test_one_scanner_invocation_receives_all_targets_and_logs_them(self):
        scanner = self.root/'scanner'
        scanner.write_text('#!/usr/bin/python3\nimport sys\nprint("ARGS", *sys.argv[1:])\nprint("Infected files: 0")\n')
        scanner.chmod(0o700)
        with patch('clam_gui.core.shutil.which', return_value=str(scanner)), patch('clam_gui.core.supported_options', return_value=set(DEFAULTS)|set(LIMITS)):
            _, output = scan(self.plan, self.store, lock_path=self.root/'scan.lock')
        self.assertIn('-- ' + str(self.a) + ' ' + str(self.b), output)
        self.assertIn(str(self.a) + '\n' + str(self.b), self.store.recent()[0][1])
