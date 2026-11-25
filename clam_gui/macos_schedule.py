"""launchd calendars plus a completion-relative interval worker for macOS."""
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import signal
import subprocess
import threading
import time

from .core import validate_profile
from .launchd import LaunchAgent, helper_command
from .runtime import activities
from .schedule import read_schedule, save_schedule
from .paths import mac_data_directory


def boot_identity():
    # Epoch boot time is stable across processes. Absolute-time deadlines exclude sleep.
    result = subprocess.run(['/usr/sbin/sysctl', '-n', 'kern.boottime'],
                            capture_output=True, text=True, timeout=5, check=True)
    match = re.search(r'sec\s*=\s*(\d+),\s*usec\s*=\s*(\d+)', result.stdout)
    if not match:
        raise ValueError('macOSの起動識別情報を確認できませんでした。')
    return match[1] + ':' + match[2]


def deadline_table(store):
    with store.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS mac_schedule_deadlines '
                   '(profile_id TEXT PRIMARY KEY, boot TEXT NOT NULL, due REAL NOT NULL)')


def save_deadline(store, key, boot, due):
    deadline_table(store)
    with store.connect() as db:
        db.execute('INSERT INTO mac_schedule_deadlines VALUES (?, ?, ?) '
                   'ON CONFLICT(profile_id) DO UPDATE SET boot=excluded.boot, due=excluded.due',
                   (key, boot, due))


class MacScheduler:
    def __init__(self, store, directory=None, run=None, command=None, namespace='scan', boot=None):
        self.store = store
        self.directory = directory
        self.run = run
        self.command = command or helper_command
        self.namespace = namespace
        self.boot = boot or boot_identity

    def agent(self, key):
        digest = hashlib.sha256((str(self.store.path) + ':' + key).encode()).hexdigest()[:20]
        return LaunchAgent('com.jidaikobo.ClamAVGui.' + self.namespace + '.' + digest,
                           self.directory, self.run)

    def configuration(self, key, schedule):
        schedule.calendar()
        arguments = [*self.command(), 'schedule-worker' if schedule.frequency == 'interval' else 'scan',
                     '--data-dir', str(self.store.path.parent), '--profile', key,
                     '--support-dir', str(mac_data_directory())]
        data = {'ProgramArguments': arguments, 'ProcessType': 'Background', 'LowPriorityIO': True,
                'Nice': 10, 'Umask': 0o077, 'ExitTimeOut': 15,
                'ClamAVGuiSchedule': {k: v for k, v in asdict(schedule).items() if k != 'enabled' and v is not None},
                'ClamAVGuiStore': str(self.store.path), 'ThrottleInterval': 10}
        data['StandardErrorPath'] = str(self.store.path.parent / 'background-errors.log')
        if schedule.frequency == 'interval':
            data.update(RunAtLoad=True, KeepAlive={'SuccessfulExit': False})
        else:
            calendar = {'Minute': schedule.minute}
            if schedule.frequency != 'hourly':
                calendar['Hour'] = schedule.hour
            if schedule.frequency == 'weekly':
                calendar['Weekday'] = (schedule.weekday + 1) % 7
            if schedule.frequency == 'monthly':
                calendar['Day'] = schedule.monthday
            data['StartCalendarInterval'] = ([dict(calendar, Weekday=(day+1)%7) for day in schedule.selected_weekdays()] if schedule.frequency == 'weekly' and len(schedule.selected_weekdays()) > 1 else dict(calendar, Weekday=(schedule.selected_weekdays()[0]+1)%7) if schedule.frequency == 'weekly' else calendar)
        return data

    def needs_repair(self, key, schedule):
        data = self.agent(key).read()
        expected = self.configuration(key, schedule)
        return data is None or any(data.get(k) != v for k, v in expected.items())

    def apply(self, key, schedule):
        plan = dict(self.store.profiles()).get(key)
        if plan is None:
            raise ValueError('検査プランが見つかりません。')
        schedule.calendar()
        if schedule.enabled:
            validate_profile(plan)
            if plan.requires_admin:
                raise ValueError('管理者権限が必要な対象は定期実行できません。')
        agent = self.agent(key)
        previous = agent.read()
        old = read_schedule(self.store, key)
        current = self.configuration(key, schedule)
        changed = previous is None or any(previous.get(k) != v for k, v in current.items())
        scanning = any(item.profile_id == key for item in activities(self.store))
        if schedule.enabled and changed and scanning:
            raise ValueError('検査が完了してから予定を変更してください。実行中の検査は続行します。')
        try:
            if schedule.enabled and plan.kind == 'scheduled':
                if changed:
                    if previous is not None:
                        agent.unload()
                    agent.write(current)
                # Save before bootstrap so an interval worker sees the enabled plan.
                save_schedule(self.store, key, schedule)
                if schedule.frequency == 'interval' and (not old.enabled or changed):
                    save_deadline(self.store, key, self.boot(), time.monotonic() + schedule.interval_minutes * 60)
                agent.enable()
            else:
                save_schedule(self.store, key, schedule)
                if previous is not None:
                    agent.disable()
        except Exception as error:
            save_schedule(self.store, key, old)
            try:
                if not scanning:
                    agent.unload()
                    if previous is not None:
                        agent.write(previous)
                        if old.enabled:
                            agent.enable()
                    elif agent.path.exists():
                        agent.path.unlink()
                elif previous is not None and old.enabled:
                    agent.enable()
            except Exception as recovery:
                raise RuntimeError('予定登録に失敗し、復元も完了できませんでした。予定の状態を確認してください。') from recovery
            raise error

    def remove_profile(self, key):
        if any(item.profile_id == key for item in activities(self.store)):
            raise ValueError('検査が完了してからプランを削除してください。')
        agent = self.agent(key)
        if agent.read() is not None:
            agent.remove()
        self.store.delete_profile(key)
        deadline_table(self.store)
        with self.store.connect() as db:
            db.execute('DELETE FROM mac_schedule_deadlines WHERE profile_id=?', (key,))


def interval_worker(store, key, stop=None, scan_once=None, clock=None, boot=None):
    stop = stop or threading.Event()
    clock = clock or time.monotonic
    boot = boot or boot_identity()
    if scan_once is None:
        from scripts.run_scheduled import main
        scan_once = lambda: main(['--data-dir', str(store.path.parent), '--profile', key], cancel=stop)
    deadline_table(store)
    while not stop.is_set():
        schedule = read_schedule(store, key)
        if not schedule.enabled or schedule.frequency != 'interval':
            return 0
        with store.connect() as db:
            row = db.execute('SELECT boot, due FROM mac_schedule_deadlines WHERE profile_id=?', (key,)).fetchone()
        if row is None or row[0] != boot:
            due = clock() + schedule.interval_minutes * 60
            save_deadline(store, key, boot, due)
        else:
            due = row[1]
        if clock() < due:
            stop.wait(min(1, due - clock()))
            continue
        scan_once()
        # Re-read after a scan: disabling future starts never interrupts the current scan.
        schedule = read_schedule(store, key)
        if not schedule.enabled:
            return 0
        save_deadline(store, key, boot, clock() + schedule.interval_minutes * 60)
    return 0
