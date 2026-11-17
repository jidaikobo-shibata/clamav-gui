"""Background entry point. Never imports GTK or opens an application window."""
import argparse
import os
from pathlib import Path
import signal
import sys
import threading

if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv=None):
    if '--check-runtime' in (argv if argv is not None else sys.argv[1:]):
        import clam_gui.macos_schedule
        import clam_gui.macos_update
        if 'gi' in sys.modules:
            raise RuntimeError('Background helper must not import GTK')
        print('Background runtime OK; GTK is not imported.')
        return 0
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=('scan', 'schedule-worker', 'update'))
    parser.add_argument('--data-dir')
    parser.add_argument('--profile')
    parser.add_argument('--support-dir')
    args = parser.parse_args(argv)
    if args.support_dir:
        os.environ['CLAMAV_GUI_SUPPORT_DIR'] = args.support_dir
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    if args.operation == 'update':
        from clam_gui.macos_update import run_update
        return run_update(cancel=stop)
    if not args.data_dir or not args.profile:
        parser.error('--data-dir and --profile are required for a scan')
    if args.operation == 'scan':
        from scripts.run_scheduled import main as scan
        return scan(['--data-dir', args.data_dir, '--profile', args.profile], cancel=stop)
    from clam_gui.core import Store
    from clam_gui.macos_schedule import interval_worker
    return interval_worker(Store(args.data_dir), args.profile, stop)


if __name__ == '__main__':
    raise SystemExit(main())
