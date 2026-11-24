"""Run one saved profile without loading GTK or changing profiles."""
import argparse
import datetime
from pathlib import Path
import signal
import subprocess
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clam_gui.platforms import get_backend
from clam_gui.jobs import execute_scan
from clam_gui.results import assess
from clam_gui.core import Store, scan, record_scan_failure
from clam_gui.execution import ScanAlreadyQueued
from clam_gui.schedule import read_schedule
from clam_gui.notifications import definition_status, scan_notification


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--profile', required=True)
    args = parser.parse_args()
    store = Store(args.data_dir)
    plan = dict(store.profiles()).get(args.profile)
    if plan is None:
        print('検査プランが見つかりません。', file=sys.stderr)
        return 2
    if plan.kind != 'scheduled' or not read_schedule(store, args.profile).enabled:
        return 0
    schedule = read_schedule(store, args.profile)
    interval = sys.platform == 'win32' and schedule.frequency == 'interval'
    clock_key = 'last-scheduled-completion-' + args.profile
    if interval:
        import time
        if time.time() - store.preferences.get(clock_key, 0) < 60 * schedule.interval_minutes:
            return 0
    cancel = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: cancel.set())
    started = datetime.datetime.now().astimezone()
    output = ''
    try:
        status, output = execute_scan(plan, store, cancel, scanner=scan, profile_id=args.profile,
                                      wait=True, check_definitions=True, can_start=lambda: read_schedule(store, args.profile).enabled and
                                      dict(store.profiles()).get(args.profile) == plan)
    except ScanAlreadyQueued as error:
        print(str(error))
        return 0
    except Exception as error:
        status, output = record_scan_failure(store, plan, error, args.profile,
                                             started.isoformat(timespec='seconds'))
    if interval:
        store.preferences.set(clock_key, time.time())
    if getattr(status, 'started_at', None):
        started = datetime.datetime.fromisoformat(status.started_at)
    finished = datetime.datetime.now().astimezone()
    title, body, urgency = scan_notification(plan.label, status, output, started, finished, get_backend().definition_status() if sys.platform == "win32" else definition_status())
    print(status)
    try:
        get_backend().notify(title, body, urgency)
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        print('デスクトップ通知を送れませんでした。履歴を確認してください。', file=sys.stderr)
    return 0 if assess(status, output).clean else 2


if __name__ == '__main__':
    raise SystemExit(main())
