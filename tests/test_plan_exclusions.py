from pathlib import Path
import tempfile
import unittest
from dataclasses import replace
from clam_gui.core import Plan, Store, HistoryEntry
from clam_gui.plan_exclusions import exclusion_state, folder_reports
from clam_gui.report import build_report


class PlanExclusionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.a = self.root/'database'
        self.a.mkdir()
        self.b = self.root/'other'
        self.b.mkdir()
        self.store = Store(self.root/'app-data')
        self.plan = Plan('Current', [str(self.root)], [])
        self.key = self.store.save_profile(self.plan)

    def test_grouping_uses_immediate_parent_and_actual_directory(self):
        output = (f'{self.a}/a: Access denied\n{self.a}/b: Access denied\n'
                  f'{self.b}: Can\'t open directory.\n')
        entry = HistoryEntry(1, 'now', 'now', 0, '', '検査不完了', output, 'incomplete')
        folders = folder_reports(build_report(entry, preview_limit=None))
        self.assertEqual([f.path for f in folders], [str(self.a), str(self.b)])
        self.assertEqual([len(f.findings) for f in folders], [2, 1])
        self.assertTrue(all(f.eligible for f in folders))

    def test_scope_and_symlink_guards(self):
        self.assertTrue(exclusion_state(self.plan, self.root))
        self.assertTrue(exclusion_state(self.plan, self.root.parent))
        self.assertFalse(exclusion_state(self.plan, self.a))
        link = self.root/'link'
        link.symlink_to(self.root, target_is_directory=True)
        self.assertTrue(exclusion_state(self.plan, link))
        self.assertTrue(exclusion_state(self.plan, self.root/'missing'))

    def test_known_detection_and_existing_exclusion_are_not_candidates(self):
        self.assertIn('脅威', exclusion_state(self.plan, self.a, [self.a/'test']))
        self.assertIn('すでに除外', exclusion_state(replace(self.plan, excluded_dirs=[str(self.a)]), self.a))
        entry = HistoryEntry(1, 'now', 'now', 1, '', '検出あり', f'{self.a}/test: Test FOUND\n', 'detected')
        folder = folder_reports(build_report(entry))[0]
        self.assertTrue(folder.detected)
        self.assertFalse(folder.eligible)

    def test_merge_uses_current_plan_and_preserves_schedule_and_settings(self):
        changed = replace(self.plan, label='Renamed', excludes=[str(self.root/'file')],
                          excluded_dirs=[str(self.b)], phishing=False, watch_recursive=False)
        self.store.save_profile(changed, self.key)
        with self.store.connect() as db:
            db.execute('INSERT INTO schedules VALUES (?,?)', (self.key, '{"enabled": true}'))
        added = self.store.add_excluded_folders(self.key, [str(self.a)], expected_target=self.plan.targets)
        current = dict(self.store.profiles())[self.key]
        self.assertEqual(added, [str(self.a)])
        self.assertEqual(current, replace(changed, excluded_dirs=[str(self.b), str(self.a)]))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT data FROM schedules').fetchone()[0], '{"enabled": true}')

    def test_duplicate_and_covered_folders_are_not_added(self):
        child = self.a/'child'
        child.mkdir()
        self.store.add_excluded_folders(self.key, [str(self.a)], expected_target=self.plan.targets)
        self.assertEqual(self.store.add_excluded_folders(self.key, [str(self.a), str(child)],
                                                       expected_target=self.plan.targets), [])
        self.assertEqual(dict(self.store.profiles())[self.key].excluded_dirs, [str(self.a)])

    def test_invalid_second_folder_does_not_partially_save(self):
        with self.assertRaises(ValueError):
            self.store.add_excluded_folders(self.key, [str(self.a), str(self.root)], expected_target=self.plan.targets)
        self.assertEqual(dict(self.store.profiles())[self.key].excluded_dirs, [])

    def test_changed_scope_or_deleted_plan_does_not_restore_old_snapshot(self):
        self.store.save_profile(replace(self.plan, targets=[str(self.b)]), self.key)
        with self.assertRaises(ValueError):
            self.store.add_excluded_folders(self.key, [str(self.a)], expected_target=self.plan.targets)
        self.store.delete_profile(self.key)
        with self.assertRaises(ValueError):
            self.store.add_excluded_folders(self.key, [str(self.a)], expected_target=self.plan.targets)
        self.assertFalse(self.store.profiles())

    def test_media_is_not_editable(self):
        self.assertTrue(exclusion_state(replace(self.plan, kind='media'), self.a))
        with self.assertRaises(ValueError):
            self.store.add_excluded_folders('external-media', [str(self.a)], expected_target=self.plan.targets)

    def test_literal_highlight_escapes_markup_and_handles_repeated_matches(self):
        from clam_gui.plan_exclusions import highlight_path
        self.assertEqual(highlight_path('/a/<b>&/a', 'a'), '/<b>a</b>/&lt;b&gt;&amp;/<b>a</b>')
        self.assertEqual(highlight_path('/a/<b>', '<b>'), '/a/<b>&lt;b&gt;</b>')
        self.assertEqual(highlight_path('/a/<b>', ''), '/a/&lt;b&gt;')

    def test_coverage_respects_directory_boundaries(self):
        from clam_gui.plan_exclusions import covered_by
        self.assertTrue(covered_by(self.a/'child', [str(self.a)]))
        self.assertTrue(covered_by(self.a, [str(self.a)]))
        self.assertFalse(covered_by(str(self.a)+'-backup', [str(self.a)]))

    def test_delta_save_preserves_concurrently_added_exclusions(self):
        child = self.root/'new'
        child.mkdir()
        self.store.save_profile(replace(self.plan, excluded_dirs=[str(self.a), str(self.b)]), self.key)
        added, removed = self.store.edit_excluded_folders(self.key, [str(child)], [str(self.a)],
                                                          expected_target=self.plan.targets)
        self.assertEqual(added, [str(child)])
        self.assertEqual(removed, [str(self.a)])
        self.assertEqual(dict(self.store.profiles())[self.key].excluded_dirs, [str(self.b), str(child)])

    def test_invalid_addition_rolls_back_removal_too(self):
        self.store.save_profile(replace(self.plan, excluded_dirs=[str(self.a)]), self.key)
        with self.assertRaises(ValueError):
            self.store.edit_excluded_folders(self.key, [str(self.root)], [str(self.a)],
                                             expected_target=self.plan.targets)
        self.assertEqual(dict(self.store.profiles())[self.key].excluded_dirs, [str(self.a)])

    def test_delta_save_rechecks_known_threat_paths(self):
        with self.assertRaises(ValueError):
            self.store.edit_excluded_folders(self.key, [str(self.a)], [],
                                             expected_target=self.plan.targets, threats=[str(self.a/'threat')])
        self.assertEqual(dict(self.store.profiles())[self.key].excluded_dirs, [])
