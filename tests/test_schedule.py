from pathlib import Path
import subprocess
import tempfile
import unittest
from clam_gui.core import Plan, Store
from clam_gui.schedule import Schedule, Scheduler, read_schedule


class ScheduleTests(unittest.TestCase):
    def test_calendars_and_validation(self):
        self.assertEqual(Schedule(True, 'daily', 9, 5).calendar(), '*-*-* 09:05:00')
        self.assertEqual(Schedule(True, 'weekly', 9, 5, 6).calendar(), 'Sun *-*-* 09:05:00')
        self.assertEqual(Schedule(True, 'monthly', 9, 5, 0, 28).calendar(), '*-*-28 09:05:00')
        for schedule in [Schedule(hour=24), Schedule(minute=60), Schedule(weekday=7),
                         Schedule(monthday=29), Schedule(frequency='bad')]:
            with self.assertRaises(ValueError):
                schedule.calendar()

    def test_multiple_weekdays_calendar_and_description(self):
        schedule = Schedule(True, 'weekly', 2, 0, weekdays=[3, 0, 3])
        self.assertEqual(schedule.calendar(), 'Mon,Thu *-*-* 02:00:00')
        self.assertIn('毎週月・木曜日', schedule.describe())
        for days in ([], [7], [-1], [True], ['Mon'], 'Mon'):
            with self.subTest(days=days), self.assertRaises(ValueError):
                Schedule(frequency='weekly', weekdays=days).calendar()

    def test_multiple_weekdays_persist_and_duplicate(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            key = store.save_profile(Plan('test', [root], []))
            manager = Scheduler(store, Path(root)/'units', lambda *_: None)
            schedule = Schedule(True, 'weekly', 2, 0, weekdays=[1, 4])
            manager.apply(key, schedule)
            self.assertIn('OnCalendar=Tue,Fri *-*-* 02:00:00', manager.paths(key)[1].read_text())
            self.assertEqual(read_schedule(Store(root), key), schedule)
            copy = store.duplicate_profile(key)
            self.assertEqual(read_schedule(store, copy).weekdays, [1, 4])
            self.assertFalse(read_schedule(store, copy).enabled)

    def test_legacy_single_weekday_is_preserved(self):
        import json
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            with store.connect() as db:
                db.execute('INSERT INTO schedules VALUES (?, ?)', ('legacy', json.dumps(dict(enabled=True, frequency='weekly', weekday=4))))
            schedule = read_schedule(store, 'legacy')
            self.assertIsNone(schedule.weekdays)
            self.assertEqual(schedule.selected_weekdays(), [4])
            self.assertEqual(schedule.calendar(), 'Fri *-*-* 10:00:00')

    def test_apply_disable_and_safe_units(self):
        with tempfile.TemporaryDirectory(prefix='clam schedule ') as root:
            store = Store(Path(root)/'data')
            key = store.save_profile(Plan('test', [root], []))
            calls = []
            manager = Scheduler(store, Path(root)/'units', lambda *args: calls.append(args))
            schedule = Schedule(True, 'weekly', 12, 30, 1)
            manager.apply(key, schedule)
            service, timer = manager.paths(key)
            self.assertIn('OnCalendar=Tue *-*-* 12:30:00', timer.read_text())
            self.assertIn('Persistent=true', timer.read_text())
            self.assertIn('"--data-dir" "' + str(store.path.parent) + '"', service.read_text())
            self.assertIn(('enable', '--now', timer.name), calls)
            self.assertEqual(read_schedule(store, key), schedule)
            manager.apply(key, Schedule())
            self.assertIn(('disable', '--now', timer.name), calls)
            self.assertFalse(read_schedule(store, key).enabled)

    def test_failure_restores_files_and_saved_schedule(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(Path(root)/'data')
            key = store.save_profile(Plan('test', [root], []))
            manager = Scheduler(store, Path(root)/'units', lambda *args: None)
            old = Schedule(True, hour=8)
            manager.apply(key, old)
            paths = manager.paths(key)
            content = [p.read_text() for p in paths]
            def fail(*args):
                if args[0] == 'restart':
                    raise RuntimeError('failure')
            manager.run = fail
            with self.assertRaises(RuntimeError):
                manager.apply(key, Schedule(True, hour=9))
            self.assertEqual(read_schedule(store, key), old)
            self.assertEqual([p.read_text() for p in paths], content)

    def test_unowned_unit_and_root_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(Path(root)/'data')
            key = store.save_profile(Plan('test', [root], []))
            calls = []
            manager = Scheduler(store, Path(root), lambda *args: calls.append(args))
            manager.paths(key)[0].write_text('human-owned')
            with self.assertRaises(ValueError):
                manager.apply(key, Schedule(True))
            self.assertEqual(calls, [])
            self.assertEqual(manager.paths(key)[0].read_text(), 'human-owned')
            admin = store.save_profile(Plan('system', ['/'], [], False, [], True))
            with self.assertRaises(ValueError):
                manager.apply(admin, Schedule(True))

    def test_systemd_accepts_calendar(self):
        for schedule in [Schedule(True), Schedule(True, 'weekly'), Schedule(True, 'weekly', weekdays=[0, 3]), Schedule(True, 'weekly', weekdays=list(range(7))), Schedule(True, 'monthly')]:
            result = subprocess.run(['systemd-analyze', 'calendar', schedule.calendar()],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


class ScheduledRunnerTests(unittest.TestCase):
    def test_disabled_schedule_never_scans(self):
        from scripts.run_scheduled import main
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            key = store.save_profile(Plan('test', [root], []))
            with patch('sys.argv', ['runner', '--data-dir', root, '--profile', key]), \
                 patch('scripts.run_scheduled.scan') as scanner:
                self.assertEqual(main(), 0)
                scanner.assert_not_called()

    def test_preflight_failure_is_saved_and_notified(self):
        from scripts.run_scheduled import main
        from clam_gui.schedule import save_schedule
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            key = store.save_profile(Plan('test', [root], []))
            save_schedule(store, key, Schedule(True))
            with patch('sys.argv', ['runner', '--data-dir', root, '--profile', key]), \
                 patch('scripts.run_scheduled.scan', side_effect=ValueError('別の検査が実行中です')), \
                 patch('clam_gui.platforms.linux.LinuxBackend.notify') as notify:
                self.assertEqual(main(), 2)
                notify.assert_called_once()
                self.assertEqual(notify.call_args.args[2], 'critical')
            self.assertIn('別の検査', store.recent()[0][2])


class ProfileActionsTests(unittest.TestCase):
    def test_duplicate_copies_conditions_and_disabled_schedule(self):
        from clam_gui.schedule import save_schedule
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            key = store.save_profile(Plan('資料', [root], [root+'/file'], False, [root+'/cache']))
            schedule = Schedule(True, 'weekly', 15, 30, 2)
            save_schedule(store, key, schedule)
            copy = store.duplicate_profile(key)
            second = store.duplicate_profile(key)
            rows = dict(store.profiles())
            self.assertEqual(rows[copy].targets, rows[key].targets)
            self.assertEqual(rows[copy].excludes, rows[key].excludes)
            self.assertEqual(rows[copy].excluded_dirs, rows[key].excluded_dirs)
            self.assertFalse(rows[copy].phishing)
            self.assertNotEqual(rows[copy].label, rows[second].label)
            self.assertFalse(read_schedule(store, copy).enabled)
            self.assertEqual(read_schedule(store, copy).weekday, 2)
            self.assertTrue(read_schedule(store, key).enabled)

    def test_delete_removes_units_and_preserves_history(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(Path(root)/'data')
            home_key = store.save_profile(Plan('home', [root], []))
            store.save('date', 'target', 'status', 'output')
            manager = Scheduler(store, Path(root)/'units', lambda *args: None)
            manager.apply(home_key, Schedule(True))
            paths = manager.paths(home_key)
            manager.remove_profile(home_key)
            self.assertFalse(any(p.exists() for p in paths))
            self.assertNotIn(home_key, dict(store.profiles()))
            self.assertEqual(len(store.recent()), 1)
            self.assertFalse(read_schedule(store, home_key).enabled)

    def test_stop_failure_preserves_profile(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(Path(root)/'data')
            key = store.save_profile(Plan('test', [root], []))
            manager = Scheduler(store, Path(root)/'units', lambda *args: None)
            manager.apply(key, Schedule(True))
            def failure(*args):
                if args[0] == 'disable':
                    raise RuntimeError('stop failed')
            manager.run = failure
            with self.assertRaises(RuntimeError):
                manager.remove_profile(key)
            self.assertIn(key, dict(store.profiles()))

    def test_delete_unscheduled_profile_needs_no_systemd(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            key = store.save_profile(Plan('test', [root], []))
            def unexpected(*args):
                raise AssertionError('should not call systemd')
            Scheduler(store, Path(root)/'units', unexpected).remove_profile(key)
            self.assertEqual(store.profiles(), [])


class ShortScheduleTests(unittest.TestCase):
    def test_hourly_calendar_and_interval_description(self):
        hourly = Schedule(True, 'hourly', minute=17)
        self.assertEqual(hourly.calendar(), '*-*-* *:17:00')
        self.assertIn('毎時17分', hourly.describe())
        interval = Schedule(True, 'interval', interval_minutes=7)
        self.assertIsNone(interval.calendar())
        self.assertIn('完了から7分後', interval.describe())
        for value in (0, 1441, True, 1.5):
            with self.assertRaises(ValueError):
                Schedule(True, 'interval', interval_minutes=value).calendar()

    def test_interval_timer_and_compatibility(self):
        from clam_gui.schedule import save_schedule
        with tempfile.TemporaryDirectory() as root:
            store = Store(Path(root)/'data')
            key = store.save_profile(Plan('test', [root], []))
            manager = Scheduler(store, Path(root)/'units', lambda *_: None)
            interval = Schedule(True, 'interval', interval_minutes=7)
            manager.apply(key, interval)
            text = manager.paths(key)[1].read_text()
            self.assertIn('OnActiveSec=7min', text)
            self.assertIn('OnUnitInactiveSec=7min', text)
            self.assertNotIn('OnCalendar=', text)
            self.assertNotIn('Persistent=', text)
            self.assertEqual(read_schedule(store, key), interval)
            manager.remove_profile(key)
            self.assertNotIn(key, dict(store.profiles()))
            old = Schedule(**{'enabled': True, 'frequency': 'daily', 'hour': 2, 'minute': 0,
                              'weekday': 0, 'monthday': 1})
            self.assertEqual(old.calendar(), '*-*-* 02:00:00')
            self.assertEqual(old.interval_minutes, 5)

    def test_hourly_calendar_is_accepted_by_systemd(self):
        result = subprocess.run(['systemd-analyze', 'calendar', Schedule(True, 'hourly', minute=17).calendar()],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
