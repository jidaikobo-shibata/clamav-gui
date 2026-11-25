"""Windows-native acceptance checks with isolated plans, history and task names."""
import argparse
import os
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clam_gui.core import Plan, Store, scan
from clam_gui.schedule import Schedule
from clam_gui.platforms import get_backend


def gui():
    from clam_gui.__main__ import Application, GLib
    app = Application()
    errors = []
    sys.excepthook = lambda typ, error, tb: errors.append(str(error))
    def inspect():
        try:
            assert app.window.get_visible()
            assert app.store.profiles()
            app.preferences()
            app.package_setup()
            assert len(app.get_windows()) >= 3
        except Exception as error:
            errors.append(repr(error))
        GLib.timeout_add_seconds(2, app.quit)
        return False
    GLib.timeout_add_seconds(2, inspect)
    app.run([])
    if errors:
        raise AssertionError(errors)


def check_monitor(store, folder):
    from clam_gui.background import heartbeat
    from clam_gui.schedule import save_schedule
    def until(predicate, limit=60):
        end = time.monotonic() + limit
        while not predicate():
            if time.monotonic() >= end:
                raise AssertionError('監視処理がタイムアウトしました。')
            time.sleep(.4)

    plan = Plan('Windows watch', [str(folder)], [], kind='watch')
    key = store.save_profile(plan)
    save_schedule(store, key, Schedule(enabled=True))
    service = get_backend().background(store)
    service.apply(True)
    try:
        until(lambda: heartbeat(store))
        def baseline_ready():
            with store.connect() as db:
                return db.execute('SELECT count(*) FROM monitor_roots').fetchone()[0]
        until(baseline_ready)
        (folder/'change.txt').write_text('new file')
        until(lambda: store.profile_history(key, plan))
        assert 'Scanned files: 1' in store.profile_history(key, plan)[0][3]
        generation = heartbeat(store)['generation']
        service.apply(True)
        until(lambda: heartbeat(store).get('generation') not in (None, generation))
    finally:
        service.apply(False)
        until(lambda: not heartbeat(store))
    print('Background watch, restart and disable OK', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--gui', action='store_true')
    parser.add_argument('--schedule', action='store_true')
    parser.add_argument('--monitor', action='store_true')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        os.environ['XDG_DATA_HOME'] = tmp
        store = Store()
        folder = root/'日本語 folder'
        folder.mkdir()
        (folder/'sample.txt').write_text('ordinary text')
        omit = folder/'除外.txt'
        omit.write_text('excluded text')
        excluded_dir = folder/'除外 dir'
        excluded_dir.mkdir()
        (excluded_dir/'nested.txt').write_text('excluded text')
        plan = Plan('Windows 検査', [str(folder)], [str(omit)], excluded_dirs=[str(excluded_dir)])
        key = store.save_profile(plan)
        status, log = scan(plan, store, profile_id=key, lock_path=root/'test.lock')
        assert 'Scanned files: 1' in log and 'Infected files: 0' in log, (status, log)
        print('Scan and history OK', flush=True)
        if args.gui:
            gui()
            print('GTK activation and dialogs OK', flush=True)
        if args.monitor:
            watched = root/'watch'
            watched.mkdir()
            check_monitor(store, watched)
        if args.schedule:
            scheduler = get_backend().scheduler(store)
            try:
                for frequency in ('daily', 'weekly', 'monthly', 'hourly'):
                    scheduler.apply(key, Schedule(enabled=True, frequency=frequency, hour=23, minute=59))
                scheduler.apply(key, Schedule(enabled=True, frequency='interval', interval_minutes=1))
                end = time.monotonic() + 135
                while time.monotonic() < end and len(store.profile_history(key, plan)) < 2:
                    time.sleep(1)
                rows = store.profile_history(key, plan)
                assert len(rows) >= 2 and 'Scanned files: 1' in rows[0][3], rows[:1]
                print('Scheduled scan and history OK', flush=True)
            finally:
                scheduler.remove_profile(key)


if __name__ == '__main__':
    main()
