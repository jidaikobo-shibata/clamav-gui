"""Run one saved profile without loading GTK or changing profiles."""
import argparse
import datetime
from pathlib import Path
import signal
import subprocess
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clam_gui.core import Store, scan, record_scan_failure
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
    if not read_schedule(store, args.profile).enabled:
        return 0
    cancel = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: cancel.set())
    started = datetime.datetime.now().astimezone()
    output = ''
    try:
        status, output = scan(plan, store, cancel, profile_id=args.profile)
    except Exception as error:
        status, output = record_scan_failure(store, plan, error, args.profile,
                                             started.isoformat(timespec='seconds'))
    finished = datetime.datetime.now().astimezone()
    title, body, urgency = scan_notification(plan.label, status, output, started, finished, definition_status())
    print(status)
    try:
        notification = subprocess.run(['notify-send', '--app-name=ClamAV GUI',
                        '--urgency=' + urgency, '--expire-time=0', title, body],
                        capture_output=True, timeout=10)
        if notification.returncode:
            print('デスクトップ通知を送れませんでした。履歴を確認してください。', file=sys.stderr)
    except (OSError, subprocess.TimeoutExpired):
        print('デスクトップ通知を送れませんでした。履歴を確認してください。', file=sys.stderr)
    return 0 if status.startswith('検査完了') else 2


if __name__ == '__main__':
    raise SystemExit(main())
