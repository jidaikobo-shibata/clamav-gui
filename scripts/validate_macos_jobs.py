"""Explicit, windowless launchd integration test with temporary plans and files.

Requires approval before use. Every created LaunchAgent is removed in finally.
Does not edit normal scan plans or request notification permission.
"""
from dataclasses import replace
import datetime
import json
import os
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clam_gui.core import Plan, Store
from clam_gui.macos_schedule import MacScheduler
from clam_gui.runtime import activities, request_stop
from clam_gui.schedule import Schedule


def wait_until(condition, timeout, description):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = condition()
        if result:
            return result
        time.sleep(.02)
    raise RuntimeError('Timed out: ' + description)


def main():
    with tempfile.TemporaryDirectory(prefix='clam-validation-') as directory:
        root = Path(directory)
        target = root / 'innocent files'
        target.mkdir()
        (target / 'hello.txt').write_text('This is harmless validation data.\n')
        for index in range(500):
            (target / (str(index) + '.txt')).write_text('Harmless local validation.\n' * 20)
        support = root / 'support'
        (support / 'definitions').mkdir(parents=True)
        (support / 'definitions/local.ndb').write_text('Local.Validation:0:*:434c414d5f4c4f43414c5f4d41524b4552\n')
        os.environ['CLAMAV_GUI_SUPPORT_DIR'] = str(support)
        store = Store(root / 'data')
        key = store.save_profile(Plan('一時検証', [str(target)], []))
        manager = MacScheduler(store, root / 'agents', namespace='validation.scan')
        agent = manager.agent(key)
        try:
            schedule = Schedule(True, 'interval', interval_minutes=1)
            manager.apply(key, schedule)
            print('Registered temporary one-minute completion-relative schedule.', flush=True)
            wait_until(lambda: len(store.recent()) >= 1, 110, 'first interval scan')
            first = store.history_entries(key, dict(store.profiles())[key])[0]
            if not first.status.startswith('検査完了'):
                raise RuntimeError(first.status + '\n' + first.output)
            running = wait_until(lambda: activities(store), 100, 'second interval scan')
            manager.apply(key, replace(schedule, enabled=False))
            wait_until(lambda: len(store.recent()) >= 2 and not activities(store), 60, 'active scan finishes after disable')
            second = store.history_entries(key, dict(store.profiles())[key])[0]
            completed = datetime.datetime.fromisoformat(first.completed_at)
            started = datetime.datetime.fromisoformat(second.started)
            gap = (started - completed).total_seconds()
            if gap < 58 or not second.status.startswith('検査完了'):
                raise RuntimeError('Interval/disable contract failed: ' + str(gap) + '\n' + second.status)
            print('Real interval/disable check passed; completion-to-start gap:', gap, flush=True)

            minute = (datetime.datetime.now() + datetime.timedelta(minutes=2)).minute
            manager.apply(key, Schedule(True, 'hourly', minute=minute))
            wait_until(lambda: len(store.recent()) >= 3 and not activities(store), 210, 'calendar scan')
            print('Real StartCalendarInterval firing passed.', flush=True)

            for index in range(500, 5500):
                (target / (str(index) + '.txt')).write_text('Harmless local validation.\n' * 20)
            agent.command('kickstart', agent.target)
            state = wait_until(lambda: activities(store), 30, 'scan to cancel')
            if not request_stop(store, state[0].token):
                raise RuntimeError('Cooperative cancellation request failed')
            wait_until(lambda: len(store.recent()) >= 4 and not activities(store), 45, 'cooperative cancellation')
            if '中止' not in store.recent()[0][2]:
                raise RuntimeError('Cancellation not recorded: ' + store.recent()[0][2])
            print('Real shared state and cooperative cancellation passed.', flush=True)
            manager.remove_profile(key)
            if store.profiles() or len(store.recent()) != 4:
                raise RuntimeError('Deleting profile did not preserve history')
            print('Temporary agent removed; scan history was preserved.', flush=True)
        finally:
            if agent.path.exists():
                agent.remove()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
