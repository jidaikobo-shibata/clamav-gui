import tempfile
import unittest
from clam_gui.core import Plan, Store
from clam_gui.plan_order import MEDIA_ID, PREFERENCE, reconcile


class PlanOrderTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(temp.name)
        self.keys = [self.store.save_profile(Plan(name, [temp.name], [])) for name in ('A', 'B', 'C')]

    def ids(self, media=False):
        return [key for key, _ in self.store.display_profiles(media)]

    def test_default_order_unchanged_and_read_does_not_save_preference(self):
        self.assertEqual(self.ids(), self.keys)
        self.assertEqual(self.ids(True), self.keys + [MEDIA_ID])
        self.assertIsNone(self.store.preferences.get(PREFERENCE))

    def test_moves_persist_and_do_not_change_scanner_profile_order_or_data(self):
        original = self.store.profiles()
        with self.store.connect() as db:
            db.execute('INSERT INTO schedules VALUES (?,?)', (self.keys[0], '{"enabled": true}'))
        self.store.move_display_profile(self.keys[2], self.keys[0])
        self.assertEqual(self.ids(), [self.keys[2], *self.keys[:2]])
        reopened = Store(self.store.path.parent)
        self.assertEqual([key for key, _ in reopened.display_profiles()], self.ids())
        self.assertEqual(self.store.profiles(), original)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT data FROM schedules').fetchone()[0], '{"enabled": true}')
        self.store.move_display_profile(self.keys[2], self.keys[1], after=True)
        self.assertEqual(self.ids(), self.keys)

    def test_hidden_media_position_survives_disable_and_reenable(self):
        self.store.move_display_profile(MEDIA_ID, self.keys[1])
        self.assertEqual(self.ids(), self.keys)
        self.assertEqual(self.ids(True), [self.keys[0], MEDIA_ID, *self.keys[1:]])
        self.store.move_display_profile(self.keys[2], self.keys[0])
        self.assertEqual(self.ids(True), [self.keys[2], self.keys[0], MEDIA_ID, self.keys[1]])

    def test_deleted_and_new_and_duplicated_plans(self):
        self.store.move_display_profile(self.keys[2], self.keys[0])
        self.store.delete_profile(self.keys[1])
        added = self.store.save_profile(Plan('D', [str(self.store.path.parent)], []))
        copied = self.store.duplicate_profile(self.keys[0])
        self.assertEqual(self.ids(), [self.keys[2], self.keys[0], added, copied])
        self.store.save_profile(Plan('Renamed', [str(self.store.path.parent)], []), self.keys[2])
        self.assertEqual(self.ids()[0], self.keys[2])

    def test_stale_moves_do_not_overwrite_order(self):
        self.store.move_display_profile(self.keys[2], self.keys[0])
        before = self.store.preferences.get(PREFERENCE)
        for key, anchor in [('deleted', self.keys[0]), (self.keys[0], 'deleted')]:
            with self.assertRaises(ValueError):
                self.store.move_display_profile(key, anchor)
            self.assertEqual(self.store.preferences.get(PREFERENCE), before)
        self.store.move_display_profile(self.keys[0], self.keys[0])
        self.assertEqual(self.store.preferences.get(PREFERENCE), before)

    def test_reconcile_discards_invalid_duplicate_and_stale_ids(self):
        self.assertEqual(reconcile([None, {}, 'B', 'B', 'gone', 'A'], ['A', 'B', 'C']), ['B', 'A', 'C'])
        self.assertEqual(reconcile({'invalid': 'shape'}, ['A', 'B']), ['A', 'B'])
