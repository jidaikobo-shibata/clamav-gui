from dataclasses import replace
import json
from pathlib import Path
import plistlib
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from clam_gui.core import Plan, Store
from clam_gui.launchd import LaunchAgent
from clam_gui.macos_schedule import MacScheduler, interval_worker, save_deadline
from clam_gui.macos_setup import MacSetup, SetupError
from clam_gui.macos_update import MacAutoUpdateService, run_update
from clam_gui.runtime import track_scan
from clam_gui.schedule import Schedule, read_schedule, save_schedule


class FakeLaunchctl:
    def __init__(self):
        self.calls, self.jobs, self.disabled = [], {}, {}
        self.kick = lambda: None

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        operation, *args = command[1:]
        output, code = '', 0
        if operation == 'print-disabled':
            output = 'disabled services for gui/501 = {\n' + '\n'.join(
                '"' + key + '" => ' + str(value).lower() for key, value in self.disabled.items()) + '\n}'
        elif operation == 'list':
            if args:
                code = 0 if args[0] in self.jobs else 113
            else:
                output = 'PID\tStatus\tLabel\n' + '\n'.join(
                    ('123' if running else '-') + '\t0\t' + key for key, running in self.jobs.items())
        elif operation in ('enable', 'disable'):
            self.disabled[args[0].split('/')[-1]] = operation == 'disable'
        elif operation == 'bootstrap':
            data = plistlib.loads(Path(args[1]).read_bytes())
            self.jobs[data['Label']] = False
        elif operation == 'bootout':
            self.jobs.pop(args[0].split('/')[-1], None)
        elif operation == 'kill':
            self.jobs[args[-1].split('/')[-1]] = False
        elif operation == 'kickstart':
            self.kick()
        else:
            raise AssertionError(command)
        return subprocess.CompletedProcess(command, code, output, '')


class MacJobsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / '日本語 space'
        self.store = Store(self.root / 'data')
        self.key = self.store.save_profile(Plan('試験', [self.temp.name], []))
        self.run = FakeLaunchctl()
        self.manager = MacScheduler(self.store, self.root / 'agents', self.run,
                                    command=lambda: ['/a space/ClamAV Helper'], boot=lambda: 'boot')

    def service(self):
        root = self.root / 'support'
        backend = MacSetup(root, locate=lambda name: '/mock/' + name)
        backend.configure()
        service = MacAutoUpdateService(root, self.root / 'agents', self.run,
                                       command=lambda: ['/a space/ClamAV Helper'])
        return backend, service

    def test_calendars_and_arguments_are_plists_not_shell_strings(self):
        for schedule, expected in (
            (Schedule(True, 'daily', 9, 5), {'Hour': 9, 'Minute': 5}),
            (Schedule(True, 'weekly', 9, 5, 6), {'Hour': 9, 'Minute': 5, 'Weekday': 0}),
            (Schedule(True, 'monthly', 9, 5, 0, 28), {'Hour': 9, 'Minute': 5, 'Day': 28}),
            (Schedule(True, 'hourly', minute=17), {'Minute': 17}),
        ):
            self.manager.apply(self.key, schedule)
            data = self.manager.agent(self.key).read()
            self.assertEqual(data['StartCalendarInterval'], expected)
            self.assertEqual(data['ProgramArguments'][0], '/a space/ClamAV Helper')
            self.assertIn(str(self.store.path.parent), data['ProgramArguments'])
            self.assertFalse(data.get('RunAtLoad', False))

    def test_interval_is_completion_relative_and_excludes_missed_runs(self):
        self.manager.apply(self.key, Schedule(True, 'interval', interval_minutes=1))
        data = self.manager.agent(self.key).read()
        self.assertNotIn('StartInterval', data)
        self.assertTrue(data['RunAtLoad'])
        self.assertEqual(data['KeepAlive'], {'SuccessfulExit': False})
        now, starts = [0.0], []
        class Stop:
            def is_set(self): return False
            def wait(self, seconds): now[0] += seconds
        def scan():
            starts.append(now[0])
            now[0] += 20
            if len(starts) == 2:
                save_schedule(self.store, self.key, Schedule(False, 'interval', interval_minutes=1))
        save_deadline(self.store, self.key, 'previous-boot', 999999)
        interval_worker(self.store, self.key, Stop(), scan, lambda: now[0], 'new-boot')
        self.assertEqual(starts, [60, 140])

    def test_disable_does_not_stop_an_active_scan(self):
        self.manager.apply(self.key, Schedule(True))
        with track_scan(self.store, self.key, '2026-11-17T00:00:00+09:00') as scan:
            self.run.calls.clear()
            self.manager.apply(self.key, Schedule(False))
            self.assertFalse(read_schedule(self.store, self.key).enabled)
            self.assertFalse(scan.cancelled())
            self.assertFalse(any(call[1] in ('bootout', 'kill') for call in self.run.calls))

    def test_removing_running_plan_and_changing_its_frequency_are_blocked(self):
        self.manager.apply(self.key, Schedule(True))
        with track_scan(self.store, self.key, ''):
            with self.assertRaises(ValueError):
                self.manager.apply(self.key, Schedule(True, 'weekly'))
            with self.assertRaises(ValueError):
                self.manager.remove_profile(self.key)
        self.assertIn(self.key, dict(self.store.profiles()))

    def test_unowned_and_symlink_agents_are_preserved(self):
        path = self.manager.agent(self.key).path
        path.parent.mkdir(parents=True)
        path.write_bytes(plistlib.dumps({'Label': self.manager.agent(self.key).label}))
        with self.assertRaises(ValueError):
            self.manager.apply(self.key, Schedule(True))
        self.assertEqual(self.run.calls, [])
        path.unlink()
        path.symlink_to(self.store.path)
        with self.assertRaises(ValueError):
            self.manager.apply(self.key, Schedule(True))
        self.assertEqual(self.run.calls, [])

    def test_delete_preserves_scan_history_and_removes_only_own_agent(self):
        self.manager.apply(self.key, Schedule(True))
        path = self.manager.agent(self.key).path
        self.store.save('date', 'target', 'result', 'log', self.key)
        self.manager.remove_profile(self.key)
        self.assertFalse(path.exists())
        self.assertEqual(self.store.profiles(), [])
        self.assertEqual(len(self.store.recent()), 1)

    def test_failed_schedule_registration_restores_previous_plan(self):
        self.manager.apply(self.key, Schedule(True, hour=8))
        agent = self.manager.agent(self.key)
        original = agent.read()
        run = self.run
        def fail(command, **kwargs):
            if command[1] == 'bootstrap':
                data = plistlib.loads(Path(command[-1]).read_bytes())
                if data['StartCalendarInterval']['Hour'] == 9:
                    return subprocess.CompletedProcess(command, 5, '', 'failure')
            return run(command, **kwargs)
        self.manager.run = fail
        with self.assertRaises(RuntimeError):
            self.manager.apply(self.key, Schedule(True, hour=9))
        self.assertEqual(agent.read(), original)
        self.assertEqual(read_schedule(self.store, self.key).hour, 8)

    def test_auto_update_job_and_disable_are_scoped(self):
        backend, service = self.service()
        with patch('clam_gui.macos_setup.find_clamav', return_value='/mock/bin'):
            self.assertFalse(service.status().enabled)
            message, state = service.set_enabled(True)
            self.assertTrue(state.enabled)
            data = service.agent.read()
            self.assertEqual(data['StartInterval'], 7200)
            self.assertIn(str(backend.root), data['ProgramArguments'])
            self.run.jobs[service.agent.label] = True
            message, state = service.set_enabled(False)
            self.assertFalse(state.enabled)
            self.assertFalse(state.running)
        signals = [c for c in self.run.calls if c[1] == 'kill']
        self.assertEqual(signals, [['/bin/launchctl', 'kill', 'SIGTERM', service.agent.target]])

    def test_update_worker_does_not_install_or_open_windows(self):
        backend, service = self.service()
        with patch('clam_gui.macos_update.MacSetup') as setup:
            setup.return_value.inspect.return_value = backend.inspect()
            setup.return_value.prepare.side_effect = SetupError('通信できません')
            setup.return_value.last_output = 'details'
            self.assertEqual(run_update(root=backend.root), 2)
        state = json.loads((backend.root / 'update-state.json').read_text())
        self.assertEqual(state['state'], 'failed')
        self.assertIn('通信', state['message'])

    def test_manual_update_observes_existing_agent_without_spawning_another_updater(self):
        backend, service = self.service()
        with patch('clam_gui.macos_setup.find_clamav', return_value='/mock/bin'):
            service.set_enabled(True)
            (backend.root / 'update-state.json').write_text(json.dumps({'token': 'old', 'state': 'completed'}))
            def kick():
                (backend.root / 'update-state.json').write_text(json.dumps(
                    {'token': 'new', 'state': 'completed', 'message': 'done'}))
            self.run.kick = kick
            with patch('clam_gui.macos_setup.subprocess.Popen') as process:
                self.assertEqual(service.update(), 'done')
            process.assert_not_called()


if __name__ == '__main__':
    unittest.main()
